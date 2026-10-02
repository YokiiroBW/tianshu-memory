"""Complete historical coverage over bounded owner requests and one local commit."""

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

import pytest
from test_auth_https import certificates as certificates
from test_process import server
from test_source_sync import rejected
from test_source_sync import sync as sync
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory.domain import canonical, fingerprint


def retain_history(sync, count=257):
    """Keep the original record dependency and append isolated old actor sources."""
    seeded, _, _ = sync.seed()
    template = tuple(
        copy.deepcopy(values[0]) for values in (sync.physicals, sync.admissions, sync.grants)
    )
    sources = [sync.admissions[0]["source"]]
    for index in range(1, count):
        physical, admission, grant = map(copy.deepcopy, template)
        key = dict(physical["key"], message_id=f"synthetic-history:{index:04d}")
        physical.update(key=key, physical_receipt_id=f"synthetic-history-physical:{index}")
        admission["selector"]["key"] = key
        admission["source"]["message_key"]["message_id"] = key["message_id"]
        admission["source"]["receipt_id"] = f"synthetic-history-admission:{index}"
        admission["physical_receipt_id"] = physical["physical_receipt_id"]
        grant["selector"] = admission["selector"]
        grant["entry_id"] = f"synthetic-history-entry:{index}"
        sync.physicals.append(physical)
        sync.admissions.append(admission)
        sync.grants.append(grant)
        sources.append(admission["source"])
    for index in range(1, count, 256):
        sync.service.source_authority.barrier(
            sync.service, sync.scope(), sources=sources[index : index + 256]
        )
    sync.calls.clear()
    return seeded


@pytest.mark.parametrize("count", [257, 513])
def test_complete_history_is_queried_in_bounded_batches_and_survives_process_restart(sync, count):
    retain_history(sync, count)
    sync.select(budget=0)
    batches = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    assert all(0 < len(body["selectors"]) <= 256 for body in batches)
    assert len({canonical(s) for body in batches for s in body["selectors"]}) == count
    assert sync.calls[-2][0] == "current" and sync.calls[-2][1]["admissions"] == []
    assert sync.calls[-1][0] == "head"
    with sync.store.transaction() as db:
        before = dict(db.execute("SELECT key,value FROM metadata"))
    # Actual Memory process restart; its persisted heads and full history are used.
    with server(sync.config_path) as client:
        response = client.post(
            "/internal/v1/memory/select", json=sync.selection(budget=0), timeout=20
        )
        assert response.status_code == 200, response.text
    with sync.store.transaction() as db:
        assert dict(db.execute("SELECT key,value FROM metadata")) == before


@pytest.mark.parametrize("change", ["edit", "retract"])
def test_late_old_dependency_changes_invalidate_after_full_history_was_retained(sync, change):
    seeded = retain_history(sync, 513)
    physical = sync.physicals[0]
    physical.update(
        revision=2,
        kind=change,
        content_digest="a" * 64,
        state="withdrawn" if change == "retract" else "active",
        physical_receipt_id="synthetic-history-late-physical",
    )
    if change == "edit":
        admission = sync.admissions[0]
        admission["source"]["message_key"]["revision"] = 2
        admission["source"]["receipt_id"] = "synthetic-history-late-admission"
        admission["physical_receipt_id"] = physical["physical_receipt_id"]
    sync.core_head["sequence"] += 1
    sync.select(budget=0)
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (seeded["group_ids"][0],)).fetchone()[
                0
            ]
            != "active"
        )
        assert db.execute("SELECT COUNT(*) FROM source_admissions").fetchone()[0] == 513
        assert db.execute("SELECT sequence FROM owner_heads WHERE owner='core'").fetchone()[0] == 11
        row = db.execute(
            "SELECT revision,state FROM physical_sources WHERE key=?",
            (fingerprint(physical["key"]),),
        ).fetchone()
        assert tuple(row) == (2, physical["state"])


