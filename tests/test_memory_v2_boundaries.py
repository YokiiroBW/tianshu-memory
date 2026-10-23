"""A single fresh boundary for local selection, and full revalidation after backend work."""

import pytest
from test_memory_v2 import candidate, commit, fact, post, query, request, setup
from test_retrieval import Backend
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.memory_reindex import MemoryReindexer
from tianshu_memory.retrieval import Retrieval


def attach(sync, mode, rerank=False):
    backend = Backend(mode, rerank)
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    return backend


def source_calls(sync):
    return [kind for kind, _, _ in sync.calls if kind != "origin"]


def verify(sync, bundle):
    return post(
        sync,
        "verify",
        dict(
            schema_version=2,
            query=request(sync, "咖啡")["query"],
            requested_scope=sync.scope(),
            bundle=bundle,
            checks=[],
        ),
    )


@pytest.mark.parametrize("mode", ["keyword", "hybrid", "vector"])
def test_empty_query_seals_negative_in_one_fresh_boundary(sync, mode):
    setup(sync)
    backend = attach(sync, mode, True)
    sync.calls.clear()
    selected = query(sync, "我喜欢什么咖啡")
    assert selected["evidence"] == []
    assert selected["query_results"][0]["status"] == "no_match"
    assert "no_match" in selected["bundle"]["selection_constraints"][0]["proof_kinds"]
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert [operation for operation, _ in backend.calls] == ["settings"]
    assert verify(sync, selected["bundle"]).status_code == 200
    text = "我喜欢咖啡。"
    job, _ = candidate(sync, text)
    commit(sync, job, text, fact("咖啡"))
    assert verify(sync, selected["bundle"]).status_code == 409


@pytest.mark.parametrize("mode", ["keyword", "hybrid", "vector"])
def test_lexical_only_selection_and_fallback_use_one_fresh_boundary(sync, mode):
    setup(sync)
    text = "我喜欢咖啡。"
    job, _ = candidate(sync, text)
    commit(sync, job, text, fact("咖啡"))
    backend = attach(sync, mode)
    sync.calls.clear()
    selected = query(sync, "我喜欢什么咖啡")
    assert any(item.get("fact", {}).get("value") == "咖啡" for item in selected["evidence"])
    assert selected["query_results"][0]["coverage"]["index_complete"] == (mode == "keyword")
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert [operation for operation, _ in backend.calls] == ["settings"]
    assert verify(sync, selected["bundle"]).status_code == 200
    sync.grants[-1]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    assert verify(sync, selected["bundle"]).status_code == 409


@pytest.mark.parametrize("kind", ["embedding", "rerank"])
@pytest.mark.parametrize("change", ["none", "withdraw", "new_source"])
def test_external_ranking_releases_then_revalidates_complete_domain(sync, kind, change):
    setup(sync)
    text = "我喜欢咖啡。"
    job, _ = candidate(sync, text)
    commit(sync, job, text, fact("咖啡"))
    backend = attach(sync, "hybrid" if kind == "embedding" else "keyword", kind == "rerank")
    if kind == "embedding":
        assert MemoryReindexer(sync.service).index_step()["indexed"] == 2

    def during_backend():
        # Nested real Memory writes must be able to acquire their own source
        # boundary while ranking waits outside its original transaction.
        if change == "withdraw":
            sync.grants[-1]["state"] = "denied"
            sync.platform_head["sequence"] += 1
        elif change == "new_source":
            newer = "我喜欢骑行。"
            new_job, _ = candidate(sync, newer, 2)
            assert (
                commit(sync, new_job, newer, fact("骑行", predicate="hobby"))["state"]
                == "completed"
            )

    backend.hook = during_backend
    backend.calls.clear()
    sync.calls.clear()
    response = post(sync, "query", request(sync, "我喜欢什么咖啡"))
    assert any(
        operation == ("embed" if kind == "embedding" else "rerank")
        for operation, _ in backend.calls
    )
    if change == "none":
        assert response.status_code == 200, response.text
        assert source_calls(sync) == ["snapshot", "current", "head"] * 2
        assert verify(sync, response.json()["bundle"]).status_code == 200
    else:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "scope_changed"
