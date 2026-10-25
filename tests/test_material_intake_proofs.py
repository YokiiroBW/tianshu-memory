"""Real intake projections share one proof, never discovery or another owner."""

import json

import pytest
from test_group_context import owner
from test_segments import MATERIALS, archive, forget_original, front, ready
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import segment_materials
from tianshu_memory.domain import Fault, canonical, fingerprint, source_selector
from tianshu_memory.segment_materials import SegmentMaterials


def observation_mapping(original):
    event = original["event"]
    return dict(
        instance_id=event["platform_id"],
        self_id=event["self_id"],
        conversation_id=event["conversation_id"],
        source_ref=original["source_ref"],
        source_digest=original["source_digest"],
        scope_version=original["scope_version"],
        archive_epoch=1,
    )


def canonical_request(h, original, serial):
    actor = owner(
        h,
        account=dict(namespace="qq", immutable_account_id=original["event"]["account_id"]),
    )
    event = front(h, original["event"]["text"], serial, actor, accept=False)
    h.passive_owner.resolve[fingerprint(event["sources"][0])] = observation_mapping(original)
    return dict(schema_version=1, event=event, context_windows=[])


def stage_observer(h, monkeypatch, after=None):
    original = SegmentMaterials._stage_observations
    stages = []

    def observed(materials, *args, **kwargs):
        rows = original(materials, *args, **kwargs)
        with h.store.transaction() as db:
            snapshot = []
            for row in rows:
                current = dict(
                    db.execute("SELECT * FROM memory_materials WHERE id=?", (row["id"],)).fetchone()
                )
                episode = db.execute(
                    "SELECT 1 FROM memory_documents WHERE kind='episode' AND source_key=?",
                    (row["source_key"],),
                ).fetchone()
                snapshot.append((current, episode is not None))
        stages.append(snapshot)
        if after is not None:
            after(rows)
        return rows

    monkeypatch.setattr(SegmentMaterials, "_stage_observations", observed)
    return stages


def test_passive_union_proves_every_owner_history_before_atomic_projection(sync, monkeypatch):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two real people in one physical room")
    ready(sync)
    originals = [
        archive(sync, native=f"union:{index}", author=account, accept_material=False)[1]
        for index, account in enumerate(("60001", "60002"))
    ]
    histories = []
    for index, value in enumerate(originals):
        actor = owner(
            sync,
            account=dict(namespace="qq", immutable_account_id=value["event"]["account_id"]),
        )
        histories.append(front(sync, "本人的先前经历。", f"union-history:{index}", actor)[1])
    expected = {
        canonical(source_selector(source, event["scope"]))
        for event in histories
        for source in event["sources"]
    }
    stages = stage_observer(sync, monkeypatch)
    sync.calls.clear()
    sync.passive_owner.calls.clear()
    refs = [value["source_ref"] for value in originals]
    accepted = sync.post(MATERIALS, dict(schema_version=1, observation_refs=refs))
    assert accepted.status_code == 200, accepted.text
    materials = accepted.json()["materials"]
    assert len(materials) == 2 and len({row["person_id"] for row in materials}) == 2
    assert stages and all(
        row["verified"] == 0 and not has_episode for stage in stages for row, has_episode in stage
    )
    snapshots = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    selected = {canonical(item) for body in snapshots for item in body["selectors"]}
    assert expected <= selected
    assert sum(kind == "head" for kind, _, _ in sync.calls) == 1
    fixed = [body for body in sync.passive_owner.calls if body["actor_id"] is not None]
    assert len(fixed) >= 2
    assert all({row["source_ref"] for row in body["sources"]} == set(refs) for body in fixed)
    with sync.store.transaction() as db:
        originals_by_ref = {value["source_ref"]: value for value in originals}
        for material in materials:
            row = db.execute(
                "SELECT * FROM memory_materials WHERE id=?", (material["material_ref"],)
            ).fetchone()
            member = json.loads(row["payload"])["member"]
            original = originals_by_ref[json.loads(row["payload"])["archive"]["source_ref"]]
            assert row["verified"] == 1
            assert member["speaker"]["person_id"] == material["person_id"]
            episode = db.execute(
                "SELECT text,observed_at FROM memory_documents WHERE source_key=?",
                (material["source_key"],),
            ).fetchone()
            assert episode["text"] == original["event"]["text"]
            assert episode["observed_at"] == member["observed_at"]