@pytest.mark.parametrize(
    "moving",
    [
        "core",
        "platform",
        "final_platform",
        "viewer",
        "second_batch",
        "missing_admission",
        "null_source",
    ],
)
def test_moving_or_failed_batches_never_commit_a_prefix(sync, moving):
    retain_history(sync)
    with sync.store.transaction() as db:
        before = dict(db.execute("SELECT key,value FROM metadata"))
        heads = [tuple(row) for row in db.execute("SELECT * FROM owner_heads ORDER BY owner")]
    sync.physicals[0]["classification"].update(value="fictional", policy_version=2)
    sync.core_head["sequence"] += 1

    def changed(kind, body, response):
        if moving == "core" and kind == "snapshot":
            sync.core_head["sequence"] += 1
        elif moving == "platform" and kind == "current":
            sync.platform_head["sequence"] += 1
        elif moving == "final_platform" and kind == "current" and not body["admissions"]:
            response["head"]["sequence"] += 1
        elif moving == "viewer" and kind == "current" and not body["admissions"]:
            response["viewer_context"]["principal_id"] = "changed-principal"
        elif moving == "second_batch" and kind == "snapshot" and len(body["selectors"]) < 256:
            response["admissions"] = []
        elif moving == "missing_admission" and kind == "snapshot" and len(body["selectors"]) < 256:
            response["admissions"][0] = {
                "selector": response["admissions"][0]["selector"],
                "state": "missing",
            }
        elif moving == "null_source" and kind == "snapshot" and len(body["selectors"]) < 256:
            response["admissions"][0]["source"] = None

    sync.mutate = changed
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with sync.store.transaction() as db:
        assert dict(db.execute("SELECT key,value FROM metadata")) == before
        assert [
            tuple(row) for row in db.execute("SELECT * FROM owner_heads ORDER BY owner")
        ] == heads


def test_receipt_lookup_uses_index_instead_of_per_admission_json_table_scans(
    sync, monkeypatch, record_property
):
    retain_history(sync, 513)
    traced, locks = [], []
    transaction = sync.service.store.transaction

    @contextmanager
    def observed():
        with transaction() as db:
            started = perf_counter()
            db.set_trace_callback(traced.append)
            yield db
            locks.append(perf_counter() - started)

    monkeypatch.setattr(sync.service.store, "transaction", observed)
    started = perf_counter()
    sync.select(budget=0)
    elapsed = perf_counter() - started
    receipt_queries = [
        sql for sql in traced if "json_extract(payload,'$.source.receipt_id')=" in sql
    ]
    assert len(receipt_queries) == 513
    assert not any(
        "SELECT key,payload FROM source_admissions WHERE payload IS NOT NULL" in sql
        for sql in traced
    )
    with transaction() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM source_admissions WHERE payload IS NOT NULL "
            "AND json_extract(payload,'$.source.receipt_id')=? AND key!=? LIMIT 1",
            ("synthetic-history-admission:1", "other-key"),
        ).fetchall()
    assert any("USING INDEX admissions_receipt" in row[3] for row in plan)
    print(
        f"historical sources=513, receipt lookups={len(receipt_queries)}, transaction durations={locks}, total seconds={elapsed}"
    )
    record_property(
        "source_scale",
        json.dumps(
            dict(
                sources=513,
                receipt_queries=len(receipt_queries),
                transaction_seconds=locks,
                total_seconds=elapsed,
            )
        ),
    )


def test_receipt_alias_across_batches_is_rejected_without_partial_sync(sync):
    retain_history(sync)
    with sync.store.transaction() as db:
        before = dict(db.execute("SELECT key,value FROM metadata"))
    sync.admissions[-1]["source"]["receipt_id"] = sync.admissions[0]["source"]["receipt_id"]
    sync.core_head["sequence"] += 1
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with sync.store.transaction() as db:
        assert dict(db.execute("SELECT key,value FROM metadata")) == before


def test_new_core_admission_cannot_hide_same_head_platform_authorization_change(sync):
    retain_history(sync)
    with sync.store.transaction() as db:
        before = dict(db.execute("SELECT key,value FROM metadata"))
    physical, admission = sync.physicals[0], sync.admissions[0]
    physical.update(
        revision=2,
        kind="edit",
        content_digest="a" * 64,
        physical_receipt_id="synthetic-authority-edit",
    )
    admission["source"]["message_key"]["revision"] = 2
    admission["source"]["receipt_id"] = "synthetic-authority-new-admission"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    sync.core_head["sequence"] += 1
    sync.grants[0]["state"] = "denied"
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with sync.store.transaction() as db:
        assert dict(db.execute("SELECT key,value FROM metadata")) == before


