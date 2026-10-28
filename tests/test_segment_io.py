"""Real intake/checkpoint behavior with bounded SQL, never a wall-clock target."""

import copy
import json

import pytest
from test_consolidation_window_snapshot import ObservedReads
from test_group_context import owner
from test_memory_v2 import post, request
from test_segments import MATERIALS, archive, front, pending, ready, seal
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import segment_sources
from tianshu_memory.candidate_extraction import CandidateExtraction
from tianshu_memory.domain import Fault, canonical, fingerprint


def test_missing_segment_journal_is_not_ready_or_repaired_by_business_startup(sync):
    from tianshu_memory.app import configured_app
    from tianshu_memory.runtime_probes import probe_database
    from tianshu_memory.segment_migration import compatible

    ready(sync)
    with sync.store.transaction() as db:
        db.execute("ALTER TABLE turn_inputs DROP COLUMN event")
    guard = sync.store.recovery_path.read_bytes()
    assert probe_database(sync.store).state == "failed"
    with pytest.raises(Fault, match="dependency_unavailable"):
        configured_app(sync.config_path)
    with sync.store.transaction() as db:
        assert not compatible(db)
    assert sync.store.recovery_path.read_bytes() == guard


def test_front_reuses_two_complete_boundaries_and_preserves_raw_metadata(sync):
    ready(sync)
    event = front(sync, "今天去河边散步。", "bounded-front", accept=False)
    source = event["sources"][0]
    key = {key: value for key, value in source["message_key"].items() if key != "revision"}
    content = sync.test_contents[canonical(key)]
    content["reply_refs"] = [copy.deepcopy(sync.event()["sources"][0]["message_key"])]
    content["mentioned_accounts"] = [copy.deepcopy(content["author"])]
    physical = next(row for row in sync.physicals if row["key"] == key)
    physical["content_digest"] = fingerprint(content)
    sync.calls.clear()
    accepted = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200, accepted.text
    material = accepted.json()["materials"][0]
    calls = [kind for kind, _, _ in sync.calls if kind != "origin"]
    assert calls.count("head") == 2
    assert [body["include_content"] for kind, body, _ in sync.calls if kind == "snapshot"] == [
        False,
        True,
        False,
    ]
    with sync.store.transaction() as db:
        member = json.loads(
            db.execute(
                "SELECT payload FROM memory_materials WHERE id=?", (material["material_ref"],)
            ).fetchone()[0]
        )["member"]
        assert member["text"] == "今天去河边散步。"
        assert member["reply_refs"] == content["reply_refs"]
        assert member["mentioned_accounts"] == content["mentioned_accounts"]
        assert sync.service.read_committed(db, event["aggregate_id"]) == canonical(event)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT text FROM memory_documents WHERE source_key=?", (material["source_key"],)
            ).fetchone()[0]
            == member["text"]
        )


@pytest.mark.parametrize("change", ["withdrawal", "grant", "unavailable"])
def test_front_rechecks_after_body_read_and_keeps_independent_journal(sync, monkeypatch, change):
    ready(sync)
    event = front(sync, "原文读取后发生来源变化。", "after-read", accept=False)
    original = CandidateExtraction.read_originals
    read = []

    def changed(reader, received, **kwargs):
        body = original(reader, received, **kwargs)
        read.append(body)
        if change == "withdrawal":
            sync.physicals[-1].update(
                revision=2,
                state="withdrawn",
                kind="retract",
                content_digest="f" * 64,
                physical_receipt_id="physical:after-read:withdrawn",
            )
            sync.core_head["sequence"] += 1
        elif change == "grant":
            sync.grants[-1]["state"] = "denied"
            sync.platform_head["sequence"] += 1
        else:
            sync.status["current"] = 503
        return body

    monkeypatch.setattr(CandidateExtraction, "read_originals", changed)
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert read and read[0][0]["text"] == "原文读取后发生来源变化。"
    assert response.status_code in {403, 409, 503}, response.text
    with sync.store.transaction() as db:
        assert sync.service.read_committed(db, event["aggregate_id"]) == canonical(event)
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        receipt = json.loads(db.execute("SELECT result FROM inbox").fetchone()[0])
        assert receipt["state"] == "accepted" and receipt["candidate_job_ref"] is None
        key = sync.service.source_identity(event["sources"][0], event["scope"])
        state = db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0]
        assert state == ("active" if change == "unavailable" else "withdrawn")


def test_body_outage_keeps_received_event_without_claiming_material_success(sync, monkeypatch):
    ready(sync)
    event = front(sync, "正文暂时不可读。", "body-unavailable", accept=False)
    transport = sync.service.source_authority.transport
    original = transport.facts

    def unavailable(body):
        if body["include_content"]:
            raise Fault("dependency_unavailable", 503)
        return original(body)

    monkeypatch.setattr(transport, "facts", unavailable)
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 503, response.text
    with sync.store.transaction() as db:
        assert sync.service.read_committed(db, event["aggregate_id"]) == canonical(event)
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 0


