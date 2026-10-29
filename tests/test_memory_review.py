"""Current-owner review, chronological pagination and replacement provenance."""

import json
from datetime import UTC, datetime

from test_browser_catalog import body, post
from test_browser_extensions import bootstrap
from test_group_context import owner
from test_segments import MATERIALS, forget_original, front, output, pending, seal, submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import browser
from tianshu_memory.domain import canonical, fingerprint, parse_time
from tianshu_memory.segment_migration import compatible


def saved(h, serial, text, observed_at, *, actor=0, portrait=None, **memory):
    event = front(h, text, serial, actor, accept=False)
    for source in event["sources"]:
        key = {key: value for key, value in source["message_key"].items() if key != "revision"}
        content = h.test_contents[canonical(key)]
        content["sent_at"] = observed_at
        next(row for row in h.physicals if row["key"] == key)["content_digest"] = fingerprint(
            content
        )
    response = h.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    material = response.json()["materials"][0]
    prepared = pending(h, seal(h, [material], "review:" + serial)[0])
    proposed = output(prepared, portrait=portrait)
    proposed["results"][0]["memories"][0].update(text=text, **memory)
    response, _ = submit(h, prepared, proposed)
    assert response.status_code == 200, response.text
    return material, response.json()


def begin(h):
    bootstrap(h)
    h.service.clock = lambda: datetime(2026, 10, 29, tzinfo=UTC)


def test_review_keeps_repeated_experiences_and_actual_agreement_state_in_own_scope(
    sync, monkeypatch
):
    begin(sync)
    saved(sync, "first", "又去河边散步。", "2026-10-27T00:00:00Z")
    saved(sync, "second", "又去河边散步。", "2026-10-28T00:00:00Z", portrait="愿意户外散步。")
    saved(sync, "older", "聊起旧书。", "2026-10-05T00:00:00Z")
    saved(
        sync,
        "open",
        "约好见面，尚未确认是否完成。",
        "2026-10-20T00:00:00Z",
        kind="agreement",
        state="confirmed",
        valid_until="2026-10-27T00:00:00Z",
        retention="long",
    )
    completed, _ = saved(
        sync,
        "done",
        "已完成取书约定。",
        "2026-10-28T12:00:00Z",
        kind="agreement",
        state="completed",
    )
    if sync.scope()["audience"] == "group":
        saved(sync, "foreign", "他人的独立秘密经历。", "2026-10-28T13:00:00Z", actor=owner(sync))
    response = post(sync, "overview")
    assert response.status_code == 200, response.text
    review = response.json()["review"]
    assert review["periods"]["7"]["count"] == 3
    # The unqualified Oct 5 experience now has the server's short retention.
    assert review["periods"]["30"]["count"] == 4
    assert [item["text"] for item in review["periods"]["7"]["items"]].count("又去河边散步。") == 2
    assert review["open_agreements"]["count"] == 1
    assert review["open_agreements"]["items"][0]["state"] == "confirmed"
    assert review["open_agreements"]["items"][0]["overdue"] is True
    assert review["portrait"]["text"] == "愿意户外散步。"
    from tianshu_memory.recall_trace import retention_distribution

    with sync.store.transaction() as db:
        distribution = retention_distribution(db)
        assert distribution["retention"]["long"] == 1
        assert distribution["importance"]["high"] == 1
        assert distribution["importance"].get("low", 0) == 0
    assert "他人的独立秘密" not in response.text
    assert "source_key" not in response.text and "message_key" not in response.text
    with monkeypatch.context() as limited:
        limited.setattr(browser, "MAX_OVERVIEW_SCAN", 2)
        partial = post(sync, "overview").json()
        assert partial["counts_truncated"] is True and partial["review"]["truncated"] is True
        assert partial["memory_group_count"] <= 2

    records, cursor = [], None
    while True:
        response = post(
            sync, "records", body(sync, limit=2, **({"cursor": cursor} if cursor else {}))
        )
        assert response.status_code == 200, response.text
        records.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert len(records) == len({item["semantic_group_id"] for item in records}) == 6
    dates = [parse_time(item["provenance"]["effective_at"]) for item in records]
    assert dates == sorted(dates, reverse=True)
    assert records[0]["units"][0]["statement"] == "已完成取书约定。"
    forget_original(sync, completed, 0)
    response = post(sync, "overview")
    assert "已完成取书约定" not in response.text
    assert response.json()["review"]["periods"]["7"]["count"] == 2


