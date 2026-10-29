"""Archive metadata may be valid without supplying any text to summarize."""

import json

from test_segments import MATERIALS, archive, output, pending, ready, seal, submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical


def test_empty_archive_is_acknowledged_without_learning_material(sync):
    ready(sync)
    _, original = archive(sync, text="", native="empty:archive", accept_material=False)
    request = dict(schema_version=1, observation_refs=[original["source_ref"]])
    first = sync.post(MATERIALS, request)
    assert first.status_code == 200, first.text
    sync.contracts.validate("memory-extraction#segment_material_response", first.json())
    assert first.json()["materials"] == []
    assert sync.post(MATERIALS, request).json() == first.json()
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM observation_sources").fetchone()[0] == 1


def test_empty_optional_archive_cannot_poison_text_segment(sync):
    ready(sync)
    archive(sync, text="", native="empty:context", accept_material=False)
    material, _ = archive(sync, text="周末计划去散步。", native="actual:text", replies=())
    response = pending(sync, seal(sync, [material], "text-with-empty-context")[0])
    assert all(member["text"] for member in response["segment_window"]["members"])


def test_existing_unprepared_job_omits_only_empty_context_without_resealing(sync):
    ready(sync)
    context, _ = archive(sync, text="原可选上下文。", native="legacy:context", replies=())
    material, _ = archive(sync, text="周末计划去散步。", native="legacy:new", replies=())
    sealed, _ = seal(sync, [material], "legacy:sealed", context=[context])
    # Seed the historical invalid sealed shape. Its source/material identities
    # remain genuine and verified; only optional text was missing in that event.
    with sync.store.transaction() as db:
        row = db.execute(
            "SELECT event,source_snapshot FROM jobs WHERE id=?", (sealed["job_id"],)
        ).fetchone()
        event = json.loads(row["event"])
        member = next(
            item for item in event["members"] if item["member_id"] == context["material_ref"]
        )
        member["text"] = ""
        encoded = canonical(event)
        snapshot = row["source_snapshot"]
        db.execute("UPDATE jobs SET event=? WHERE id=?", (encoded, sealed["job_id"]))
        assert (
            db.execute(
                "SELECT COUNT(*) FROM memory_batches WHERE job_id=?", (sealed["job_id"],)
            ).fetchone()[0]
            == 0
        )
    response = pending(sync, sealed)
    assert response["job_id"] == sealed["job_id"]
    assert response["segment_window"]["coverage"]["omitted_context"] is True
    assert [item["member_id"] for item in response["segment_window"]["members"]] == [
        material["material_ref"]
    ]
    assert pending(sync, sealed) == response
    with sync.store.transaction() as db:
        current = db.execute(
            "SELECT event,source_snapshot FROM jobs WHERE id=?", (sealed["job_id"],)
        ).fetchone()
        assert current["event"] == encoded
        assert set(json.loads(snapshot)) <= set(json.loads(current["source_snapshot"]))
    committed, _ = submit(sync, response, output(response))
    assert committed.status_code == 200, committed.text
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM write_ledger WHERE job_id=?", (sealed["job_id"],)
            ).fetchone()[0]
            == 1
        )
