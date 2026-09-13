import copy
import sqlite3

import pytest

from tianshu_memory.domain import Fault
from tianshu_memory.service import MemoryService
from tianshu_memory.sources import LocalFixtureSources
from tianshu_memory.store import Store
from tianshu_memory.workflow import LocalWorkflow


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_revision_invalidates_whole_group_projection_old_index_cache_and_job(h, kind):
    seeded, job, event = h.seed(
        [
            h.draft(units=[h.unit(), h.unit(statement="另一完整条件")]),
            h.draft(scope=h.group),
        ]
    )
    cached = h.select()
    group_cache = h.select(scope=h.group)
    other = h.event([h.source()], turn="delayed-turn", event_id="delayed-event")
    delayed_job = h.post("memory/turn-commits", other).json()["candidate_job_ref"]
    request = h.revision(seeded["record_ids"][0], kind)
    revised = h.post("memory/revise", request)
    assert revised.status_code == 200, revised.text
    assert revised.json()["semantic_state"] == "invalidated"
    assert revised.json()["index_state"] == "pending"
    assert revised.json()["record_version"] == 2
    assert h.select()["selected_units"] == [] and h.select(scope=h.group)["selected_units"] == []
    assert (
        h.post("memory/select", h.selection(known=cached["scope_version"], budget=0)).json()["code"]
        == "scope_changed"
    )
    assert (
        h.post(
            "memory/select", h.selection(scope=h.group, known=group_cache["scope_version"])
        ).json()["code"]
        == "scope_changed"
    )
    with h.store.transaction() as db:
        # Deliberately leave stale index and pending outbox undelivered.
        assert db.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM records WHERE version=2").fetchone()[0] == 3
        assert (
            db.execute("SELECT COUNT(*) FROM projections WHERE state='active'").fetchone()[0] == 0
        )
    with pytest.raises(Fault) as failure:
        h.workflow.commit_candidate(delayed_job, [h.draft()])
    assert failure.value.code == "scope_changed"
    # Retry is the original semantic result, even though confirmation is consumed and source revoked.
    retry = copy.deepcopy(request)
    retry["command"]["request_id"] = "retry-revision"
    assert h.post("memory/revise", retry).json() == dict(
        revised.json(), request_id="retry-revision"
    )
    h.workflow.rebuild_index()
    assert h.select()["selected_units"] == []
    later = h.event([h.source()], turn="later-turn", event_id="later-event")
    later["scope_version"] = revised.json()["scope_version"]
    receipt = h.post("memory/turn-commits", later).json()
    assert receipt["state"] == "stale_source" and receipt["candidate_job_ref"] is None
    # Replay of original completed job only returns ledger result; no new writes or resurrection.
    original_drafts = [
        h.draft(units=[h.unit(), h.unit(statement="另一完整条件")]),
        h.draft(scope=h.group),
    ]
    assert h.workflow.commit_candidate(job, original_drafts)["record_ids"] == seeded["record_ids"]
    assert h.select()["selected_units"] == []


def test_revision_needs_exact_confirmation_and_version(h):
    seeded, _, _ = h.seed()
    request = h.revision(seeded["record_ids"][0])
    changed = copy.deepcopy(request)
    changed["replacement_statement"] = "被模型篡改的确认"
    assert h.post("memory/revise", changed).status_code == 403
    changed = copy.deepcopy(request)
    changed["confirmation_ref"] = "invented"
    assert h.post("memory/revise", changed).status_code == 403
    changed = copy.deepcopy(request)
    changed["expected_version"] = 2
    assert h.post("memory/revise", changed).json()["code"] == "version_conflict"
    assert len(h.select()["selected_units"]) == 1


def test_revision_transaction_failure_does_not_consume_confirmation_or_change_authority(
    h, monkeypatch
):
    seeded, _, _ = h.seed()
    request = h.revision(seeded["record_ids"][0])
    before = h.select()

    def fail(*args):
        raise sqlite3.OperationalError("synthetic outbox failure")

    with monkeypatch.context() as patch:
        patch.setattr(h.service, "_emit", fail)
        assert h.post("memory/revise", request).status_code == 503
    after = h.select()
    assert (
        after["scope_version"] == before["scope_version"]
        and after["selected_units"] == before["selected_units"]
    )
    assert h.post("memory/revise", request).status_code == 200


def test_event_replay_turn_dedupe_conflict_and_no_auto_memory(h):
    job, event = h.candidate()
    assert h.select()["selected_units"] == []
    replay = h.post("memory/turn-commits", event).json()
    assert replay["state"] == "duplicate" and replay["candidate_job_ref"] == job
    changed_id = dict(event, event_id="another-event")
    assert h.post("memory/turn-commits", changed_id).json()["state"] == "duplicate"
    conflict = dict(event, reality="fictional")
    assert h.post("memory/turn-commits", conflict).json()["code"] == "idempotency_conflict"
    model_event = dict(event, event_type="model.completed")
    assert h.post("memory/turn-commits", model_event).status_code == 400
    model_confirmed = dict(event, confirmed_user_correction=True)
    assert h.post("memory/turn-commits", model_confirmed).status_code == 400
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0] == 1


