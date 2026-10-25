"""Complete pending obligations, compared to the former direct reader on real rows."""

import copy
import json

import pytest
from test_group_context import owner
from test_segment_io import ApplyReads
from test_segments import (
    MATERIALS,
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

from tianshu_memory import segment_sources
from tianshu_memory.domain import canonical, fingerprint


def jobs(db):
    return {
        row["id"]: (json.loads(row["event"]), json.loads(row["source_snapshot"]))
        for row in db.execute("SELECT * FROM jobs WHERE state='pending' AND extraction_schema=6")
    }


def direct_current(db, event, snapshot):
    """The original independent singleton predicates, without the batch maps."""
    for key, (revision, epoch) in snapshot.items():
        source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
        if (
            source is None
            or source["state"] != "active"
            or source["revision"] != revision
            or source["epoch"] != epoch
            or db.execute("SELECT 1 FROM suppression WHERE source_key=?", (key,)).fetchone()
        ):
            return False
    for identity in event["material_ids"]:
        material = db.execute("SELECT * FROM memory_materials WHERE id=?", (identity,)).fetchone()
        if material is None:
            return False
        payload = json.loads(material["payload"])
        member = payload["member"]
        binding = db.execute(
            "SELECT * FROM accounts WHERE account_key=?", (canonical(member["speaker"]["account"]),)
        ).fetchone()
        source = db.execute(
            "SELECT * FROM sources WHERE key=?", (material["source_key"],)
        ).fetchone()
        if not (
            binding is not None
            and binding["person_id"] == member["source_scope"]["person_id"]
            and binding["version"] == material["binding_version"]
            and source is not None
            and source["state"] == "active"
            and source["scope"] == material["scope"]
            and source["revision"] == payload["source_revision"]
            and source["epoch"] == payload["source_epoch"]
            and not db.execute(
                "SELECT 1 FROM suppression WHERE source_key=?", (source["key"],)
            ).fetchone()
            and (material["kind"] != "observation" or material["verified"] == 1)
        ):
            return False
    return bool(snapshot)


def compare(service, db, supplied):
    expected = {
        identity
        for identity, (event, snapshot) in supplied.items()
        if direct_current(db, event, snapshot)
    }
    actual = segment_sources.pending_current_many(service, db, supplied)
    assert actual == expected
    return actual


def observe_invalidation(monkeypatch):
    original, calls = segment_sources.pending_current_many, []

    def checked(service, db, supplied):
        expected = {
            identity
            for identity, (event, snapshot) in supplied.items()
            if direct_current(db, event, snapshot)
        }
        reads = ApplyReads(db)
        actual = original(service, reads, supplied)
        assert actual == expected
        calls.append((copy.deepcopy(supplied), actual, reads))
        return actual

    monkeypatch.setattr(segment_sources, "pending_current_many", checked)
    return calls


def states(db):
    return dict(db.execute("SELECT id,state FROM jobs"))


def test_cross_owner_context_and_private_scope_are_not_filtered_by_changed_scope(sync, monkeypatch):
    if sync.scope()["audience"] != "group":
        pytest.skip("Same-group context and a separate private conversation")
    ready(sync)
    second = owner(sync)
    private = owner(sync, audience="self_private", conversation="conversation:pending-private")
    first, _ = front(sync, "A问下周几点见。", "pending-a")
    another, _ = front(sync, "B说九点。", "pending-b", second)
    shared = seal(sync, [first, another], "segment:pending-shared")[0]["job_id"]
    later, _ = front(sync, "B补充在门口见。", "pending-b-later", second)
    inherited = seal(sync, [later], "segment:pending-inherited", context=[first])[0]["job_id"]
    separate, _ = front(sync, "私聊自己的安排。", "pending-private", private)
    isolated = seal(sync, [separate], "segment:pending-private")[0]["job_id"]
    calls = observe_invalidation(monkeypatch)
    with sync.service.local_commit() as db:
        supplied = jobs(db)
        assert set(supplied) == {shared, inherited, isolated}
        event, snapshot = supplied[inherited]
        assert first["source_key"] in snapshot
        assert first["material_ref"] in event["material_ids"]
        assert {target["scope"]["person_id"] for target in event["targets"]} == {
            another["person_id"]
        }
        before = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope(private))})
        assert states(db) == {identity: "pending" for identity in supplied}
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == before
        # A binding change in A must also retire B's inherited context, even
        # though A is not one of that job's targets and the trigger is private.
        db.execute(
            "UPDATE accounts SET version=version+1 WHERE account_key=?",
            (canonical(sync.physicals[0]["author"]),),
        )
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope(private))})
        assert states(db) == {
            shared: "scope_changed",
            inherited: "scope_changed",
            isolated: "pending",
        }
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    assert len(calls) == 2 and all(
        set(supplied) == {shared, inherited, isolated} for supplied, _, _ in calls
    )


