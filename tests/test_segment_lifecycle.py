"""Unsealed source loss is a real terminal job, never learned coverage."""

import copy
import json

import pytest
from test_group_context import owner
from test_segments import (
    PROCESS,
    SEAL,
    archive,
    forget_original,
    front,
    output,
    pending,
    ready,
    seal,
    submit,
)
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.segments import Segments


def create_request(materials, serial="segment:source-lifecycle"):
    return dict(
        schema_version=1,
        segment_id=serial,
        frame=materials[0]["frame"],
        material_refs=[item["material_ref"] for item in materials],
        context_refs=[],
        context_windows=[],
        omitted_context=False,
    )


def invalidated(h, request):
    # Discarding the first create acknowledgement leaves the exact same package
    # and durable job available; no new request identity or learned claim exists.
    first = h.post(SEAL, request)
    assert first.status_code == 200, first.text
    result = first.json()
    h.contracts.validate("memory-extraction#segment_seal_response", result)
    assert result["state"] == "invalidated" and result["job_id"] == result["candidate_job_ref"]
    assert h.post(SEAL, request).json() == result
    read = dict(extraction_schema=6, job_id=result["job_id"], batch_ref=None, output=None)
    response = h.post(PROCESS, read)
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-extraction#segment_invalidated_response", response.json())
    assert response.json() == dict(
        state="invalidated",
        extraction_schema=6,
        job_id=result["job_id"],
        batch_ref=None,
        result=None,
    )
    assert h.post(PROCESS, read).json() == response.json()
    with h.store.transaction() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (result["job_id"],)).fetchone())
        assert job["state"] == "stale_source"
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_segments").fetchone()[0] == 1
        for table in ("memory_segment_claims", "memory_batches", "write_ledger", "records"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        event = json.loads(job["event"])
        assert event["new_material_ids"] == request["material_refs"]
        assert {member["member_id"] for member in event["members"]} >= set(request["material_refs"])
        snapshot = json.loads(job["source_snapshot"])
        for identity in request["material_refs"]:
            material = db.execute(
                "SELECT * FROM memory_materials WHERE id=?", (identity,)
            ).fetchone()
            original = json.loads(material["payload"])
            assert snapshot[material["source_key"]] == [
                original["source_revision"],
                original["source_epoch"],
            ]
    different = dict(request, omitted_context=not request["omitted_context"])
    conflict = h.post(SEAL, different)
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    return event


@pytest.mark.parametrize("kind", ["core", "observation"])
def test_unsealed_forgotten_material_has_an_idempotent_negative_job(sync, kind):
    ready(sync)
    if kind == "core":
        material, _ = front(sync, "封盘前忘记的散步发言。", "unsealed-forget")
        actor = 0
    else:
        material, _ = archive(sync, text="封盘前忘记的旁听发言。", replies=())
        actor = owner(
            sync,
            audience=sync.scope()["audience"],
            account=dict(namespace="qq", immutable_account_id="60001"),
        )
    request = create_request([material])
    forget_original(sync, material, actor)
    # A known withdrawn input can stop locally even while a positive read of
    # the external owner would currently be unavailable.
    sync.status["current"] = 503
    sync.calls.clear()
    if kind == "observation":
        sync.passive_owner.calls.clear()
    event = invalidated(sync, request)
    assert sync.calls == []
    if kind == "observation":
        assert sync.passive_owner.calls == []
    assert event["members"][0]["text"] in {"封盘前忘记的散步发言。", "封盘前忘记的旁听发言。"}


def test_unsealed_archive_epoch_revocation_retains_original_versions(sync):
    ready(sync)
    material, _ = archive(sync, replies=())
    request = create_request([material])
    sync.passive_owner.archive_epoch += 1
    sync.passive_owner.revision += 1
    invalidated(sync, request)
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM sources WHERE key=?", (material["source_key"],)
            ).fetchone()[0]
            == "withdrawn"
        )
        assert db.execute("SELECT state FROM observation_sources").fetchone()[0] == "active"


