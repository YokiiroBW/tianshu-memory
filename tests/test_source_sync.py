"""Real HTTPS synthesized owners + configured Memory + isolated SQLite integration.

This verifies Memory's implementation, not deployed Core/Platform or cross-product L0.
"""

import copy
import json
import sqlite3
from contextlib import closing

import pytest
from source_sync_harness import SyncHarness
from test_auth_https import certificates as certificates
from test_process import server
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory.domain import Fault
from tianshu_memory.store import Store
from tianshu_memory.workflow import TrustedWorkflow


@pytest.fixture(params=["self_private", "group"])
def sync(tmp_path, source_contracts, source_examples, certificates, monkeypatch, request):
    harness = SyncHarness(tmp_path, source_contracts, source_examples, certificates, request.param)
    with harness.owners(), harness.runtime(monkeypatch):
        yield harness


def rejected(response, status=503, code="dependency_unavailable"):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text
    assert "synthetic-core-token" not in response.text


def barrier_kinds(sync):
    return [kind for kind, _, _ in sync.calls if kind != "origin"]


def test_configured_https_consume_candidate_select_probe_and_background_check(sync):
    assert sync.client.get("/health").json()["source_backend"] == "source_sync_https"
    assert barrier_kinds(sync) == []  # identity registration has no source authority dependency
    seeded, job, event = sync.seed()
    assert barrier_kinds(sync) == ["snapshot", "current", "head"] * 2
    selected = sync.select()
    assert len(selected["selected_units"]) == 1
    assert selected["selected_units"][0]["record_id"] == seeded["record_ids"][0]
    assert selected["selected_units"][0]["negations"] == ["晚上不喝咖啡"]
    probe = sync.select(budget=0, known=selected["scope_version"])
    assert probe["selected_units"] == [] and probe["omissions"] == ["budget"]
    checked = sync.post("memory/source-sync/check", sync.check())
    assert checked.status_code == 200, checked.text
    assert checked.json()["scope_version"] == selected["scope_version"]
    assert barrier_kinds(sync) == ["snapshot", "current", "head"] * 5
    replay = sync.post("memory/turn-commits", event)
    assert replay.status_code == 200 and replay.json()["state"] == "duplicate"
    assert replay.json()["candidate_job_ref"] == job
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM physical_sources").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM source_admissions WHERE verified=1").fetchone()[0] == 1
        )
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM source_writes").fetchone()[0] == 1
    for kind, request, token in sync.calls:
        if kind in {"snapshot", "head"}:
            assert token == "Bearer synthetic-core-token"
            assert request["include_content"] is False
        elif kind == "current":
            assert token == "Bearer synthetic-platform-token"
            assert request["operation"] == "current"


@pytest.mark.parametrize("kind", ["snapshot", "current", "head"])
def test_each_barrier_transport_failure_rejects_zero_budget_without_business_write(sync, kind):
    sync.seed()
    before = sync.select()["scope_version"]
    sync.status[kind] = 503
    rejected(sync.post("memory/select", sync.selection(budget=0, known=before)))
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE state='active'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1


def test_moving_core_retries_complete_barrier_then_fails_without_publishing(sync):
    def move(kind, body, response):
        if kind == "head":
            sync.core_head["sequence"] += 1
            response["head"] = copy.deepcopy(sync.core_head)

    sync.mutate = move
    rejected(sync.post("memory/turn-commits", sync.event()))
    assert barrier_kinds(sync) == ["snapshot", "current", "head"] * 3
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM source_admissions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM owner_heads").fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure", ["physical", "admission", "turn", "coverage", "grant", "digest"]
)
def test_missing_or_malformed_owner_facts_fail_closed(sync, failure):
    def break_response(kind, body, response):
        if kind == "snapshot":
            if failure == "physical":
                response["physicals"] = [
                    {"key": response["physicals"][0]["key"], "state": "missing"}
                ]
            elif failure == "admission":
                response["admissions"] = [{"selector": body["selectors"][0], "state": "missing"}]
            elif failure == "turn":
                response["turns"] = [{"turn_id": body["turn_ids"][0], "state": "missing"}]
            elif failure == "coverage":
                response["admissions"] = []
            elif failure == "digest":
                response["request_digest"] = "0" * 64
        elif kind == "current" and failure == "grant":
            response["grants"][0]["admission_digest"] = "0" * 64

    sync.mutate = break_response
    rejected(sync.post("memory/turn-commits", sync.event()))
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM source_admissions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field",
    ["causation_id", "turn_sequence", "confirmed_user_correction", "delivery_state", "reality"],
)
def test_entire_owner_event_and_physical_reality_are_verified(sync, field):
    event = sync.event()
    event[field] = {
        "causation_id": "synthetic-wrong-collector",
        "turn_sequence": 2,
        "confirmed_user_correction": True,
        "delivery_state": "failed",
        "reality": "fictional",
    }[field]
    response = sync.post("memory/turn-commits", event)
    if field == "confirmed_user_correction":
        rejected(response, 400, "invalid_input")
    else:
        rejected(response)
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_owner_event_agreement_cannot_invent_physical_reality(sync):
    sync.turns[0]["committed_event"]["reality"] = "fictional"
    rejected(sync.post("memory/turn-commits", sync.event()))
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("owner", ["core", "platform"])
@pytest.mark.parametrize("change", ["rollback", "generation"])
def test_both_owner_rollbacks_or_restoration_generations_are_rejected(sync, owner, change):
    sync.seed()
    head = getattr(sync, owner + "_head")
    if change == "rollback":
        head["sequence"] -= 1
    else:
        head["generation"] = "synthetic-restored-owner"
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with sync.store.transaction() as db:
        row = db.execute("SELECT * FROM owner_heads WHERE owner=?", (owner,)).fetchone()
        assert row["generation"] == f"synthetic-{owner}" and row["sequence"] == 10