@pytest.mark.parametrize("count", [100, 300])
def test_complete_pending_union_uses_bounded_reads_and_checks_the_last_page(
    sync, monkeypatch, count
):
    if sync.scope()["audience"] != "group":
        pytest.skip("One real passive group domain with over a SQL page of dependencies")
    ready(sync)
    refs = [
        archive(sync, native=f"pending-page:{index}", replies=(), accept_material=False)[1][
            "source_ref"
        ]
        for index in range(count)
    ]
    materials = []
    for start in range(0, count, 32):
        accepted = sync.post(
            MATERIALS, dict(schema_version=1, observation_refs=refs[start : start + 32])
        )
        assert accepted.status_code == 200, accepted.text
        materials.extend(accepted.json()["materials"])
    for start in range(0, count, 24):
        seal(sync, materials[start : start + 24], f"segment:page:{start}")
    calls = observe_invalidation(monkeypatch)
    with sync.service.local_commit() as db:
        supplied = jobs(db)
        all_materials = {
            identity for event, _ in supplied.values() for identity in event["material_ids"]
        }
        all_sources = {key for _, snapshot in supplied.values() for key in snapshot}
        assert all_materials == {material["material_ref"] for material in materials}
        assert len(all_sources) == count
        before = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope())})
        assert states(db) == {identity: "pending" for identity in supplied}
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == before
        assert len(calls) == 1 and set(calls[0][0]) == calls[0][1] == set(supplied)
        reads = calls[0][2]
        # Upper bound only: fewer reads or pipelines remain valid. These are
        # SQL statements, not a claim that every SELECT is a network trip.
        assert reads.statements <= 20
        assert all(
            not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in reads.sql
        )
        # Force the final sorted source page negative without passing its key
        # as a direct trigger. Every job depending on it must be discovered.
        tail = sorted(all_sources)[-1]
        db.execute("INSERT INTO suppression VALUES (?,?)", (tail, "pending:last-page"))
        expected = {identity for identity, (_, snapshot) in supplied.items() if tail in snapshot}
        assert expected
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope())})
        assert states(db) == {
            identity: "scope_changed" if identity in expected else "pending"
            for identity in supplied
        }
        assert calls[-1][2].statements <= 20
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "epoch",
        "withdrawn",
        "suppression",
        "binding",
        "person",
        "missing_source",
        "missing_material",
        "empty_snapshot",
    ],
)
def test_each_original_pending_obligation_is_preserved_and_read_again(sync, change):
    ready(sync)
    other = owner(sync, audience="self_private", conversation="conversation:pending-other")
    first, _ = front(sync, "完整的本人材料。", "predicate-first")
    second, _ = front(sync, "另一域的本人材料。", "predicate-other", other)
    first_job = seal(sync, [first], "segment:predicate-first")[0]["job_id"]
    second_job = seal(sync, [second], "segment:predicate-other")[0]["job_id"]
    with sync.service.local_commit() as db:
        supplied = jobs(db)
        assert compare(sync.service, db, supplied) == {first_job, second_job}
        if change in {"revision", "epoch"}:
            db.execute(
                f"UPDATE sources SET {change}={change}+1 WHERE key=?", (first["source_key"],)
            )
        elif change == "withdrawn":
            db.execute("UPDATE sources SET state='withdrawn' WHERE key=?", (first["source_key"],))
        elif change == "suppression":
            db.execute(
                "INSERT INTO suppression VALUES (?,?)", (first["source_key"], "pending:negative")
            )
        elif change in {"binding", "person"}:
            if change == "binding":
                db.execute(
                    "UPDATE accounts SET version=version+1 WHERE account_key=?",
                    (canonical(sync.physicals[0]["author"]),),
                )
            else:
                db.execute(
                    "UPDATE accounts SET person_id=? WHERE account_key=?",
                    (second["person_id"], canonical(sync.physicals[0]["author"])),
                )
        else:
            # Missing rows/empty snapshots are isolated negative inputs to the
            # real reader; no guarded business row or foreign key is removed.
            supplied = copy.deepcopy(supplied)
            if change == "missing_source":
                supplied[first_job][1]["source:absent"] = [1, 1]
            elif change == "missing_material":
                supplied[first_job][0]["material_ids"].append("material:absent")
            else:
                supplied[first_job][1].clear()
        assert compare(sync.service, db, supplied) == {second_job}
    # A separate transaction must also reread every binding/source predicate.
    with sync.service.local_commit() as db:
        assert compare(sync.service, db, supplied) == {second_job}
        sync.service._invalidate_jobs(db, source_keys={second["source_key"]})
        assert states(db)[second_job] == "stale_source"
        if change not in {"missing_source", "missing_material", "empty_snapshot"}:
            assert states(db)[first_job] == "scope_changed"
        else:
            assert states(db)[first_job] == "pending"


