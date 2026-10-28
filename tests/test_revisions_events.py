import copy
import sqlite3

import pytest

from tianshu_memory.domain import Fault


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


def test_source_version_change_invalidates_records_and_search(h):
    h.seed()
    h.workflow.observe_source(h.source(revision=2), h.private)
    assert h.select()["selected_units"] == []
    with pytest.raises(Fault):
        h.workflow.observe_source(h.source(revision=1), h.private)
    h.workflow.rebuild_index()
    assert h.select()["selected_units"] == []


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
