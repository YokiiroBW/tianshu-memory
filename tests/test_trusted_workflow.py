"""Workflow boundary tests with isolated SQLite and explicit approval/barrier doubles.

These tests isolate application registration/commit semantics; they are not real user
approval, owner HTTP integration, or a cross-product acceptance claim.
"""

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tianshu_memory.domain import Fault, canonical, fingerprint, semantic_request
from tianshu_memory.workflow import LocalWorkflow, TrustedWorkflow


def test_candidate_limit_rejects_before_any_operation_or_write(h, monkeypatch):
    job, _ = h.candidate()
    drafts = [h.draft(units=[h.unit() for _ in range(32)]) for _ in range(8)]
    drafts.append(h.draft())

    def forbidden_operation(**kwargs):
        pytest.fail("Oversized candidate must fail before the source barrier or business writes")

    monkeypatch.setattr(h.service, "operation", forbidden_operation)
    with pytest.raises(Fault) as error:
        h.workflow.commit_candidate(job, drafts)
    assert (error.value.code, error.value.status) == ("invalid_input", 400)
    with h.store.transaction() as db:
        for table in ("records", "groups", "write_ledger", "source_writes"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0] == "pending"


def test_candidate_limit_accepts_complete_256_records(h):
    job, _ = h.candidate()
    drafts = [h.draft(units=[h.unit() for _ in range(32)]) for _ in range(8)]
    result = h.workflow.commit_candidate(job, drafts)
    assert result["state"] == "committed"
    assert len(result["record_ids"]) == 256
    assert len(result["group_ids"]) == 8
    assert h.workflow.commit_candidate(job, drafts) == result


def test_candidate_reloads_snapshot_after_barrier(h, monkeypatch):
    job, event = h.candidate()

    @contextmanager
    def changed_snapshot(**kwargs):
        assert kwargs == {"scope": h.private, "sources": event["sources"], "event": event}
        # An independently committed source synchronization can change the job's dependency.
        with h.store.transaction() as db:
            db.execute("UPDATE jobs SET source_snapshot='{}' WHERE id=?", (job,))
        with h.store.transaction() as db:
            yield db

    monkeypatch.setattr(h.service, "operation", changed_snapshot)
    with pytest.raises(Fault) as error:
        h.workflow.commit_candidate(job, [h.draft()])
    assert error.value.code == "version_conflict"
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert (
            db.execute("SELECT source_snapshot FROM jobs WHERE id=?", (job,)).fetchone()[0] == "{}"
        )


def test_candidate_rejects_changed_job_event_after_barrier(h, monkeypatch):
    job, event = h.candidate()

    @contextmanager
    def changed_event(**kwargs):
        with h.store.transaction() as db:
            changed = dict(event, scope=dict(h.private, actor_id="unapproved-actor"))
            db.execute("UPDATE jobs SET event=? WHERE id=?", (canonical(changed), job))
        with h.store.transaction() as db:
            yield db

    monkeypatch.setattr(h.service, "operation", changed_event)
    with pytest.raises(Fault) as error:
        h.workflow.commit_candidate(job, [h.draft()])
    assert error.value.code == "version_conflict"
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_trusted_workflow_rejects_fixture_backend(h):
    with pytest.raises(ValueError, match="production SourceAuthority"):
        TrustedWorkflow(h.service)
    assert not issubclass(TrustedWorkflow, LocalWorkflow)