class ApplyReads(ObservedReads):
    def __init__(self, db):
        super().__init__(db)
        self.sql = []

    def execute(self, sql, parameters=()):
        self.sql.append(sql)
        return super().execute(sql, parameters)

    def read_batch(self, requests):
        requests = list(requests)
        self.sql.extend(sql for sql, _ in requests)
        return super().read_batch(requests)


def test_hundred_passive_materials_are_all_reproved_with_bounded_steady_sql(sync, monkeypatch):
    if sync.scope()["audience"] != "group":
        pytest.skip("A complete group candidate domain")
    ready(sync)
    originals = [
        archive(sync, native=f"steady:{index}", replies=(), accept_material=False)[1]
        for index in range(100)
    ]
    refs = [value["source_ref"] for value in originals]
    materials = []
    for start in range(0, len(refs), 32):
        accepted = sync.post(
            MATERIALS, dict(schema_version=1, observation_refs=refs[start : start + 32])
        )
        assert accepted.status_code == 200, accepted.text
        materials.extend(accepted.json()["materials"])
    assert len(materials) == 100
    expected = {material["material_ref"] for material in materials}
    observed = []
    original = segment_sources.apply

    def measured(service, db, observations):
        supplied = {item["id"] for observation in observations for item in observation["rows"]}
        reads = ApplyReads(db)
        original(service, reads, observations)
        observed.append((supplied, reads))

    monkeypatch.setattr(segment_sources, "apply", measured)
    sync.passive_owner.calls.clear()
    body = request(sync, "周末散步")
    body["queries"][0]["kinds"] = ["episodes"]
    recalled = post(sync, "query", body)
    assert recalled.status_code == 200 and recalled.json()["evidence"], recalled.text
    assert observed and all(supplied == expected for supplied, _ in observed)
    assert all(reads.statements <= 16 for _, reads in observed)
    assert all(
        not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for _, reads in observed
        for sql in reads.sql
    )
    assert sync.passive_owner.calls
    assert all(
        {item["source_ref"] for item in call["sources"]} == set(refs)
        for call in sync.passive_owner.calls
    )
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM memory_materials WHERE verified=1").fetchone()[0]
            == 100
        )
        assert db.execute("SELECT COUNT(*) FROM sources WHERE state='active'").fetchone()[0] == 100
    # A new request cannot reuse the first request's allowed rows or grants.
    sync.passive_owner.read_allowed = False
    denied = post(sync, "query", body)
    assert denied.status_code == 403 and "evidence" not in denied.json(), denied.text


def test_mixed_passive_binding_negative_preserves_alias_withdrawal_and_later_owner(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two owners and their genuine physical alias")
    ready(sync)
    pairs = [
        archive(sync, text="同群本人的散步。", native="mixed:first", author="60001", replies=()),
        archive(sync, text="另一人的爬山。", native="mixed:second", author="60002", replies=()),
    ]
    (bad, archived), (good, _) = sorted(pairs, key=lambda pair: pair[0]["material_ref"])
    actor = owner(
        sync, account=dict(namespace="qq", immutable_account_id=archived["event"]["account_id"])
    )
    event = front(sync, archived["event"]["text"], "mixed-alias", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = {
        key: archived[key] for key in ("source_ref", "source_digest", "scope_version")
    } | dict(
        instance_id=archived["event"]["platform_id"],
        self_id=archived["event"]["self_id"],
        conversation_id=archived["event"]["conversation_id"],
        archive_epoch=1,
    )
    accepted = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200 and accepted.json()["materials"] == [bad], accepted.text
    prepared = pending(sync, seal(sync, [bad], "segment:mixed-pending")[0])
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])
    with sync.service.local_commit() as db:
        db.execute(
            "UPDATE accounts SET version=version+1 WHERE account_key=?",
            (
                canonical(
                    dict(namespace="qq", immutable_account_id=archived["event"]["account_id"])
                ),
            ),
        )
    checked = []
    original = segment_sources.apply

    def observed(service, db, observations):
        ordered = [item["id"] for observation in observations for item in observation["rows"]]
        original(service, db, observations)
        checked.append(ordered)

    monkeypatch.setattr(segment_sources, "apply", observed)
    identities = [bad["material_ref"], good["material_ref"]]
    # The profile source domain only needs these actual observation materials;
    # it never claims a fresh positive grant for the unselected Core alias.
    for _ in range(2):
        with sync.service.operation(
            scope=event["scope"], profile=True, material_ids=identities
        ) as db:
            rows = {
                row["id"]: row
                for row in db.execute(
                    "SELECT * FROM memory_materials WHERE id IN (?,?)", identities
                )
            }
            assert not segment_sources.current(sync.service, db, rows[bad["material_ref"]])
            assert segment_sources.current(sync.service, db, rows[good["material_ref"]])
            states = dict(db.execute("SELECT key,state FROM sources"))
            assert states[bad["source_key"]] == states[core_key] == "withdrawn"
            assert states[good["source_key"]] == "active"
            assert rows[bad["material_ref"]]["verified"] == 0
            assert rows[good["material_ref"]]["verified"] == 1
            assert (
                db.execute("SELECT state FROM jobs WHERE id=?", (prepared["job_id"],)).fetchone()[0]
                == "stale_source"
            )
    assert checked and all(order == identities for order in checked)
