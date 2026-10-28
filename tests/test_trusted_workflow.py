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


@pytest.mark.parametrize("adapter", [None, object(), SimpleNamespace(verify_approval=True)])
def test_confirmation_unavailable_without_callable_real_adapter(trusted, adapter):
    workflow = TrustedWorkflow(trusted.h.service, confirmation_adapter=adapter)
    with pytest.raises(Fault) as error:
        workflow.confirm_revision(trusted.input)
    assert (error.value.code, error.value.status) == ("dependency_unavailable", 503)
    assert trusted.calls == []
    with trusted.h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


@pytest.mark.parametrize("decision", [False, True, 1, "confirmed", {"confirmed": True}])
def test_confirmation_never_treats_truthy_payload_as_approval(trusted, decision):
    adapter = SimpleNamespace(verify_approval=lambda input: decision)
    with pytest.raises(Fault) as error:
        TrustedWorkflow(trusted.h.service, adapter).confirm_revision(trusted.input)
    assert error.value.status == 503
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
    workflow = TrustedWorkflow(trusted.h.service)
    proof = workflow._register_confirmation(trusted.input)
    request = trusted.input["request"]
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
    assert workflow._register_confirmation(trusted.input) == dict(proof, consumed=True)
    altered = copy.deepcopy(trusted.input)
    altered["request"]["replacement_statement"] = "different semantic approval"
    with pytest.raises(Fault) as error:
        workflow._register_confirmation(altered)
    assert error.value.code == "idempotency_conflict"
    with trusted.h.store.transaction() as db:
        row = db.execute("SELECT * FROM confirmations").fetchone()
        assert row["consumed"] == 1 and row["digest"] == proof["semantic_digest"]
        assert row["account_key"] == canonical(trusted.h.account)
        assert json.loads(row["scope"]) == trusted.h.private


@pytest.mark.parametrize("change", ["binding", "record"])
def test_confirmation_rechecks_changes_committed_during_approval(trusted, change):
    with trusted.h.store.transaction() as db:
        if change == "binding":
            db.execute("UPDATE accounts SET version=version+1")
        else:
            db.execute("UPDATE records SET version=version+1")
    with pytest.raises(Fault) as error:
        TrustedWorkflow(trusted.h.service)._register_confirmation(trusted.input)
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
        TrustedWorkflow(trusted.h.service)._register_confirmation(input)
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