@pytest.fixture
def trusted(h, monkeypatch):
    """Keep the actual workflow/SQLite; replace only the remote synchronization boundary."""
    from tianshu_memory.source_authority import SourceAuthority

    seeded, job, event = h.seed()
    h.contracts.load_sources()
    h.store.migrate_profiles(h.directory / "before-profiles.sqlite")
    h.store.migrate_sources(h.directory / "before-sources.sqlite", h.contracts)
    # Constructor arguments are deployment dependencies, not user payload authority.
    h.service.source_authority = SourceAuthority(None, h.contracts)
    calls = []

    @contextmanager
    def isolated_operation(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        with h.store.transaction() as db:
            yield db

    monkeypatch.setattr(h.service, "operation", isolated_operation)
    context = dict(h.config["origins"]["origin-private"], issuer="platform")
    request = {
        "command": h.command(),
        "record_id": seeded["record_ids"][0],
        "expected_version": 1,
        "revision_kind": "correct",
        "confirmation_ref": "approval:synthetic-user",
        "evidence_refs": [h.source()],
        "replacement_statement": "只在上午喝茶",
    }
    input = {
        "request": request,
        "verified_context": context,
        "binding_version": 1,
        "expires_at": "2027-01-01T00:00:00Z",
    }
    return SimpleNamespace(h=h, input=input, calls=calls, job=job, event=event)


@pytest.mark.parametrize("scope_change", ["audience", "conversation", "actor", "person"])
def test_trusted_candidate_does_not_expand_source_scope(trusted, scope_change):
    scope = copy.deepcopy(trusted.h.private)
    if scope_change == "audience":
        scope = trusted.h.group
    elif scope_change == "conversation":
        scope["conversation_id"] = "unapproved-conversation"
    elif scope_change == "actor":
        scope["actor_id"] = "unapproved-actor"
    else:
        scope["person_id"] = "unapproved-person"
    draft = trusted.h.draft(scope=scope, item_key=None, relationship_delta=None)
    with pytest.raises(Fault) as error:
        TrustedWorkflow(trusted.h.service).commit_candidate(
            {"job_id": trusted.job, "drafts": [draft]}
        )
    assert error.value.status == 403
    assert trusted.calls == [
        {
            "scope": trusted.h.private,
            "sources": trusted.event["sources"],
            "event": trusted.event,
        }
    ]
    with trusted.h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1


@pytest.mark.parametrize("adapter", [None, object(), SimpleNamespace(verify_approval=True)])
def test_confirmation_unavailable_without_callable_real_adapter(trusted, adapter):
    workflow = TrustedWorkflow(trusted.h.service, confirmation_adapter=adapter)
    with pytest.raises(Fault) as error:
        workflow.confirm_revision(trusted.input)
    assert (error.value.code, error.value.status) == ("dependency_unavailable", 503)
    assert trusted.calls == []
    with trusted.h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


@pytest.mark.parametrize("decision", [False, 1, "confirmed", {"confirmed": True}])
def test_confirmation_never_treats_truthy_payload_as_approval(trusted, decision):
    adapter = SimpleNamespace(verify_approval=lambda input: decision)
    with pytest.raises(Fault) as error:
        TrustedWorkflow(trusted.h.service, adapter).confirm_revision(trusted.input)
    assert error.value.status == 403
    assert trusted.calls == []


def test_confirmation_adapter_failure_is_unavailable_without_writes(trusted):
    def unavailable(input):
        raise TimeoutError("synthetic user issuer timeout")

    with pytest.raises(Fault) as error:
        TrustedWorkflow(
            trusted.h.service, SimpleNamespace(verify_approval=unavailable)
        ).confirm_revision(trusted.input)
    assert error.value.status == 503
    with trusted.h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_confirmation_binds_exact_payload_and_never_reissues_consumed_proof(trusted):
    seen = []

    def approve(input):
        seen.append(copy.deepcopy(input))
        input["request"]["replacement_statement"] = "adapter cannot mutate retained input"
        return True

    workflow = TrustedWorkflow(trusted.h.service, SimpleNamespace(verify_approval=approve))
    proof = workflow.confirm_revision(trusted.input)
    request = trusted.input["request"]
    assert seen == [trusted.input]
    assert proof == {
        "confirmation_ref": request["confirmation_ref"],
        "record_id": request["record_id"],
        "semantic_digest": fingerprint(semantic_request(request)),
        "account": trusted.h.account,
        "scope": trusted.h.private,
        "binding_version": 1,
        "expected_version": 1,
        "expires_at": trusted.input["expires_at"],
        "consumed": False,
    }
    assert trusted.calls == [
        {
            "scope": trusted.h.private,
            "sources": request["evidence_refs"],
            "context": trusted.input["verified_context"],
        }
    ]
    with trusted.h.store.transaction() as db:
        db.execute(
            "UPDATE confirmations SET consumed=1 WHERE ref=?", (request["confirmation_ref"],)
        )
    assert workflow.confirm_revision(trusted.input) == dict(proof, consumed=True)
    altered = copy.deepcopy(trusted.input)
    altered["request"]["replacement_statement"] = "different semantic approval"
    with pytest.raises(Fault) as error:
        workflow.confirm_revision(altered)
    assert error.value.code == "idempotency_conflict"
    with trusted.h.store.transaction() as db:
        row = db.execute("SELECT * FROM confirmations").fetchone()
        assert row["consumed"] == 1 and row["digest"] == proof["semantic_digest"]
        assert row["account_key"] == canonical(trusted.h.account)
        assert json.loads(row["scope"]) == trusted.h.private


@pytest.mark.parametrize("change", ["binding", "record"])
def test_confirmation_rechecks_changes_committed_during_approval(trusted, change):
    def approve(input):
        with trusted.h.store.transaction() as db:
            if change == "binding":
                db.execute("UPDATE accounts SET version=version+1")
            else:
                db.execute("UPDATE records SET version=version+1")
        return True

    with pytest.raises(Fault) as error:
        TrustedWorkflow(
            trusted.h.service, SimpleNamespace(verify_approval=approve)
        ).confirm_revision(trusted.input)
    assert error.value.status == (403 if change == "binding" else 409)
    with trusted.h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change,status",
    [
        ("issuer", 403),
        ("caller", 403),
        ("assertion", 403),
        ("scope", 404),
        ("account", 403),
        ("binding", 403),
        ("record", 404),
        ("expected", 409),
        ("expired", 400),
    ],
)
def test_confirmation_rejects_mismatched_authority(trusted, change, status):
    input = copy.deepcopy(trusted.input)
    context = input["verified_context"]
    if change == "issuer":
        context["issuer"] = "nonebot"
    elif change == "caller":
        context["authenticated_service"] = "other-service"
    elif change == "assertion":
        context["assertion_ref"] = "different-origin"
    elif change == "scope":
        context["allowed_scope"]["actor_id"] = "different-actor"
    elif change == "account":
        context["verified_account"]["immutable_account_id"] = "unknown-account"
    elif change == "binding":
        input["binding_version"] = 2
    elif change == "record":
        input["request"]["record_id"] = "unknown-record"
    elif change == "expected":
        input["request"]["expected_version"] = 2
    else:
        input["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(Fault) as error:
        TrustedWorkflow(
            trusted.h.service, SimpleNamespace(verify_approval=lambda input: True)
        ).confirm_revision(input)
    assert error.value.status == status
    with trusted.h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_production_profiles_and_global_rebuild_remain_unavailable(trusted):
    workflow = TrustedWorkflow(trusted.h.service)
    for operation in (
        lambda: workflow.approve_profile({}, {}, "2027-01-01T00:00:00Z"),
        lambda: workflow.publish_profile({}, "invented-approval", {}),
        workflow.rebuild_index,
    ):
        with pytest.raises(Fault) as error:
            operation()
        assert error.value.status == 503
    assert not hasattr(workflow, "observe_source")