@pytest.mark.parametrize("failure", ["forbidden", "unavailable", "unproved_conflict"])
def test_unsealed_errors_are_not_negative_receipts(sync, monkeypatch, failure):
    ready(sync)
    material, _ = front(sync, "仍须区分来源失效和权限错误。", "seal-error")
    request = create_request([material])
    if failure == "forbidden":
        # A negative source never substitutes for the caller's real authority.
        forget_original(sync, material, 0)
    if failure == "forbidden":
        sync.config["callers"]["companion"]["event_scopes"] = []
        sync.save()
        expected = 403
    elif failure == "unavailable":
        sync.status["current"] = 503
        expected = 503
    else:

        def conflict(self, event, job_id=None):
            raise Fault("scope_changed", 409)

        monkeypatch.setattr(Segments, "_operation", conflict)
        expected = 409
    response = sync.post(SEAL, request)
    assert response.status_code == expected, response.text
    assert "job_id" not in response.json() and "state" not in response.json()
    with sync.store.transaction() as db:
        for table in (
            "jobs",
            "memory_segments",
            "memory_segment_claims",
            "memory_batches",
            "write_ledger",
        ):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0


def test_durable_new_core_revision_stops_the_old_material_without_reading_the_old_turn(sync):
    ready(sync)
    material, event = front(sync, "原来约在周六。", "unsealed-revision")
    request = create_request([material])
    old_source = event["sources"][0]
    key = {name: value for name, value in old_source["message_key"].items() if name != "revision"}
    physical = next(row for row in sync.physicals if row["key"] == key)
    admission = next(row for row in sync.admissions if row["selector"]["key"] == key)
    physical.update(revision=2, kind="edit", physical_receipt_id="physical:unsealed-revision:2")
    body = dict(
        sync.test_contents[canonical(key)],
        message_key=dict(key, revision=2),
        kind="edit",
        parts=[dict(kind="text", text="改为周日。")],
    )
    physical["content_digest"] = fingerprint(body)
    sync.test_contents[canonical(key)] = body
    admission["source"]["message_key"]["revision"] = 2
    admission["source"]["receipt_id"] = "admission:unsealed-revision:2"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    event.update(
        input_revision=2,
        sources=[copy.deepcopy(admission["source"])],
        aggregate_version=event["aggregate_version"] + 1,
    )
    sync.turns[-1].update(
        input_revision=2,
        input_sources=event["sources"],
        aggregate_version=event["aggregate_version"],
    )
    sync.core_head["sequence"] += 1
    sync.platform_head["sequence"] += 1
    # The original real source-sync port durably observes revision2. There is
    # no job yet and the old immutable material keeps its captured revision1.
    checked = sync.post(
        "memory/source-sync/check",
        dict(
            sync.check(),
            turn_id=event["aggregate_id"],
            input_revision=2,
            scope=event["scope"],
            sources=event["sources"],
        ),
    )
    assert checked.status_code == 200, checked.text
    with sync.store.transaction() as db:
        row = db.execute(
            "SELECT revision,state FROM sources WHERE key=?", (material["source_key"],)
        ).fetchone()
        assert tuple(row) == (2, "active")
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0
    sync.status["current"] = 503
    sync.calls.clear()
    frozen = invalidated(sync, request)
    assert frozen["members"][0]["text"] == "原来约在周六。"
    assert sync.calls == []


def test_one_unsealed_negative_ends_the_whole_segment_without_covering_other_owner(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two real owners in one group")
    ready(sync)
    other = owner(sync)
    first, _ = front(sync, "稍后会忘记这一条。", "mixed-negative")
    second, _ = front(sync, "仍有效的另一人发言。", "mixed-positive", other)
    request = create_request([first, second])
    forget_original(sync, first, 0)
    event = invalidated(sync, request)
    assert {target["scope"]["person_id"] for target in event["targets"]} == {
        first["person_id"],
        second["person_id"],
    }
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM sources WHERE key=?", (second["source_key"],)).fetchone()[
                0
            ]
            == "active"
        )


def test_completed_segment_and_ledger_replay_precede_later_forget(sync):
    ready(sync)
    material, _ = front(sync, "已写入后再忘记。", "completed-negative")
    sealed, create = seal(sync, [material])
    prepared = pending(sync, sealed)
    completed, write = submit(sync, prepared, output(prepared))
    assert completed.status_code == 200, completed.text
    with sync.store.transaction() as db:
        original = dict(db.execute("SELECT * FROM write_ledger").fetchone())
    forget_original(sync, material, 0)
    replay = sync.post(SEAL, create)
    assert replay.status_code == 200 and replay.json()["state"] == "completed"
    assert replay.json()["job_id"] == sealed["job_id"]
    assert sync.post(PROCESS, write).json() == completed.json()
    with sync.store.transaction() as db:
        assert dict(db.execute("SELECT * FROM write_ledger").fetchone()) == original
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "committed"
