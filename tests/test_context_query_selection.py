"""New context discovery stays local; only returned material obtains fresh proof."""

import copy
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from test_consolidation import pending, prepare, submit, window, withdraw_source
from test_contextual_recall import contextual_query, experience
from test_group_context import owner
from test_memory_v2 import candidate, post, request, setup
from test_memory_v2_boundaries import attach, source_calls, verify
from test_query_vector_boundary import indexed, outside_boundaries
from test_segments import MATERIALS, archive, forget_original, front
from test_segments import ready as segment_ready
from test_selected_source_proofs import snapshot_selectors, summaries
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import consolidation_sources
from tianshu_memory.domain import Fault, canonical, fingerprint, parse_time, source_selector
from tianshu_memory.memory_context import read_v2
from tianshu_memory.memory_reindex import materialize
from tianshu_memory.workflow import TrustedWorkflow


def before_final(monkeypatch, callback):
    """A real write may finish after discovery and before the source checkpoint."""
    original = read_v2.operation
    seen = []

    @contextmanager
    def boundary(service, items):
        if not seen:
            seen.append(copy.deepcopy(items))
            callback(items)
        with original(service, items) as db:
            yield db

    monkeypatch.setattr(read_v2, "operation", boundary)
    return seen


def guard_window(monkeypatch, rejected):
    original = consolidation_sources.window_failures
    checked = []

    def check(service, db, manifests, turns, owners):
        manifests = list(manifests)
        identities = {item["window_id"] for item in manifests}
        checked.append(identities)
        if rejected in identities:
            raise Fault("dependency_unavailable", 503)
        return original(service, db, manifests, turns, owners)

    monkeypatch.setattr(consolidation_sources, "window_failures", check)
    return checked


def test_new_context_query_does_not_prove_an_unreturned_window_or_archive(sync, monkeypatch):
    if sync.scope()["audience"] != "group":
        pytest.skip("An unrelated archive author shares the real group")
    _, _, selected, _, unrelated = summaries(sync)
    segment_ready(sync)
    archive(sync, text="另一个人准备登山。")
    sync.passive_owner.calls.clear()
    sync.passive_owner.read_allowed = False
    checked = guard_window(monkeypatch, unrelated["window_id"])
    sync.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))
    assert result["evidence"] and all(item["kind"] == "summary" for item in result["evidence"])
    assert snapshot_selectors(sync) == {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in selected["members"]
    }
    assert checked == [{selected["window_id"]}]
    assert sync.passive_owner.calls == []
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("entry", ["factual", "known_bundle", "rerank"])
def test_full_query_paths_keep_unrelated_complete_window_proof(sync, monkeypatch, entry):
    bundle, _, _, _, unrelated = summaries(sync)
    checked = guard_window(monkeypatch, unrelated["window_id"])
    body = request(sync, "河边散步")
    if entry != "factual":
        body["retrieval_mode"] = "context"
    if entry == "known_bundle":
        body["known_bundle"] = bundle
    backend = attach(sync, "keyword", True) if entry == "rerank" else None
    response = post(sync, "query", body)
    assert response.status_code == 503 and response.json()["code"] == "dependency_unavailable"
    assert any(unrelated["window_id"] in identities for identities in checked)
    if backend is not None:
        assert all(operation == "settings" for operation, _ in backend.calls)


@pytest.mark.parametrize("change", ["append", "expire", "correct"])
def test_final_query_boundary_never_adds_or_refills_candidates(sync, monkeypatch, change):
    prepare(sync)
    experience(sync, "去河边散步。", 1, importance="low")
    experience(sync, "去河边散步并喝茶。", 2, importance="low")
    selected = []

    def change_after_discovery(items):
        selected.extend(items[0]["selected"]["group_ids"])
        assert len(selected) == 1
        with sync.store.transaction() as db:
            row = db.execute(
                "SELECT r.id,s.expires_at FROM records r JOIN memory_summaries s ON s.group_id=r.group_id WHERE r.group_id=?",
                (selected[0],),
            ).fetchone()
        if change == "append":
            experience(sync, "去河边散步，又见到新朋友。", 3)
        elif change == "expire":
            sync.service.clock = lambda: parse_time(row["expires_at"]) + timedelta(seconds=1)
        else:
            response = sync.post("memory/revise", sync.revision(row["id"], kind="correct"))
            assert response.status_code == 200, response.text
        sync.calls.clear()

    before_final(monkeypatch, change_after_discovery)
    result = contextual_query(sync, "河边散步", limits=dict(facts=1, episodes=0))
    if change == "append":
        assert {item["unit"]["semantic_group_id"] for item in result["evidence"]} == set(selected)
    else:
        assert result["evidence"] == [] and "stale_source" in result["omissions"]
    assert "budget" in result["omissions"]
    assert verify(sync, result["bundle"]).status_code == 200