def test_front_stages_canonical_archive_until_its_final_boundary_and_captures_both_sources(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("A real Core/observation physical bridge")
    ready(sync)
    _, original = archive(sync, text="晚上我去河边散步。", accept_material=False)
    body = canonical_request(sync, original, "stage-canonical")
    stages = stage_observer(sync, monkeypatch)
    sync.calls.clear()
    sync.passive_owner.calls.clear()
    accepted = sync.post(MATERIALS, body)
    assert accepted.status_code == 200, accepted.text
    material = accepted.json()["materials"][0]
    assert len(stages) == 1 and len(stages[0]) == 1
    staged, has_episode = stages[0][0]
    assert staged["verified"] == 0 and not has_episode
    assert material["material_ref"] == staged["id"]
    assert sum(kind == "head" for kind, _, _ in sync.calls) == 2
    assert [body["include_content"] for kind, body, _ in sync.calls if kind == "snapshot"] == [
        False,
        True,
        False,
    ]
    proofs = sync.passive_owner.calls
    assert len(proofs) >= 3
    assert all(
        [item["source_ref"] for item in proof["sources"]] == [original["source_ref"]]
        for proof in proofs
    )
    with sync.store.transaction() as db:
        fresh = db.execute(
            "SELECT * FROM memory_materials WHERE id=?", (material["material_ref"],)
        ).fetchone()
        member = json.loads(fresh["payload"])["member"]
        assert fresh["verified"] == 1
        assert member["reply_refs"] == original["event"]["message_context"]["reply_ids"]
        assert member["mentioned_accounts"] == [dict(namespace="qq", immutable_account_id="60002")]
        core_key = sync.service.source_identity(body["event"]["sources"][0], body["event"]["scope"])
        rows = list(db.execute("SELECT * FROM memory_documents WHERE kind='episode'"))
        assert {row["source_key"] for row in rows} == {core_key, material["source_key"]}
        assert all(row["text"] == member["text"] for row in rows)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert sync.service.read_committed(db, body["event"]["aggregate_id"]) == canonical(
            body["event"]
        )


@pytest.mark.parametrize("entry", ["passive", "canonical"])
@pytest.mark.parametrize("change", ["denied", "unavailable", "epoch", "binding"])
def test_stage_is_not_authority_when_owner_changes_before_final_projection(
    sync, monkeypatch, entry, change
):
    if sync.scope()["audience"] != "group":
        pytest.skip("A physical group with independent current archive permission")
    ready(sync)
    _, original = archive(sync, native="stage-change", accept_material=False)
    body = (
        canonical_request(sync, original, "stage-change")
        if entry == "canonical"
        else dict(schema_version=1, observation_refs=[original["source_ref"]])
    )

    def change_owner(rows):
        if change == "denied":
            sync.passive_owner.read_allowed = False
        elif change == "unavailable":
            sync.status["current"] = 503
        elif change == "epoch":
            sync.passive_owner.archive_epoch += 1
        else:
            with sync.service.local_commit() as db:
                db.execute(
                    "UPDATE accounts SET version=version+1 WHERE account_key=?",
                    (canonical(dict(namespace="qq", immutable_account_id="60001")),),
                )

    stages = stage_observer(sync, monkeypatch, change_owner)
    rejected = sync.post(MATERIALS, body)
    # A front admission still reports binding_version=1: local version=2
    # conflicts with that genuine Core fact before passive current() runs.
    expected_status = (
        503
        if entry == "canonical" and change == "binding"
        else {
            "denied": 403,
            "unavailable": 503,
            "epoch": 409,
            "binding": 409,
        }[change]
    )
    assert rejected.status_code == expected_status, rejected.text
    assert stages and all(row["verified"] == 0 for row, _ in stages[0])
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_material_aliases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 1
        if entry == "canonical":
            assert sync.service.read_committed(db, body["event"]["aggregate_id"]) == canonical(
                body["event"]
            )


def test_saved_alias_with_null_resolve_still_requires_fresh_observation_permission(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("A real physical alias whose resolver later has no new mapping")
    from tianshu_memory.candidate_extraction import CandidateExtraction

    ready(sync)
    _, original = archive(sync, native="saved-alias", accept_material=False)
    body = canonical_request(sync, original, "saved-alias")
    first = sync.post(MATERIALS, body)
    assert first.status_code == 200, first.text
    material = first.json()["materials"][0]
    sync.passive_owner.resolve.clear()
    read_originals = CandidateExtraction.read_originals

    def revoked(reader, event, **kwargs):
        result = read_originals(reader, event, **kwargs)
        sync.passive_owner.read_allowed = False
        return result

    monkeypatch.setattr(CandidateExtraction, "read_originals", revoked)
    rejected = sync.post(MATERIALS, body)
    assert rejected.status_code == 403, rejected.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 2
        assert (
            db.execute("SELECT material_id FROM memory_material_aliases").fetchone()[0]
            == material["material_ref"]
        )


@pytest.mark.parametrize("entry", ["passive", "canonical"])
def test_public_forget_after_staging_cannot_be_undone_by_final_material_projection(
    sync, monkeypatch, entry
):
    if sync.scope()["audience"] != "group":
        pytest.skip("The real owner explicitly forgets the staged physical source")
    ready(sync)
    material, original = archive(sync, native="stage-forget")
    if entry == "canonical":
        body = canonical_request(sync, original, "stage-forget")
        actor = next(
            index
            for index in range(len(sync.admissions))
            if sync.scope(index)["person_id"] == material["person_id"]
        )
    else:
        actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
        body = dict(schema_version=1, observation_refs=[original["source_ref"]])

    def forget_staged(rows):
        assert [row["id"] for row in rows] == [material["material_ref"]]
        forget_original(sync, material, actor)

    stages = stage_observer(sync, monkeypatch, forget_staged)
    rejected = sync.post(MATERIALS, body)
    assert rejected.status_code == 409, rejected.text
    assert stages[0][0][0]["verified"] == 1
    with sync.store.transaction() as db:
        assert db.execute(
            "SELECT 1 FROM suppression WHERE source_key=?", (material["source_key"],)
        ).fetchone()
        assert (
            db.execute(
                "SELECT state FROM sources WHERE key=?", (material["source_key"],)
            ).fetchone()[0]
            == "withdrawn"
        )
        assert db.execute("SELECT COUNT(*) FROM memory_material_aliases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT state FROM observation_sources WHERE source_ref=?",
                (original["source_ref"],),
            ).fetchone()[0]
            == "active"
        )


def test_second_owner_projection_failure_rolls_back_all_episodes_and_exact_retry_works(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two independent people in a single atomic projection request")
    ready(sync)
    originals = [
        archive(sync, native=f"atomic:{index}", author=account, accept_material=False)[1]
        for index, account in enumerate(("60001", "60002"))
    ]
    body = dict(schema_version=1, observation_refs=[row["source_ref"] for row in originals])
    stages = stage_observer(sync, monkeypatch)
    capture = segment_materials.capture
    attempted = []

    def fail_second(db, source, text, observed_at):
        attempted.append(source["scope"])
        if len(attempted) == 2:
            raise Fault("dependency_unavailable", 503)
        return capture(db, source, text, observed_at)

    monkeypatch.setattr(segment_materials, "capture", fail_second)
    rejected = sync.post(MATERIALS, body)
    assert rejected.status_code == 503, rejected.text
    assert len(set(attempted)) == 2
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM memory_materials WHERE verified=1").fetchone()[0] == 2
        )
        assert (
            db.execute("SELECT COUNT(*) FROM observation_sources WHERE state='active'").fetchone()[
                0
            ]
            == 2
        )
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    monkeypatch.setattr(segment_materials, "capture", capture)
    retried = sync.post(MATERIALS, body)
    assert retried.status_code == 200 and len(retried.json()["materials"]) == 2, retried.text
    assert len(stages) == 2
    assert all(row["verified"] == 0 and not has_episode for row, has_episode in stages[0])
    assert all(row["verified"] == 1 and not has_episode for row, has_episode in stages[1])
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 2
        )
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
