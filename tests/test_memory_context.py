"""Synthetic user/source proofs through the real service and guarded SQLite authority."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tianshu_memory.app import create_app
from tianshu_memory.contracts import Contracts
from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.memory_context import MemoryContext
from tianshu_memory.memory_context.migration import semantic_payload
from tianshu_memory.store import Store


class ProofIssuer:
    """Test-only, exact payload attestations; never installed in a production composition."""

    def __init__(self):
        self.issued = {}

    def issue(self, request, purpose, accounts, scopes):
        self.issued[request["proof_ref"]] = (
            fingerprint(semantic_payload(request)),
            purpose,
            accounts,
            scopes,
        )

    def verify(self, request, context, purpose):
        expected = self.issued.get(request["proof_ref"])
        if expected is None or expected[:2] != (fingerprint(semantic_payload(request)), purpose):
            raise Fault("forbidden", 403)
        return dict(
            schema_version=1,
            request_id=request["command"]["request_id"],
            proof_ref=request["proof_ref"],
            purpose=purpose,
            operation_digest=expected[0],
            valid=True,
            principal_id="synthetic-user",
            accounts=expected[2],
            scopes=expected[3],
            expires_at="2030-01-01T00:00:00Z",
        )


@pytest.fixture
def continuity(h):
    contracts = Contracts(h.contracts.directory)
    contracts.load_context()
    h.contracts = h.service.contracts = h.auth.contracts = contracts
    h.store.migrate_profiles(h.directory / "pre-profile.sqlite")
    h.store.migrate_sources(h.directory / "pre-source.sqlite", contracts)
    h.store.migrate_context(h.directory / "pre-context.sqlite")
    h.config["callers"]["companion"]["operations"] += [
        "context_" + name for name in ("query", "propose", "receipt", "batch", "association")
    ]
    h.save_config()
    issuer = ProofIssuer()
    application = MemoryContext(h.service, proofs=issuer)
    h.client.close()
    h.client = TestClient(create_app(service=h.service, auth=h.auth, memory_context=application))
    h.context_application, h.proofs = application, issuer
    return h


def proposal(h, *, key=None, source=None, kind="upsert", item="item:1", field="coffee"):
    source = source or h.source()
    h.workflow.observe_source(source, h.private)
    return dict(
        command=h.command(key=key),
        scope=h.private,
        batch_ref="batch:1",
        item_id=item,
        kind=kind,
        target=dict(
            category="evidence",
            field_key=field,
            item_key=None,
            record_id=None,
            expected_version=None,
        ),
        units=[h.unit(source=source)],
        evidence_refs=[source],
        proof_ref=None,
    )


def query_request(h, **kwargs):
    result = dict(
        h.selection(),
        include_associated=False,
        time_range=None,
        limit=32,
        known_association_version=None,
        known_scope_checks=None,
    )
    result.update(kwargs)
    return result


def post_ok(h, path, request):
    response = h.post("memory/context/" + path, request)
    assert response.status_code == 200, response.text
    return response.json()


def test_natural_recall_all_categories_preserves_whole_group_and_budget(continuity):
    h = continuity
    request = proposal(h)
    request["target"]["category"] = "style"
    request["units"].append(h.unit(statement="偶尔也喝茶", conditions=["休息日"], negations=[]))
    receipt = post_ok(h, "propose", request)
    query = query_request(h)
    query["selection"] = ["style", "evidence"]
    result = post_ok(h, "query", query)
    assert {u["record_id"] for u in result["selected_units"]} == set(receipt["record_ids"])
    assert len(result["selected_units"]) == 2
    assert any(u["negations"] for u in result["selected_units"])
    assert result["coverage"]["history_complete"] is False
    assert result["coverage"]["complete"] is True
    query["budget"] = {"tokens": 400, "bytes": 400}
    limited = post_ok(h, "query", query)
    assert not limited["selected_units"] and limited["omissions"] == ["budget"]


def test_query_is_bounded_before_loading_whole_groups(continuity):
    h = continuity
    for index in range(6):
        post_ok(
            h,
            "propose",
            proposal(
                h,
                source=h.source("source-" + str(index)),
                item="item:" + str(index),
                field="coffee:" + str(index),
            ),
        )
    result = post_ok(h, "query", query_request(h, limit=2))
    assert len(result["dependency_groups"]) == 2
    assert result["coverage"]["complete"] is False


def test_ack_loss_replays_and_receipt_batch_resume_without_duplicate_writes(continuity):
    h = continuity
    request = proposal(h, key="stable-operation")
    first = post_ok(h, "propose", request)
    request["command"] = h.command(key="stable-operation")
    replay = post_ok(h, "propose", request)
    assert replay["record_ids"] == first["record_ids"]
    receipt = post_ok(
        h, "receipt", dict(query=h.query(), scope=h.private, operation_id="stable-operation")
    )
    assert receipt["receipt"] == first
    batch = post_ok(
        h, "batch", dict(query=h.query(), scope=h.private, batch_ref="batch:1", after=None, limit=1)
    )
    assert batch["complete"] and batch["receipts"] == [first]
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    request["units"][0]["statement"] = "different"
    assert h.post("memory/context/propose", request).status_code == 409


def test_one_evidence_can_support_two_targets_without_becoming_two_evidence_sources(continuity):
    h = continuity
    first = proposal(h, field="coffee")
    post_ok(h, "propose", first)
    second = proposal(h, field="sleep", item="item:2")
    second["units"][0]["statement"] = "晚上注意睡眠"
    post_ok(h, "propose", second)
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM context_applications").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(DISTINCT source_key) FROM lineage").fetchone()[0] == 1
    duplicate = post_ok(h, "propose", proposal(h, field="coffee", item="item:3"))
    assert duplicate["state"] == "duplicate"


def test_no_op_has_a_receipt_without_a_fact_or_any_units(continuity):
    h = continuity
    request = proposal(h, kind="no_op")
    request.update(units=[], evidence_refs=[])
    assert post_ok(h, "propose", request)["state"] == "no_op"
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    request["command"] = h.command()
    request["units"] = [h.unit()]
    assert h.post("memory/context/propose", request).status_code == 400


def correction(h, first, *, version=1, kind="correct", source=None):
    request = proposal(
        h, source=source or h.source("correction-message"), kind=kind, item="correction:1"
    )
    request["target"].update(record_id=first["record_ids"][0], expected_version=version)
    request["proof_ref"] = "synthetic-revision-proof:" + str(version)
    request["units"] = (
        []
        if kind == "forget"
        else [
            h.unit(
                source=request["evidence_refs"][0],
                statement="现在只喝茶",
                conditions=["上午"],
                negations=["不再喝咖啡"],
            )
        ]
    )
    h.proofs.issue(request, "revision", [h.account], [h.private])
    return request


def test_correction_and_new_semantic_group_commit_atomically_and_old_reflection_cannot_restore(
    continuity,
):
    h = continuity
    original = proposal(h)
    first = post_ok(h, "propose", original)
    revised = post_ok(h, "propose", correction(h, first))
    assert revised["state"] == "corrected"
    query = query_request(h)
    query["query_text"] = "喝茶"
    result = post_ok(h, "query", query)
    assert {u["record_id"] for u in result["selected_units"]} == set(revised["record_ids"])
    assert result["selected_units"][0]["conditions"] == ["上午"]
    original["command"] = h.command()
    original["item_id"] = "late-reflection"
    late = post_ok(h, "propose", original)
    assert late["state"] == "rejected"
    assert late["error_code"] == "version_conflict"


def test_failed_rebuild_rolls_back_revision_and_consumed_proof(continuity):
    h = continuity
    first = post_ok(h, "propose", proposal(h))
    # Reusing the very source being corrected would recreate withdrawn evidence.
    request = correction(h, first, source=h.source())
    rejected = post_ok(h, "propose", request)
    assert rejected["state"] == "rejected"
    assert (
        post_ok(h, "query", query_request(h))["selected_units"][0]["record_id"]
        == first["record_ids"][0]
    )
    with h.store.transaction() as db:
        assert not db.execute(
            "SELECT 1 FROM confirmations WHERE ref=?", (request["proof_ref"],)
        ).fetchone()


def test_stale_revision_is_a_queryable_item_rejection_and_does_not_abort_other_items(continuity):
    h = continuity
    first = post_ok(h, "propose", proposal(h))
    failed = post_ok(h, "propose", correction(h, first, version=99))
    assert failed["state"] == "rejected" and failed["current_version"] == 1
    other = proposal(h, source=h.source("other-message"), field="reading", item="other-item")
    assert post_ok(h, "propose", other)["state"] == "committed"
    assert (
        post_ok(
            h,
            "receipt",
            dict(query=h.query(), scope=h.private, operation_id=failed["operation_id"]),
        )["receipt"]
        == failed
    )


def test_forget_does_not_turn_into_a_rebuild_or_relationship_delta(continuity):
    h = continuity
    first = post_ok(h, "propose", proposal(h))
    assert post_ok(h, "propose", correction(h, first, kind="forget"))["state"] == "tombstoned"
    assert not post_ok(h, "query", query_request(h))["selected_units"]
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM relationship_entries").fetchone()[0] == 0


def test_plain_model_claim_is_not_revision_permission(continuity):
    h = continuity
    first = post_ok(h, "propose", proposal(h))
    request = correction(h, first)
    h.proofs.issued.clear()
    assert h.post("memory/context/propose", request).status_code == 403
    assert post_ok(h, "query", query_request(h))["selected_units"]


def associated(h):
    other_account = {"namespace": "web", "immutable_account_id": "synthetic-web-user"}
    with h.store.transaction() as db:
        db.execute("INSERT INTO people VALUES ('other-person')")
        db.execute(
            "INSERT INTO accounts VALUES (?,'other-person',1,NULL)", (canonical(other_account),)
        )
    other_scope = dict(h.private, person_id="other-person", conversation_id="other-private")
    other_context = dict(
        h.config["origins"]["origin-private"],
        allowed_scope=other_scope,
        verified_account=other_account,
        assertion_ref="other-origin",
    )
    h.config["origins"]["other-origin"] = other_context
    h.save_config()
    request = dict(
        command=h.command(),
        action="link",
        source_account=h.account,
        target_account=other_account,
        association_id=None,
        expected_version=0,
        scopes=[h.private, other_scope],
        proof_ref="synthetic-association-proof",
    )
    h.proofs.issue(request, "association", [h.account, other_account], [h.private, other_scope])
    return request, other_scope, other_account


def test_association_requires_purpose_bound_both_account_consent_and_keeps_person_ids(continuity):
    h = continuity
    request, other_scope, account = associated(h)
    h.proofs.issued.clear()
    assert h.post("memory/context/association", request).status_code == 403
    h.proofs.issue(request, "association", [h.account, account], [h.private, other_scope])
    receipt = post_ok(h, "association", request)
    assert receipt["source_person_id"] == h.person and receipt["target_person_id"] == "other-person"
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT person_id FROM accounts WHERE account_key=?", (canonical(account),)
            ).fetchone()[0]
            == "other-person"
        )
    request["scopes"][1] = dict(other_scope, audience="group")
    request["command"] = h.command()
    assert h.post("memory/context/association", request).status_code == 403


def test_associated_context_is_scope_bound_revocable_and_send_checks_detect_target_change(
    continuity,
):
    h = continuity
    link, other_scope, _ = associated(h)
    linked = post_ok(h, "association", link)
    source = h.source("other-source")
    h.workflow.observe_source(source, other_scope)
    request = proposal(h, source=h.source("base-source"))
    request.update(
        command=h.command(origin="other-origin"),
        scope=other_scope,
        evidence_refs=[source],
        units=[h.unit(source=source)],
    )
    post_ok(h, "propose", request)
    base = query_request(h, include_associated=True)
    result = post_ok(h, "query", base)
    assert result["selected_units"][0]["subject_person_id"] == "other-person"
    base.update(
        known_scope_checks=result["scope_checks"],
        known_association_version=result["association_version"],
        budget={"tokens": 0, "bytes": 0},
    )
    with h.store.transaction() as db:
        h.service._bump(db, other_scope)
    assert h.post("memory/context/query", base).status_code == 409
    revoke = dict(
        command=h.command(),
        action="revoke",
        source_account=h.account,
        target_account=None,
        association_id=linked["association_id"],
        expected_version=1,
        scopes=[],
        proof_ref=None,
    )
    assert post_ok(h, "association", revoke)["state"] == "revoked"
    assert not post_ok(h, "query", query_request(h, include_associated=True))["selected_units"]


def test_group_does_not_expand_private_associations(continuity):
    h = continuity
    request, _, _ = associated(h)
    post_ok(h, "association", request)
    query = query_request(h, include_associated=True)
    query.update(query=h.query("origin-group"), requested_scope=h.group)
    result = post_ok(h, "query", query)
    assert len(result["scope_checks"]) == 1 and result["scope_checks"][0]["scope"] == h.group


def test_legacy_jobs_can_reuse_source_for_another_target_after_migration(continuity):
    h = continuity
    _, event = h.candidate()
    first_job = h.workflow.jobs()[0]["id"]
    assert h.workflow.commit_candidate(first_job, [h.draft()])["state"] == "committed"
    event.update(event_id="second-event", aggregate_id="second-turn", scope_version=2)
    second = h.post("memory/turn-commits", event).json()
    assert (
        h.workflow.commit_candidate(
            second["candidate_job_ref"], [h.draft(), h.draft(field_key="sleep")]
        )["state"]
        == "committed"
    )
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM groups WHERE field_key='coffee'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM groups WHERE field_key='sleep'").fetchone()[0] == 1


def test_real_nonempty_upgrade_preserves_old_sources_accounts_records_and_checkpoint(h):
    h.store.migrate_profiles(h.directory / "schema1.sqlite")
    first, _, _ = h.seed()
    h.store.migrate_sources(h.directory / "schema2.sqlite", h.contracts)
    result = h.store.migrate_context(h.directory / "schema3.sqlite")
    assert result["mapped_applications"] == 1
    reopened = Store(h.store.path)
    with reopened.transaction() as db:
        assert db.execute("SELECT id FROM records").fetchone()[0] == first["record_ids"][0]
        assert db.execute("SELECT person_id FROM accounts").fetchone()[0] == h.person
        assert db.execute("SELECT COUNT(*) FROM context_applications").fetchone()[0] == 1
    # Restoring only pre-migration DB bytes cannot silently erase the new guarded ledger.
    old = sqlite3.connect(h.directory / "schema3.sqlite")
    newer = sqlite3.connect(h.store.path)
    old.backup(newer)
    old.close()
    newer.close()
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(h.store.path)


def test_schema_and_service_scope_checks_precede_new_content(continuity):
    h = continuity
    request = proposal(h)
    request["scope"] = dict(h.private, person_id="somebody-else")
    assert h.post("memory/context/propose", request).status_code == 403
    request = proposal(h)
    request["units"][0]["unknown"] = "bad"
    assert h.post("memory/context/propose", request).status_code == 400
    assert h.client.post("/internal/v1/memory/context/propose", json=request).status_code == 401


def test_missing_original_time_keeps_candidate_without_claiming_range(continuity):
    h = continuity
    post_ok(h, "propose", proposal(h))
    result = post_ok(
        h,
        "query",
        query_request(h, time_range={"from": "2026-10-03T00:00:00Z", "to": "2026-10-04T00:00:00Z"}),
    )
    assert result["selected_units"]
    assert result["coverage"]["missing_source_times"] == 1
    assert result["coverage"]["complete"] is False
    assert "source_time_unavailable" in result["omissions"]


def test_association_ack_replay_does_not_need_consumed_remote_proof(continuity):
    h = continuity
    request, _, _ = associated(h)
    first = post_ok(h, "association", request)
    h.proofs.issued.clear()
    replay = post_ok(h, "association", request)
    assert replay == first