def test_bounded_vector_context_query_proves_only_the_selected_episode(sync, monkeypatch):
    backend = indexed(sync)
    outside_boundaries(sync, monkeypatch, backend)
    originals = copy.deepcopy(backend.document_batches)
    sync.calls.clear()
    result = contextual_query(sync, "睡前选什么", limits=dict(facts=0, episodes=1))
    assert len(result["evidence"]) == 1 and result["evidence"][0]["kind"] == "episode"
    assert {fingerprint(json.loads(item)) for item in snapshot_selectors(sync)} == {
        item["source_key"] for item in result["bundle"]["source_dependencies"]
    }
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert backend.query_calls == 1 and backend.inputs[0][0] == "睡前选什么"
    assert backend.document_batches == originals and backend.reranks == []
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("empty", [False, True])
def test_empty_or_zero_budget_context_query_still_has_a_fresh_viewer_boundary(sync, empty):
    prepare(sync)
    if not empty:
        experience(sync)
    sync.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=0))
    assert result["evidence"] == [] and result["bundle"]["source_dependencies"] == []
    assert snapshot_selectors(sync) == set()
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert result["query_results"][0]["status"] == "incomplete"
    assert "no_match" not in result["omissions"]
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize(
    "code,status", [("forbidden", 403), ("scope_changed", 409), ("dependency_unavailable", 503)]
)
def test_query_does_not_convert_authority_errors_to_an_empty_success(
    sync, monkeypatch, code, status
):
    prepare(sync)
    experience(sync)

    def reject(items):
        assert items[0]["selected"]["group_ids"]
        raise Fault(code, status)

    before_final(monkeypatch, reject)
    body = dict(request(sync, "河边散步"), retrieval_mode="context")
    response = post(sync, "query", body)
    assert response.status_code == status and response.json()["code"] == code


def test_new_query_proves_a_foreign_context_leaf_and_persists_its_negative(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two actual authors in the same selected window")
    prepare(sync)
    other = owner(sync)
    context = candidate(sync, "约在河边见。", 1, other, capture=False)[0]
    current = candidate(sync, "九点去河边散步。", 2, capture=False)[0]
    supplied = window(sync, [current], context=[context])
    submit(sync, pending(sync, [current], supplied), "本人约九点去河边散步。")
    sync.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))
    assert result["evidence"] and all(
        item["subject_person_id"] == sync.scope()["person_id"] for item in result["evidence"]
    )
    assert snapshot_selectors(sync) == {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in supplied["manifest"]["members"]
    }
    withdraw_source(sync, context)
    fresh = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))
    assert fresh["evidence"] == [] and "stale_source" in fresh["omissions"]
    foreign = next(
        member
        for member in supplied["manifest"]["members"]
        if member["source_scope"] == sync.scope(other)
    )
    key = sync.service.source_identity(foreign["reference"]["source"], foreign["source_scope"])
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0] == "withdrawn"
        )


def test_query_episode_keeps_the_real_core_observation_alias_and_forget_closure(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("An actual observation/Core physical mapping")
    segment_ready(sync)
    material, original = archive(sync, text="一起去河边散步。")
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "一起去河边散步。", "query-alias", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = {
        key: original[key] for key in ("source_ref", "source_digest", "scope_version")
    } | dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        archive_epoch=1,
    )
    accepted = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200 and accepted.json()["materials"] == [material]
    sync.calls.clear()
    sync.passive_owner.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=8))
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["subject_person_id"] == material["person_id"]
    assert snapshot_selectors(sync) == {
        canonical(source_selector(event["sources"][0], event["scope"]))
    }
    assert len(sync.passive_owner.calls) == 2
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])
    forget_original(sync, dict(material, source_key=core_key), actor)
    assert contextual_query(sync, "河边散步")["evidence"] == []
    with sync.store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT source_key FROM suppression")} >= {
            core_key,
            material["source_key"],
        }


@pytest.mark.parametrize("limit,change", [(1, False), (2, False), (2, True)])
def test_query_keeps_whole_groups_and_binds_every_record_version(sync, monkeypatch, limit, change):
    setup(sync)
    job, _ = candidate(sync, "河边散步时上午喝咖啡，晚上不喝咖啡。")
    with sync.store.transaction() as db:
        event = json.loads(db.execute("SELECT event FROM jobs WHERE id=?", (job,)).fetchone()[0])
    draft = copy.deepcopy(sync.examples["memory/candidate-commit"]["drafts"][0])
    draft["scope"] = sync.scope()
    unit = dict(draft["units"][0], sources=event["sources"])
    draft["units"] = [dict(unit, statement=text) for text in ("上午喝咖啡。", "晚上不喝咖啡。")]
    committed = TrustedWorkflow(sync.service).commit_candidate(dict(job_id=job, drafts=[draft]))
    assert len(committed["record_ids"]) == 2
    # The reviewed legacy writer indexes before its lineage is complete. Use
    # the existing verified rebuild to attach the actual source's timestamp.
    assert materialize(sync.service, event["scope"], event["sources"]) == len(event["sources"])
    with sync.store.transaction() as db:
        document = db.execute(
            "SELECT observed_at FROM memory_documents WHERE group_id=?",
            (committed["group_ids"][0],),
        ).fetchone()
        assert document["observed_at"] is not None

    def update_after_discovery(items):
        if not change:
            return
        assert items[0]["selected"]["group_ids"] == committed["group_ids"]
        # The complete group changes without changing its indexed text/hash.
        # Fresh local eligibility still accepts this row, so source-only or
        # first-record comparisons cannot catch this concurrent version change.
        with sync.store.transaction() as db:
            identity = committed["record_ids"][1]
            row = db.execute("SELECT payload FROM records WHERE id=?", (identity,)).fetchone()
            payload = json.loads(row[0])
            payload["record_version"] += 1
            db.execute(
                "UPDATE records SET version=version+1,payload=? WHERE id=?",
                (canonical(payload), identity),
            )

    before_final(monkeypatch, update_after_discovery)
    sync.calls.clear()
    result = contextual_query(sync, "咖啡", limits=dict(facts=limit, episodes=0))
    if limit == 1 or change:
        assert result["evidence"] == []
        assert ("stale_source" if change else "budget") in result["omissions"]
    else:
        assert {item["unit"]["record_id"] for item in result["evidence"]} == set(
            committed["record_ids"]
        )
        assert set(result["bundle"]["group_dependencies"][0]["group"]["record_ids"]) == set(
            committed["record_ids"]
        )
    assert verify(sync, result["bundle"]).status_code == 200