def test_event_current_sources_unknown_scope_owner_and_expired_session(h):
    event = h.event([h.source()])
    assert h.post("memory/turn-commits", event).status_code == 503
    h.workflow.observe_source(h.source(), h.private)
    # Durable background events do not require an expired short-lived chat origin.
    h.config["origins"] = {}
    h.save_config()
    response = h.post("memory/turn-commits", event)
    assert response.status_code == 200 and response.json()["state"] == "accepted"
    denied = copy.deepcopy(event)
    denied["event_id"] = "denied-event"
    denied["scope"]["person_id"] = "another-person"
    assert h.post("memory/turn-commits", denied).status_code == 403
    denied["owner"] = "model"
    assert h.post("memory/turn-commits", denied).status_code == 400
    h.config["callers"]["companion"]["event_scopes"] = []
    h.save_config()
    assert h.post("memory/turn-commits", event).status_code == 403


def test_source_retraction_old_candidate_and_scope_changed_receipts(h):
    job, event = h.candidate()
    h.workflow.observe_source(h.source(), h.private, state="withdrawn")
    with pytest.raises(Fault) as error:
        h.workflow.commit_candidate(job, [h.draft()])
    assert error.value.code == "version_conflict"
    with pytest.raises(Fault):
        h.workflow.observe_source(h.source(), h.private, state="active")
    stale = dict(event, event_id="late-event", aggregate_id="late-turn")
    assert h.post("memory/turn-commits", stale).json()["state"] == "stale_source"
    stale.update(event_id="scope-event", aggregate_id="scope-turn", scope_version=50)
    assert h.post("memory/turn-commits", stale).json()["state"] == "scope_changed"


def test_relationship_write_ledger_dedup_restart_outbox_and_no_recount(h):
    draft = h.draft(category="relationship", relationship_delta=3)
    seeded, job, event = h.seed([draft])
    assert h.workflow.relationship_value(h.private) == 3
    assert h.workflow.commit_candidate(job, [draft]) == seeded
    with pytest.raises(Fault) as error:
        h.workflow.commit_candidate(job, [dict(draft, relationship_delta=4)])
    assert error.value.code == "idempotency_conflict"
    # Same source in another turn must not increment relationship again.
    another = h.event([h.source()], turn="turn-new", event_id="event-new")
    second = h.post("memory/turn-commits", another).json()["candidate_job_ref"]
    assert h.workflow.commit_candidate(second, [draft])["state"] == "duplicate_source"
    restarted = MemoryService(
        Store(h.store.path), h.contracts, clock=h.clock, source_authority=LocalFixtureSources()
    )
    worker = LocalWorkflow(restarted)
    assert worker.relationship_value(h.private) == 3
    assert (
        restarted.resolve(
            {"query": h.query(), "account": h.account}, h.config["origins"]["origin-private"]
        )["person_id"]
        == h.person
    )
    pending = worker.outbox()
    assert pending and pending == worker.outbox()
    worker.acknowledge(pending[0]["event_id"])
    assert pending[0]["event_id"] not in {x["event_id"] for x in worker.outbox()}
    # Late delivery update has no second candidate or memory accounting.
    late = dict(event, event_id="late-delivery", aggregate_version=4, delivery_state="partial")
    assert h.post("memory/turn-commits", late).json()["candidate_job_ref"] == job
    assert worker.relationship_value(h.private) == 3


def test_source_version_change_invalidates_records_and_search(h):
    h.seed()
    h.workflow.observe_source(h.source(revision=2), h.private)
    assert h.select()["selected_units"] == []
    with pytest.raises(Fault):
        h.workflow.observe_source(h.source(revision=1), h.private)
    h.workflow.rebuild_index()
    assert h.select()["selected_units"] == []


def test_fictional_source_cannot_be_promoted_to_real_and_zero_write_legal(h):
    job, _ = h.candidate(reality="fictional")
    with pytest.raises(Fault):
        h.workflow.commit_candidate(job, [h.draft()])
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert (
        h.workflow.commit_candidate(job, [h.draft(units=[h.unit(reality="fictional")])])["state"]
        == "committed"
    )
    assert h.select()["selected_units"][0]["reality"] == "fictional"


def test_pending_job_recovers_and_empty_review_can_skip(h):
    job, _ = h.candidate()
    service = MemoryService(
        Store(h.store.path), h.contracts, source_authority=LocalFixtureSources(), clock=h.clock
    )
    workflow = LocalWorkflow(service)
    assert workflow.jobs() == [{"id": job, "state": "pending"}]
    assert workflow.commit_candidate(job, [])["state"] == "skipped"
    assert workflow.jobs() == [{"id": job, "state": "skipped"}]