@pytest.mark.parametrize("change", ["edit", "retract", "reality"])
def test_physical_negative_broadcasts_to_all_local_actor_lineage_in_one_transaction(sync, change):
    first, _, _ = sync.seed(0)
    second, _, _ = sync.seed(1)
    versions = [sync.select(actor=i)["scope_version"] for i in range(2)]
    physical = sync.physicals[0]
    if change == "edit":
        physical.update(
            revision=2,
            content_digest="a" * 64,
            physical_receipt_id="synthetic-physical:edit",
            kind="edit",
        )
    elif change == "retract":
        physical.update(
            revision=2,
            state="withdrawn",
            kind="retract",
            physical_receipt_id="synthetic-physical:retract",
            content_digest="b" * 64,
        )
    else:
        physical["classification"].update(value="fictional", policy_version=2)
    sync.core_head["sequence"] += 1
    rejected(
        sync.post("memory/select", sync.selection(budget=0, known=versions[0])),
        409,
        "scope_changed",
    )
    # The business probe conflicted, but both actors' invalidations have already committed.
    with sync.store.transaction() as db:
        ids = first["group_ids"] + second["group_ids"]
        assert all(
            db.execute("SELECT state FROM groups WHERE id=?", (id,)).fetchone()[0] != "active"
            for id in ids
        )
        assert db.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT COUNT(*) FROM owner_heads WHERE owner='core' AND sequence=11"
            ).fetchone()[0]
            == 1
        )
    assert sync.select(actor=1)["selected_units"] == []
    for actor in range(2):
        assert sync.select(actor=actor)["scope_version"] == versions[actor] + 1


def test_platform_denial_is_actor_local_and_invalidates_before_bad_event(sync):
    sync.seed(0)
    sync.seed(1)
    before = [sync.select(actor=i)["scope_version"] for i in range(2)]
    sync.grants[0]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    event = sync.event()
    event["causation_id"] = "synthetic-forged-event"
    rejected(sync.post("memory/turn-commits", event))
    assert sync.select()["selected_units"] == []
    assert len(sync.select(actor=1)["selected_units"]) == 1
    assert sync.select()["scope_version"] == before[0] + 1
    assert sync.select(actor=1)["scope_version"] == before[1]