def test_timeline_byte_trimming_uses_the_same_time_cursor_without_skipping(sync, monkeypatch):
    begin(sync)
    for serial, stamp in (
        ("whole", "2026-10-28T00:00:00Z"),
        ("fraction", "2026-10-28T00:00:00.000001Z"),
        ("utc", "2026-10-28T00:00:01Z"),
    ):
        saved(sync, serial, serial + "记忆内容。" * 180, stamp)
    first = post(sync, "records", body(sync, limit=1)).json()
    monkeypatch.setattr(browser, "MAX_RESPONSE_BYTES", len(canonical(first).encode()) + 128)
    seen, cursor = [], None
    for _ in range(5):
        response = post(
            sync, "records", body(sync, limit=5, **({"cursor": cursor} if cursor else {}))
        )
        assert response.status_code == 200, response.text
        seen.extend(
            item["units"][0]["statement"].split("记忆内容")[0] for item in response.json()["items"]
        )
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == ["utc", "fraction", "whole"]


def test_replacement_keeps_revision_links_separate_and_obeys_context_withdrawal(sync):
    begin(sync)
    original, first = saved(
        sync, "old", "周末约好取书。", "2026-10-27T00:00:00Z", kind="agreement", state="confirmed"
    )
    ref = first["results"][0]["memory_refs"][0]
    saved(
        sync,
        "new",
        "已经取好书，约定完成。",
        "2026-10-28T00:00:00Z",
        kind="agreement",
        state="completed",
        operation="replace",
        prior_refs=[ref],
    )
    response = post(sync, "records")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert len(response.json()["items"]) == 1
    assert item["provenance"]["replacement_count"] == 1
    assert item["provenance"]["related_memory_count"] == 0
    assert post(sync, "overview").json()["review"]["open_agreements"]["count"] == 0
    forget_original(sync, original, 0)
    response = post(sync, "records")
    # The previous utterance was actually selected as conversation context.
    # Its withdrawal still hides the derived update; a revision link cannot
    # make that context or either saved text survive the original deletion.
    assert response.status_code == 200 and response.json()["items"] == [], response.text
    assert "已经取好书" not in response.text and "周末约好取书" not in response.text


def test_existing_segment_upgrade_preserves_records_and_backfills_only_order_metadata(sync):
    begin(sync)
    saved(sync, "upgrade", "记录排序仍用原话时间。", "2026-10-28T00:00:00Z")
    with sync.store.transaction() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM records ORDER BY id")]
        db.execute("ALTER TABLE memory_segment_items DROP COLUMN replaces")
        db.execute("ALTER TABLE memory_segment_items DROP COLUMN observed_epoch")
        db.execute("UPDATE metadata SET value='1' WHERE key='memory_segment_schema'")
        assert not compatible(db)
    backup = sync.directory / "before-review-upgrade.sqlite"
    receipt = sync.store.migrate_segments(backup)
    assert receipt["memory_segment_schema"] == 2 and receipt["upgraded_from"] == 1
    assert backup.is_file() and backup.with_name(backup.name + ".source-guard.json").is_file()
    with sync.store.transaction() as db:
        assert compatible(db)
        assert [tuple(row) for row in db.execute("SELECT * FROM records ORDER BY id")] == before
        row = db.execute("SELECT replaces,observed_epoch FROM memory_segment_items").fetchone()
        assert json.loads(row["replaces"]) == []
        assert row["observed_epoch"] == parse_time("2026-10-28T00:00:00Z").timestamp()
    assert sync.store.migrate_segments(backup) == dict(
        memory_segment_schema=2, already_installed=True
    )
    assert post(sync, "records").status_code == 200