def test_complete_history_check_keeps_turn_inputs_together_and_retries_local_revision(sync):
    retain_history(sync, 513)
    changed = False

    def local_write(kind, body, response):
        nonlocal changed
        if kind == "current" and not body["admissions"] and not changed:
            changed = True
            with sync.store.transaction() as db:
                db.execute("INSERT INTO people VALUES ('synthetic-concurrent-person')")

    sync.mutate = local_write
    response = sync.post("memory/source-sync/check", sync.check())
    assert response.status_code == 200, response.text
    batches = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    assert len(batches) == 6
    assert all(batches[index]["turn_ids"] == [sync.turns[0]["turn_id"]] for index in (0, 3))
    assert all(not batches[index]["turn_ids"] for index in (1, 2, 4, 5))
    selected = {canonical(selector) for selector in batches[0]["selectors"]}
    assert canonical(sync.admissions[0]["selector"]) in selected


def test_one_complete_batch_sync_invalidates_more_than_256_records_once(sync):
    seeded = retain_history(sync, 257)
    with sync.store.transaction() as db:
        group = db.execute("SELECT * FROM groups WHERE id=?", (seeded["group_ids"][0],)).fetchone()
        record = db.execute(
            "SELECT * FROM records WHERE id=?", (seeded["record_ids"][0],)
        ).fetchone()
        lineage = db.execute("SELECT * FROM lineage WHERE group_id=?", (group["id"],)).fetchone()
        for index in range(300):
            group_id, record_id = (
                f"synthetic-large-group:{index}",
                f"synthetic-large-record:{index}",
            )
            payload = json.loads(record["payload"])
            payload["record_id"] = record_id
            payload["semantic_group_id"] = group_id
            db.execute(
                "INSERT INTO groups VALUES (?,?,?,?,?,?,?)",
                (
                    group_id,
                    group["scope"],
                    group["state"],
                    canonical([record_id]),
                    group["category"],
                    group["field_key"],
                    group["item_key"],
                ),
            )
            db.execute(
                "INSERT INTO records VALUES (?,?,1,'active',?)",
                (record_id, group_id, canonical(payload)),
            )
            db.execute(
                "INSERT INTO lineage VALUES (?,?,?,?)",
                (group_id, lineage["source_key"], lineage["revision"], lineage["epoch"]),
            )
    before = sync.select(budget=0)["scope_version"]
    sync.physicals[0].update(
        revision=2,
        state="withdrawn",
        kind="retract",
        content_digest="b" * 64,
        physical_receipt_id="synthetic-large-retract",
    )
    sync.core_head["sequence"] += 1
    assert sync.select(budget=0)["scope_version"] == before + 1
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM records WHERE state='invalidated'").fetchone()[0]
            == 301
        )
        assert (
            db.execute("SELECT COUNT(*) FROM groups WHERE state='invalidated'").fetchone()[0] == 301
        )
        assert db.execute("SELECT COUNT(*) FROM history WHERE version=2").fetchone()[0] == 301


def test_shared_batch_schema_and_rules_accept_exact_consumer_observation(sync, monkeypatch):
    captured = []
    synchronize = sync.service.source_authority.sync_batches

    def observed(service, observation, **kwargs):
        captured.append(copy.deepcopy(observation))
        return synchronize(service, observation, **kwargs)

    monkeypatch.setattr(sync.service.source_authority, "sync_batches", observed)
    retain_history(sync, 257)
    example = captured[-1]
    sync.service.contracts.validate("sync-batch#barrier", example)
    assert sync.service.contracts.source_batch_rules.batch_barrier(example, "2026-10-03T00:00:00Z")
    # Ignored, wholly synthetic evidence for the coordinator's shared validator.
    path = (
        Path(__file__).resolve().parents[1]
        / ".runtime"
        / f"dq-source-batch-joint-{sync.scope()['audience']}.json"
    )
    path.write_text(canonical(example), encoding="utf-8")
    mutated = copy.deepcopy(example)
    mutated["final_access"]["head"]["sequence"] += 1
    with pytest.raises(ValueError, match="moving_owner"):
        sync.service.contracts.source_batch_rules.batch_barrier(mutated, "2026-10-03T00:00:00Z")