def test_alias_forget_and_later_invalidation_see_negatives_in_the_same_transaction(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("Genuine Core and observation alias with another author")
    ready(sync)
    good, _ = archive(sync, native="pending:good", author="60002", replies=())
    good_job = seal(sync, [good], "segment:good-pending")[0]["job_id"]
    material, archived = archive(sync, native="pending:alias", replies=())
    actor = owner(
        sync, account=dict(namespace="qq", immutable_account_id=archived["event"]["account_id"])
    )
    event = front(sync, archived["event"]["text"], "pending-alias", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = {
        key: archived[key] for key in ("source_ref", "source_digest", "scope_version")
    } | dict(
        instance_id=archived["event"]["platform_id"],
        self_id=archived["event"]["self_id"],
        conversation_id=archived["event"]["conversation_id"],
        archive_epoch=1,
    )
    accepted = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200 and accepted.json()["materials"] == [material], accepted.text
    first_job = seal(sync, [material], "segment:alias-pending")[0]["job_id"]
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])
    with sync.store.transaction() as db:
        frozen = jobs(db)
    calls = observe_invalidation(monkeypatch)
    # The public forget path, not a fabricated alias, reaches Service invalidation.
    forget_original(sync, dict(material, source_key=core_key), actor)
    with sync.service.local_commit() as db:
        assert states(db)[first_job] == "stale_source"
        assert {row[0] for row in db.execute("SELECT source_key FROM suppression")} >= {
            core_key,
            material["source_key"],
        }
        assert compare(sync.service, db, frozen) == {good_job}
        db.execute("UPDATE memory_materials SET verified=0 WHERE id=?", (good["material_ref"],))
        assert compare(sync.service, db, frozen) == set()
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope(actor))})
        assert states(db)[good_job] == "scope_changed"
        # A second call in this same transaction sees the previous negatives;
        # it neither writes another terminal event nor makes a row current.
        emitted = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope(actor))})
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == emitted
        assert compare(sync.service, db, frozen) == set()
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    assert calls


@pytest.mark.parametrize("change", ["epoch", "suppression"])
def test_inherited_summary_dependency_is_checked_without_a_material_id(sync, monkeypatch, change):
    ready(sync)
    previous, _ = front(sync, "上周约好去河边。", "summary-dependency")
    first = seal(sync, [previous], "segment:summary-dependency")[0]
    prepared = pending(sync, first)
    completed, _ = submit(sync, prepared, output(prepared))
    assert completed.status_code == 200, completed.text
    current, _ = front(sync, "今天再讨论同一安排。", "summary-current")
    second = seal(sync, [current], "segment:summary-current")[0]
    prepared = pending(sync, second)
    assert prepared["state"] == "pending"
    calls = observe_invalidation(monkeypatch)
    with sync.service.local_commit() as db:
        supplied = jobs(db)
        event, snapshot = supplied[second["job_id"]]
        assert previous["material_ref"] not in event["material_ids"]
        assert previous["source_key"] in snapshot
        assert compare(sync.service, db, supplied) == {second["job_id"]}
        if change == "epoch":
            db.execute("UPDATE sources SET epoch=epoch+1 WHERE key=?", (previous["source_key"],))
        else:
            db.execute(
                "INSERT INTO suppression VALUES (?,?)", (previous["source_key"], "pending:history")
            )
        sync.service._invalidate_jobs(db, scopes={canonical(sync.scope())})
        assert states(db) == {first["job_id"]: "committed", second["job_id"]: "scope_changed"}
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1
    assert len(calls) == 2
    assert calls[0][1] == {second["job_id"]} and calls[1][1] == set()


@pytest.mark.parametrize("schema", [3, 4, 5])
def test_legacy_pending_branch_keeps_its_actual_job_while_schema6_is_batched(
    sync, monkeypatch, schema
):
    from test_candidate_extraction import PATH
    from test_consolidation import pending as legacy_pending
    from test_consolidation import prepare, window
    from test_memory_v2 import candidate

    prepare(sync)
    old, _ = candidate(sync, "旧协议尚未提交的原话。", 1, capture=False)
    if schema == 5:
        legacy_pending(sync, [old], window(sync, [old]))
    else:
        response = sync.post(
            PATH, dict(job_id=old, output=None, supported_extraction_schemas=[schema])
        )
        assert response.status_code == 200 and response.json()["extraction_schema"] == schema, (
            response.text
        )
    ready(sync)
    fresh, _ = front(sync, "新段独立的原话。", "legacy-neighbor")
    new = seal(sync, [fresh], "segment:legacy-neighbor")[0]["job_id"]
    calls = observe_invalidation(monkeypatch)
    with sync.service.local_commit() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (old,)).fetchone()
        assert row["extraction_schema"] == schema
        old_keys = set(json.loads(row["source_snapshot"]))
        sync.service._invalidate_jobs(db, source_keys=old_keys)
        assert states(db) == {old: "stale_source", new: "pending"}
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    assert len(calls) == 1 and set(calls[0][0]) == {new}
