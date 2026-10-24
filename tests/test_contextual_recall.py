"""Positive contextual recall through the existing HTTP/source/storage harness."""

import copy
import hashlib
import hmac
from datetime import timedelta

import pytest
from test_consolidation import pending, prepare, submit, window, withdraw_source
from test_group_context import owner
from test_memory_context import ProofIssuer
from test_memory_preparation import checks, payload
from test_memory_v2 import candidate, commit, fact, post, request, setup
from test_memory_v2_boundaries import attach, verify
from test_memory_v2_domains import private_conversation
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, parse_time
from tianshu_memory.memory_reindex import MemoryReindexer


def contextual_query(h, text, *, mode="context", intent="current", **changes):
    body = request(h, text)
    if mode is not None:
        body["retrieval_mode"] = mode
    body["queries"][0]["intent"] = intent
    body.update(changes)
    response = post(h, "query", body)
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-context-v2#query_response", response.json())
    return response.json()


def experience(h, text="曾经一起去河边散步。", serial=1, *, importance="normal"):
    job = candidate(h, text, serial, capture=False)[0]
    done, _ = submit(h, pending(h, [job], window(h, [job])), text, importance=importance)
    return done


@pytest.mark.parametrize("populated", [False, True])
def test_context_returns_positive_material_without_a_name_or_absence_claim(sync, populated):
    prepare(sync)
    if populated:
        experience(sync, "林叶说周末喜欢在河边散步。")
    result = contextual_query(sync, "林叶家之前提过什么河边散步经历？")
    assert bool(result["evidence"]) == populated
    assert result["bundle"]["retrieval_mode"] == "context"
    assert "context_only" in result["omissions"]
    for found, constraint in zip(
        result["query_results"], result["bundle"]["selection_constraints"], strict=True
    ):
        assert found["status"] == constraint["result_status"] == "incomplete"
        assert "context_only" in found["omissions"] and "no_match" not in found["omissions"]
        assert found["coverage"]["candidate_complete"] is False
        assert constraint["candidate_domain_complete"] is False
        assert constraint["proof_kinds"] == ["positive_sources"]
        assert constraint["owner_resolution"] == dict(mode="unresolved", targets=[])
    assert all(item["owner_scope"] == sync.scope() for item in result["evidence"])
    assert verify(sync, result["bundle"]).status_code == 200