@pytest.mark.parametrize("kind", ["forget", "correct"])
def test_suppression_is_actor_local_and_correction_never_publishes_new_value(sync, kind):
    first, _, _ = sync.seed(0)
    sync.seed(1)
    request = sync.revision(first["record_ids"][0], kind=kind)
    revised = sync.post("memory/revise", request)
    assert revised.status_code == 200, revised.text
    assert sync.select()["selected_units"] == []
    assert len(sync.select(actor=1)["selected_units"]) == 1
    with sync.store.transaction() as db:
        rows = db.execute(
            "SELECT a.actor_id,s.reason FROM suppression s JOIN source_admissions a ON a.key=s.source_key"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [("actor:a", kind)]
        assert db.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 1
    response = sync.post("memory/source-sync/check", sync.check())
    assert response.status_code in {409, 503}, response.text


def test_actual_memory_process_restart_keeps_suppression_and_owner_rollback_guard(sync):
    seeded, _, _ = sync.seed()
    request = sync.revision(seeded["record_ids"][0])
    with server(sync.config_path) as client:
        assert client.get("/health").json()["source_backend"] == "source_sync_https"
        selected = client.post("/internal/v1/memory/select", json=sync.selection())
        assert selected.status_code == 200 and len(selected.json()["selected_units"]) == 1
        revised = client.post("/internal/v1/memory/revise", json=request)
        assert revised.status_code == 200, revised.text
    with server(sync.config_path) as client:
        selected = client.post("/internal/v1/memory/select", json=sync.selection())
        assert selected.status_code == 200 and selected.json()["selected_units"] == []
        sync.platform_head["sequence"] -= 1
        rejected(client.post("/internal/v1/memory/select", json=sync.selection(budget=0)))


def test_missing_real_confirmation_issuer_is_unavailable_even_after_https_authority(sync):
    seeded, _, _ = sync.seed()
    request = {
        "command": sync.command(),
        "record_id": seeded["record_ids"][0],
        "expected_version": 1,
        "revision_kind": "forget",
        "confirmation_ref": "synthetic-unissued-confirmation",
        "evidence_refs": sync.event()["sources"],
        "replacement_statement": None,
    }
    with pytest.raises(Fault) as error:
        TrustedWorkflow(sync.service).confirm_revision(
            {
                "request": request,
                "verified_context": sync.contexts["synthetic-viewer:0"],
                "binding_version": 1,
                "expires_at": "2030-01-01T00:00:00Z",
            }
        )
    assert (error.value.code, error.value.status) == ("dependency_unavailable", 503)
    rejected(sync.post("memory/revise", request), 403, "forbidden")
    assert len(sync.select()["selected_units"]) == 1


def test_background_check_rejects_wrong_scope_before_owner_network(sync):
    request = sync.check()
    request["scope"]["actor_id"] = "synthetic-unconfigured-actor"
    rejected(sync.post("memory/source-sync/check", request), 403, "forbidden")
    assert barrier_kinds(sync) == []


def test_local_revision_change_during_owner_reads_retries_from_new_m0(sync):
    calls = []

    def mutate_once(kind, body, response):
        if kind == "current" and not calls:
            calls.append(kind)
            with sync.store.transaction() as db:
                db.execute(
                    "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'"
                )

    sync.mutate = mutate_once
    response = sync.post("memory/turn-commits", sync.event())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "accepted"
    assert barrier_kinds(sync) == ["snapshot", "current", "head"] * 2
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_platform_cannot_change_grant_at_the_same_owner_head(sync):
    sync.seed()
    sync.grants[0]["state"] = "denied"
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM groups WHERE state='active'").fetchone()[0] == 1


def test_delayed_physical_change_is_valid_after_unrelated_scope_observed_new_global_head(sync):
    sync.seed()
    previous = sync.select()["scope_version"]
    sync.core_head["sequence"] = 11
    # Another authorized conversation has empty coverage, so it learns the global head
    # without observing the changed physical row owned by the first conversation.
    other = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
    other["assertion_ref"] = "synthetic-other-conversation"
    other["allowed_scope"]["conversation_id"] = "synthetic-other-conversation"
    sync.contexts[other["assertion_ref"]] = other
    request = sync.selection(budget=0)
    request["query"]["origin"]["assertion_ref"] = other["assertion_ref"]
    request["requested_scope"] = other["allowed_scope"]
    response = sync.post("memory/select", request)
    assert response.status_code == 200, response.text
    sync.physicals[0].update(
        revision=2,
        content_digest="c" * 64,
        kind="edit",
        physical_receipt_id="synthetic-physical:delayed-edit",
    )
    rejected(
        sync.post("memory/select", sync.selection(budget=0, known=previous)), 409, "scope_changed"
    )
    assert sync.select()["selected_units"] == []


@pytest.mark.parametrize("budget", [0, 100000])
def test_cross_author_profile_queries_check_full_domain_even_for_absent_target(sync, budget):
    reader = sync.profile_fixture()
    assert reader["allowed_scope"]["person_id"] != sync.person
    selected = sync.post("memory/profiles/select", sync.profile_request(reader))
    assert selected.status_code == 200, selected.text
    assert len(selected.json()["selected_units"]) == 1
    assert "message_key" not in selected.text and "receipt_id" not in selected.text
    sync.calls.clear()
    sync.status["current"] = 503
    request = sync.profile_request(
        reader, budget=budget, target={"kind": "person", "person_id": "synthetic-absent-target"}
    )
    rejected(sync.post("memory/profiles/select", request))
    snapshots = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    assert snapshots[0]["selectors"] == [sync.admissions[0]["selector"]]


def test_profile_public_epoch_changes_only_on_first_active_projection_invalidation(sync):
    reader = sync.profile_fixture()
    selected = sync.post("memory/profiles/select", sync.profile_request(reader)).json()
    physical = sync.physicals[0]
    physical.update(
        revision=2,
        state="withdrawn",
        kind="retract",
        content_digest="d" * 64,
        physical_receipt_id="synthetic-profile-withdrawal",
    )
    sync.core_head["sequence"] += 1
    rejected(
        sync.post(
            "memory/profiles/select",
            sync.profile_request(reader, budget=0, known=selected["scope_version"]),
        ),
        409,
        "scope_changed",
    )
    current = sync.post("memory/profiles/select", sync.profile_request(reader)).json()
    assert current["selected_units"] == []
    assert current["scope_version"] == selected["scope_version"] + 1
    # A later private owner observation must not announce further history changes
    # through this already-withdrawn shared projection's public epoch.
    physical.update(
        revision=3, content_digest="e" * 64, physical_receipt_id="synthetic-later-withdrawal"
    )
    sync.core_head["sequence"] += 1
    sync.select()
    later = sync.post("memory/profiles/select", sync.profile_request(reader)).json()
    assert later["scope_version"] == current["scope_version"]


@pytest.mark.parametrize("damage", ["missing_checkpoint", "restored_database"])
def test_database_restore_or_missing_checkpoint_blocks_reads_and_reopen(sync, damage):
    seeded, _, _ = sync.seed()
    backup_path = sync.directory / "before-forget.sqlite"
    with (
        closing(sqlite3.connect(sync.store.path)) as source,
        closing(sqlite3.connect(backup_path)) as backup,
    ):
        source.backup(backup)
    request = sync.revision(seeded["record_ids"][0])
    response = sync.post("memory/revise", request)
    assert response.status_code == 200, response.text
    if damage == "missing_checkpoint":
        guard = sync.store.recovery_path
        guard.rename(guard.with_suffix(".quarantined"))
    else:
        # Explicit fault injection into this test's isolated SQLite file. The durable
        # checkpoint deliberately remains newer and prevents forgotten state resurfacing.
        with (
            closing(sqlite3.connect(backup_path)) as backup,
            closing(sqlite3.connect(sync.store.path)) as restored,
        ):
            backup.backup(restored)
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with pytest.raises(Fault) as error:
        Store(sync.store.path)
    assert (error.value.code, error.value.status) == ("dependency_unavailable", 503)


@pytest.mark.parametrize("count", [256, 257])
def test_memory_coverage_limit_is_complete_or_explicitly_unavailable(sync, count):
    physical, admission, grant = (
        copy.deepcopy(sync.physicals[0]),
        copy.deepcopy(sync.admissions[0]),
        copy.deepcopy(sync.grants[0]),
    )
    sync.physicals, sync.admissions, sync.grants = [], [], []
    sources = []
    for index in range(count):
        p, a, g = copy.deepcopy(physical), copy.deepcopy(admission), copy.deepcopy(grant)
        key = dict(p["key"], message_id=f"synthetic-limit-message:{index}")
        p.update(key=key, physical_receipt_id=f"synthetic-limit-physical:{index}")
        a["selector"]["key"] = key
        a["source"]["message_key"]["message_id"] = key["message_id"]
        a["source"]["receipt_id"] = f"synthetic-limit-admission:{index}"
        a["physical_receipt_id"] = p["physical_receipt_id"]
        g["selector"] = a["selector"]
        g["entry_id"] = f"synthetic-limit-entry:{index}"
        sync.physicals.append(p)
        sync.admissions.append(a)
        sync.grants.append(g)
        sources.append(a["source"])
    sync.turns[0]["input_sources"] = sources
    sync.turns[0]["committed_event"]["sources"] = sources
    sync.calls.clear()
    response = sync.post("memory/turn-commits", sync.event())
    if count == 257:
        rejected(response)
        assert barrier_kinds(sync) == []
    else:
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "accepted"
        assert (
            len(next(body for kind, body, _ in sync.calls if kind == "snapshot")["selectors"])
            == 256
        )
        assert (
            len(next(body for kind, body, _ in sync.calls if kind == "current")["admissions"])
            == 256
        )
        sync.calls.clear()
        sync.select(budget=0)
        assert (
            len(next(body for kind, body, _ in sync.calls if kind == "snapshot")["selectors"])
            == 256
        )


def test_discarded_committed_responses_replay_without_duplicate_source_or_relationship(sync):
    event = sync.event()
    with server(sync.config_path) as client:
        # The caller closes the real HTTP response before reading the JSON acknowledgement.
        # The request may already have committed; retry must use the identical event.
        with client.stream("POST", "/internal/v1/memory/turn-commits", json=event):
            pass
        replay = client.post("/internal/v1/memory/turn-commits", json=event)
        assert replay.status_code == 200, replay.text
        assert replay.json()["state"] == "duplicate"
        job_id = replay.json()["candidate_job_ref"]
    draft = copy.deepcopy(sync.examples["memory/candidate-commit"]["drafts"][0])
    draft.update(scope=sync.scope(), category="relationship", relationship_delta=4)
    draft["units"][0]["sources"] = event["sources"]
    request = {"job_id": job_id, "drafts": [draft]}
    workflow = TrustedWorkflow(sync.service)
    workflow.commit_candidate(request)  # The internal caller also discards its first result.
    replay = workflow.commit_candidate(request)
    assert replay["state"] == "committed"
    assert len(replay["record_ids"]) == len(replay["group_ids"]) == 1
    # Reopen verifies the independently persisted checkpoint as well as the SQLite state.
    with Store(sync.store.path).transaction() as db:
        for table in ("inbox", "jobs", "write_ledger", "source_writes", "relationship_entries"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert db.execute("SELECT amount FROM relationship_entries").fetchone()[0] == 4
        guard = json.loads(sync.store.recovery_path.read_text(encoding="utf-8"))
        assert guard["revision"] == int(
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        )


def test_corrupted_checkpoint_blocks_http_reads_and_store_reopen(sync):
    sync.seed()
    sync.store.recovery_path.write_bytes(b'{"schema":3,"revision":')
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with pytest.raises(Fault) as error:
        Store(sync.store.path)
    assert (error.value.code, error.value.status) == ("dependency_unavailable", 503)


def test_unsupported_restore_of_old_database_and_old_guard_is_indistinguishable(sync):
    """Document a limitation, not restore safety: paired rollback can revive forgotten data.

    A local guard provides no external monotonic anchor when both files are rolled back.
    This unsupported restore must not be advertised as protected by the checkpoint.
    """
    seeded, _, _ = sync.seed()
    backup_path = sync.directory / "unsupported-paired-rollback.sqlite"
    with (
        closing(sqlite3.connect(sync.store.path)) as source,
        closing(sqlite3.connect(backup_path)) as backup,
    ):
        source.backup(backup)
    old_guard = sync.store.recovery_path.read_bytes()
    request = sync.revision(seeded["record_ids"][0])
    response = sync.post("memory/revise", request)
    assert response.status_code == 200, response.text
    assert sync.select()["selected_units"] == []
    with (
        closing(sqlite3.connect(backup_path)) as backup,
        closing(sqlite3.connect(sync.store.path)) as restored,
    ):
        backup.backup(restored)
    sync.store.recovery_path.write_bytes(old_guard)
    with Store(sync.store.path).transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0
    visible = sync.select()["selected_units"]
    assert [unit["record_id"] for unit in visible] == seeded["record_ids"]


def test_repeated_stable_probes_keep_checkpoint_bytes_mtime_and_revision_unchanged(sync):
    sync.seed()
    version = sync.select(budget=0)["scope_version"]
    guard = sync.store.recovery_path
    before_bytes, before_mtime = guard.read_bytes(), guard.stat().st_mtime_ns
    for _ in range(2):
        assert sync.select(budget=0, known=version)["scope_version"] == version
    assert guard.read_bytes() == before_bytes
    assert guard.stat().st_mtime_ns == before_mtime
    with sync.store.transaction() as db:
        assert (
            int(db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0])
            == json.loads(before_bytes)["revision"]
        )


def test_same_core_head_cannot_regrant_a_new_admission_after_physical_edit(sync):
    sync.seed()
    sync.physicals[0].update(
        revision=2,
        kind="edit",
        content_digest="f" * 64,
        physical_receipt_id="synthetic-physical:new-revision",
    )
    sync.core_head["sequence"] += 1
    assert sync.select()["selected_units"] == []
    admission = sync.admissions[0]
    admission["source"]["message_key"]["revision"] = 2
    admission["source"]["receipt_id"] = "synthetic-admission:regranted"
    admission["physical_receipt_id"] = sync.physicals[0]["physical_receipt_id"]
    # Platform acknowledges the new admission at a new head. Core contradicts the
    # admission it already returned at its unchanged head and must still be rejected.
    sync.platform_head["sequence"] += 1
    rejected(sync.post("memory/select", sync.selection(budget=0)))
    with sync.store.transaction() as db:
        assert db.execute("SELECT revision FROM sources").fetchone()[0] == 1