def test_out_of_order_aggregate_gap_requires_snapshot_and_altered_turn_refused(h):
    _, event = h.candidate()
    gap = dict(event, event_id="gap", aggregate_version=5)
    assert h.post("memory/turn-commits", gap).status_code == 503
    changed = dict(
        event,
        event_id="different-input",
        aggregate_version=4,
        sources=[h.source("another-message")],
    )
    assert h.post("memory/turn-commits", changed).json()["code"] == "idempotency_conflict"


def test_atomic_candidate_rollback_on_second_incomplete_group(h):
    job, _ = h.candidate()
    broken = h.draft(units=[dict(h.unit(), missing="unknown-field")])
    with pytest.raises(Fault):
        h.workflow.commit_candidate(job, [h.draft(), broken])
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    assert h.workflow.commit_candidate(job, [h.draft()])["state"] == "committed"


def test_queue_backpressure_retraction_frees_slot_and_retry_is_accepted(h):
    h.service.max_pending_jobs = 1
    job, _ = h.candidate()
    next_source = h.source("next-source")
    h.workflow.observe_source(next_source, h.private)
    event = h.event([next_source], turn="next-turn", event_id="next-event")
    response = h.post("memory/turn-commits", event)
    assert response.status_code == 429 and response.json()["code"] == "queue_full"
    h.workflow.observe_source(h.source(), h.private, state="withdrawn")
    assert {r["id"]: r["state"] for r in h.workflow.jobs()}[job] == "stale_source"
    accepted = h.post("memory/turn-commits", event)
    assert accepted.status_code == 200 and accepted.json()["state"] == "accepted"


def test_zero_budget_still_checks_scope_identity_and_current_version(h):
    h.seed()
    current = h.select(budget=0, known=1)
    assert current["selected_units"] == [] and current["budget_used"] == {"tokens": 0, "bytes": 0}
    request = h.selection(budget=0)
    request["requested_scope"]["person_id"] = "somebody-else"
    assert h.post("memory/select", request).status_code == 403
    h.config["origins"]["origin-private"]["revoked"] = True
    h.save_config()
    assert h.post("memory/select", h.selection(budget=0)).status_code == 403


def test_hidden_revision_uniform_not_found(h):
    seeded, _, _ = h.seed()
    request = h.revision(seeded["record_ids"][0])
    request["command"]["origin"]["assertion_ref"] = "origin-group"
    assert h.post("memory/revise", request).status_code == 404
    request["record_id"] = "record-does-not-exist"
    assert h.post("memory/revise", request).status_code == 404


def test_mixed_reality_preserved_per_source_and_group(h):
    real, fictional = h.source("real-source"), h.source("fictional-source")
    h.workflow.observe_source(real, h.private, reality="real")
    h.workflow.observe_source(fictional, h.private, reality="fictional")
    event = h.event([real, fictional], reality="mixed")
    job = h.post("memory/turn-commits", event).json()["candidate_job_ref"]
    drafts = [
        h.draft(units=[h.unit(real)]),
        h.draft(units=[h.unit(fictional, reality="fictional")]),
    ]
    h.workflow.commit_candidate(job, drafts)
    assert {u["reality"] for u in h.select()["selected_units"]} == {"real", "fictional"}


def test_tombstone_cannot_be_corrected_even_with_fresh_evidence_and_confirmation(h):
    seeded, _, _ = h.seed()
    forgotten_request = h.revision(seeded["record_ids"][0], "forget")
    forgotten = h.post("memory/revise", forgotten_request)
    assert forgotten.status_code == 200 and forgotten.json()["authoritative_state"] == "tombstoned"
    evidence = h.source("fresh-correction-evidence")
    h.workflow.observe_source(evidence, h.private)
    correction = dict(
        forgotten_request,
        command=h.command(),
        expected_version=2,
        revision_kind="correct",
        confirmation_ref="fresh-correction-confirmation",
        evidence_refs=[evidence],
        replacement_statement="新的咖啡表述",
    )
    h.workflow.confirm_revision(correction, h.account, h.private, "2027-01-01T00:00:00Z")
    with h.store.transaction() as db:
        before = tuple(
            db.execute(
                "SELECT version,state,payload FROM records WHERE id=?", (seeded["record_ids"][0],)
            ).fetchone()
        )
        history_count = db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    rejected = h.post("memory/revise", correction)
    assert rejected.status_code == 400 and rejected.json()["code"] == "invalid_input"
    with h.store.transaction() as db:
        assert (
            tuple(
                db.execute(
                    "SELECT version,state,payload FROM records WHERE id=?",
                    (seeded["record_ids"][0],),
                ).fetchone()
            )
            == before
        )
        assert db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == history_count
        assert (
            db.execute(
                "SELECT consumed FROM confirmations WHERE ref=?", (correction["confirmation_ref"],)
            ).fetchone()[0]
            == 0
        )
    assert h.post("memory/revise", forgotten_request).json() == forgotten.json()
    assert h.select()["selected_units"] == []