def test_factual_default_keeps_the_original_selection_signature(sync):
    setup(sync)
    job, _ = candidate(sync, "我喜欢咖啡。")
    commit(sync, job, "我喜欢咖啡。", fact("咖啡"))
    result = contextual_query(sync, "我喜欢什么咖啡", mode=None)
    bundle = result["bundle"]
    assert "retrieval_mode" not in bundle
    # This is the pre-context wire signature, including scope/history state.
    original_fresh = {"bundle_id", "verified_at", "valid_until", "owner_heads", "source_revision"}
    signed = {key: value for key, value in bundle.items() if key not in original_fresh}
    with sync.store.transaction() as db:
        key = db.execute(
            "SELECT value FROM metadata WHERE key='memory_selection_proof_key'"
        ).fetchone()[0]
    expected = (
        "bundle:"
        + hmac.new(
            bytes.fromhex(key),
            b"memory-context/v2/selection\0" + canonical(signed).encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    assert bundle["bundle_id"] == expected
    assert "latest_values" in bundle["selection_constraints"][0]["proof_kinds"]
    assert verify(sync, bundle).status_code == 200
    explicit = contextual_query(sync, "我喜欢什么咖啡", mode="factual")
    assert explicit["bundle"]["retrieval_mode"] == "factual"
    assert explicit["evidence"] == result["evidence"]
    assert verify(sync, explicit["bundle"]).status_code == 200


@pytest.mark.parametrize("mode", [None, "context"], ids=["factual", "context"])
@pytest.mark.parametrize("author", ["same", "other"])
def test_append_changes_factual_domain_but_not_the_selected_context(sync, mode, author):
    if author == "other" and sync.scope()["audience"] != "group":
        pytest.skip("An unrelated original author is visible only in the group fixture")
    prepare(sync)
    experience(sync)
    selected = contextual_query(sync, "群里之前说过什么河边散步经历？", mode=mode)
    assert selected["evidence"]
    actor = owner(sync) if author == "other" else 0
    new = candidate(sync, "今天去图书馆阅读。", 2, actor, capture=False)[0]
    if author == "same":
        submit(sync, pending(sync, [new], window(sync, [new])), "今天去图书馆阅读。")
    else:
        # Capturing another author's episode does not grant their private scope.
        from test_candidate_extraction import PATH

        response = sync.post(PATH, dict(job_id=new, output=None, supported_extraction_schemas=[4]))
        assert response.status_code == 200, response.text
    verified = verify(sync, selected["bundle"])
    if mode is None and author == "other":
        # This natural wording has an unresolved named reference under the
        # existing factual parser. Its proof covers that domain, not the room.
        constraint = selected["bundle"]["selection_constraints"][0]
        assert constraint["owner_resolution"]["mode"] != "room"
        assert "unique_owner" not in constraint["proof_kinds"]
        assert all(
            item["owner_scope"] == sync.scope()
            for item in selected["bundle"]["source_dependencies"]
        )
    expected = 200 if mode == "context" or author == "other" else 409
    assert verified.status_code == expected, verified.text
    if mode == "context":
        fresh = verified.json()["bundle"]
        assert fresh["bundle_id"] == selected["bundle"]["bundle_id"]
        assert fresh["source_revision"] > selected["bundle"]["source_revision"]
        for key in (
            "evidence_digest",
            "group_dependencies",
            "episode_dependencies",
            "source_dependencies",
        ):
            assert fresh[key] == selected["bundle"][key]


@pytest.mark.parametrize("mode", [None, "context"], ids=["factual", "context"])
def test_explicit_room_append_changes_factual_but_not_selected_context(sync, mode):
    if sync.scope()["audience"] != "group":
        pytest.skip("Room candidate changes require the group fixture")
    prepare(sync)
    experience(sync, "我喜欢咖啡，也曾去河边散步。")
    selected = contextual_query(sync, "群里谁喜欢咖啡", mode=mode)
    assert selected["evidence"]
    if mode is None:
        constraint = selected["bundle"]["selection_constraints"][0]
        assert constraint["owner_resolution"]["mode"] == "room"
        assert constraint["candidate_domain_complete"] is True
    from test_candidate_extraction import PATH

    new = candidate(sync, "我也喜欢咖啡。", 2, owner(sync), capture=False)[0]
    response = sync.post(PATH, dict(job_id=new, output=None, supported_extraction_schemas=[4]))
    assert response.status_code == 200, response.text
    verified = verify(sync, selected["bundle"])
    assert verified.status_code == (200 if mode == "context" else 409), verified.text
    if mode == "context":
        assert verified.json()["bundle"]["bundle_id"] == selected["bundle"]["bundle_id"]


@pytest.mark.parametrize("change", ["withdraw", "forget", "correct"])
def test_selected_context_source_or_record_change_still_rejects(sync, change):
    prepare(sync)
    saved = experience(sync)
    selected = contextual_query(sync, "河边散步")
    if change == "withdraw":
        with sync.store.transaction() as db:
            job = db.execute("SELECT job_id FROM memory_batches").fetchone()[0]
        withdraw_source(sync, job)
    else:
        revision = sync.revision(saved["result"]["record_ids"][0], kind=change)
        assert sync.post("memory/revise", revision).status_code == 200
    assert verify(sync, selected["bundle"]).status_code == 409


def test_current_observations_are_rebuilt_and_never_grant_a_scope(sync):
    prepare(sync)
    experience(sync)
    original = contextual_query(sync, "河边散步")["bundle"]
    old = copy.deepcopy(original)
    old["scope_version"] += 100
    old["association_version"] += 100
    old["scope_checks"][0]["scope_version"] += 100
    old["history_snapshot"]["history_checks"][0]["history_version"] += 100
    old["input_history_snapshot"]["history_checks"][0]["history_version"] += 100
    response = verify(sync, old)
    assert response.status_code == 200, response.text
    fresh = response.json()["bundle"]
    assert fresh["bundle_id"] == original["bundle_id"]
    for key in (
        "scope_version",
        "association_version",
        "scope_checks",
        "history_snapshot",
        "input_history_snapshot",
    ):
        assert fresh[key] == original[key]


@pytest.mark.parametrize("tamper", ["mode", "evidence", "source", "record", "constraint", "query"])
def test_context_signature_still_binds_all_selected_material_and_meaning(sync, tamper):
    prepare(sync)
    experience(sync)
    old = copy.deepcopy(contextual_query(sync, "河边散步")["bundle"])
    if tamper == "mode":
        old["retrieval_mode"] = "factual"
    elif tamper == "evidence":
        old["evidence_digest"] = "a" * 64
    elif tamper == "source":
        old["source_dependencies"][0]["owner_scope"]["person_id"] = "person:unrelated"
    elif tamper == "record":
        old["group_dependencies"][0]["records"][0]["record_version"] += 1
    elif tamper == "constraint":
        old["selection_constraints"][0]["proof_kinds"].append("latest_values")
    else:
        old["queries"][0]["text"] = "另一段完全不同的经历"
    assert verify(sync, old).status_code == 409


@pytest.mark.parametrize("mode", ["context", "factual"])
def test_known_bundle_cannot_change_its_reading_mode(sync, mode):
    prepare(sync)
    experience(sync)
    selected = contextual_query(sync, "河边散步", mode=mode)
    body = request(sync, "河边散步")
    body.update(
        retrieval_mode="factual" if mode == "context" else "context",
        known_bundle=selected["bundle"],
    )
    assert post(sync, "query", body).status_code == 400


def test_selected_private_association_remains_required_and_unlinked_scope_stays_hidden(sync):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Explicit private associations are not group authorization")
    prepare(sync)
    linked = private_conversation(sync, "private:context-linked", same_person=False)
    hidden = private_conversation(sync, "private:context-hidden", same_person=True)
    for serial, actor in enumerate((linked, hidden), 1):
        job, _ = candidate(sync, "我喜欢咖啡。", serial, actor)
        commit(sync, job, "我喜欢咖啡。", fact("咖啡"))
    sync.config["callers"]["companion"]["operations"].append("context_association")
    sync.save()
    proof = ProofIssuer()
    sync.service.memory_context.proofs = proof
    link = dict(
        command=sync.command(),
        action="link",
        source_account=sync.account,
        target_account=sync.physicals[linked]["author"],
        association_id=None,
        expected_version=0,
        scopes=[sync.scope(), sync.scope(linked)],
        proof_ref="context:association",
    )
    proof.issue(
        link, "association", [link["source_account"], link["target_account"]], link["scopes"]
    )
    response = sync.post("memory/context/association", link)
    assert response.status_code == 200, response.text
    result = contextual_query(sync, "咖啡", include_associated=True)
    assert result["evidence"]
    assert {canonical(item["owner_scope"]) for item in result["evidence"]} == {
        canonical(sync.scope(linked))
    }
    revoke = dict(
        command=sync.command(),
        action="revoke",
        source_account=sync.account,
        target_account=None,
        association_id=response.json()["association_id"],
        expected_version=1,
        scopes=[],
        proof_ref=None,
    )
    assert sync.post("memory/context/association", revoke).status_code == 200
    assert verify(sync, result["bundle"]).status_code == 409


@pytest.mark.parametrize("kind", ["embedding", "rerank"])
@pytest.mark.parametrize("change", ["append", "withdraw"])
def test_context_external_ranking_uses_only_unchanged_fresh_documents(sync, kind, change):
    setup(sync)
    job, _ = candidate(sync, "我喜欢咖啡。")
    commit(sync, job, "我喜欢咖啡。", fact("咖啡"))
    backend = attach(sync, "hybrid" if kind == "embedding" else "keyword", kind == "rerank")
    if kind == "embedding":
        assert MemoryReindexer(sync.service).index_step()["indexed"] == 2
    with sync.store.transaction() as db:
        original_documents = {row[0] for row in db.execute("SELECT id FROM memory_documents")}

    def during_ranking():
        if change == "withdraw":
            sync.grants[-1]["state"] = "denied"
            sync.platform_head["sequence"] += 1
        else:
            candidate(sync, "刚刚又说了另一条咖啡经历。", 2)

    backend.hook = during_ranking
    backend.calls.clear()
    result = contextual_query(sync, "我喜欢什么咖啡")
    assert any(op == ("embed" if kind == "embedding" else "rerank") for op, _ in backend.calls)
    if change == "withdraw":
        assert result["evidence"] == [] and "stale_source" in result["omissions"]
    else:
        assert result["evidence"]
        for item in result["evidence"]:
            identity = item.get("episode_id") or "fact:" + item["unit"]["semantic_group_id"]
            assert identity in original_documents
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("mode", [None, "context"], ids=["factual", "context"])
@pytest.mark.parametrize("intent", ["current", "history", "mixed"])
@pytest.mark.parametrize("kind", ["embedding", "rerank"])
def test_external_ranking_rechecks_expiry_through_the_same_eligibility(sync, mode, intent, kind):
    prepare(sync)
    experience(sync, "河边散步时聊起咖啡。", importance="low")
    with sync.store.transaction() as db:
        expiry = parse_time(db.execute("SELECT expires_at FROM memory_summaries").fetchone()[0])
    backend = attach(sync, "hybrid" if kind == "embedding" else "keyword", kind == "rerank")
    if kind == "embedding":
        assert MemoryReindexer(sync.service).index_step()["indexed"] == 2
    crossed = []

    def during_ranking():
        crossed.append(True)
        sync.service.clock = lambda: expiry + timedelta(seconds=1)

    backend.hook = during_ranking
    backend.calls.clear()
    result = contextual_query(sync, "河边散步咖啡", mode=mode, intent=intent)
    assert crossed == [True]
    operation = "embed" if kind == "embedding" else "rerank"
    assert sum(op == operation for op, _ in backend.calls) == 1
    if intent == "current":
        assert result["evidence"] == [] and "stale_source" in result["omissions"]
    else:
        assert {item["kind"] for item in result["evidence"]} >= {"summary", "episode"}
    assert verify(sync, result["bundle"]).status_code == 200
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0


def test_context_prepare_keeps_selected_checks_and_optional_required_classification(sync):
    prepare(sync)
    experience(sync)
    bundle = contextual_query(sync, "河边散步")["bundle"]
    catalog = checks(sync, bundle)
    experience(sync, "今天去图书馆阅读。", 2)
    optional = post(sync, "prepare", payload(sync, bundle, catalog))
    assert optional.status_code == 200 and optional.json()["state"] == "valid", optional.text
    assert optional.json()["bundle"]["bundle_id"] == bundle["bundle_id"]
    assert any(item["status"] == "rejected" for item in optional.json()["checks"])
    required = post(sync, "prepare", payload(sync, bundle, catalog, [catalog[0]["check_id"]]))
    assert required.status_code == 200 and required.json()["state"] == "invalid", required.text
    assert required.json()["component"] == "required_check"


def test_context_history_restores_natural_expiry_but_never_forgotten_sources(sync):
    prepare(sync)
    saved = experience(sync, importance="low")
    old = contextual_query(sync, "河边散步")["bundle"]
    with sync.store.transaction() as db:
        expiry = db.execute("SELECT expires_at FROM memory_summaries").fetchone()[0]
    sync.service.clock = lambda: parse_time(expiry) + timedelta(seconds=1)
    assert verify(sync, old).status_code == 409
    assert contextual_query(sync, "河边散步")["evidence"] == []
    history = contextual_query(sync, "河边散步", intent="history")
    assert {item["kind"] for item in history["evidence"]} >= {"summary", "episode"}
    assert (
        sync.post("memory/revise", sync.revision(saved["result"]["record_ids"][0])).status_code
        == 200
    )
    assert verify(sync, history["bundle"]).status_code == 409
    assert contextual_query(sync, "河边散步", intent="mixed")["evidence"] == []
