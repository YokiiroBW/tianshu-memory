"""Semantic behavior and authority boundaries with deterministic model responses."""

import pytest
from test_memory_context import continuity  # noqa: F401

from tianshu_memory.domain import Fault
from tianshu_memory.retrieval import Retrieval, vectors


class Backend:
    def __init__(self, mode="hybrid", rerank=False):
        self.config = dict(
            mode=mode, rerank=rerank, candidates=40, revision="one", embedding_revision="one"
        )
        self.calls = []
        self.hook = None

    def call(self, operation, payload):
        self.calls.append((operation, payload))
        if operation == "settings":
            return dict(self.config)
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if operation == "embed":
            return {
                "vectors": [
                    [1.0, 0.0] if "咖啡" in text or "睡前" in text else [0.0, 1.0]
                    for text in payload["texts"]
                ]
            }
        return {"scores": [float("咖啡" in text) for text in payload["documents"]]}


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
@pytest.mark.parametrize("rerank", [False, True])
def test_modes_retrieve_paraphrase_and_preserve_complete_groups(h, mode, rerank):
    h.seed(
        [
            h.draft(),
            h.draft(
                field_key="hobby", units=[h.unit(statement="喜欢散步", conditions=[], negations=[])]
            ),
        ]
    )
    backend = Backend(mode, rerank)
    h.service.retrieval = Retrieval(backend)
    answer = h.select(query="睡前推荐")
    if mode == "keyword":
        assert answer["selected_units"] == []
        assert not any(op in {"embed", "rerank"} for op, _ in backend.calls)
    else:
        assert "咖啡" in answer["selected_units"][0]["statement"]
        assert answer["selected_units"][0]["negations"]
        assert any(op == "rerank" for op, _ in backend.calls) == rerank


def test_keyword_rerank_never_calls_embedding(h):
    h.seed()
    backend = Backend("keyword", True)
    h.service.retrieval = Retrieval(backend)
    assert h.select()["selected_units"]
    assert any(op == "rerank" for op, _ in backend.calls)
    assert not any(op == "embed" for op, _ in backend.calls)


def test_cache_reuses_documents_but_rebuilds_on_model_change(h):
    h.seed()
    backend = Backend("vector")
    h.service.retrieval = Retrieval(backend)
    h.select(query="睡前")
    backend.calls.clear()
    h.select(query="睡前")
    assert [payload["texts"] for op, payload in backend.calls if op == "embed"] == [["睡前"]]
    backend.config.update(revision="two", embedding_revision="two")
    backend.calls.clear()
    h.select(query="睡前")
    assert len([op for op, _ in backend.calls if op == "embed"]) == 2


def test_hidden_scope_never_reaches_models_and_forgotten_cache_is_not_used(h):
    seeded, _, _ = h.seed(
        [h.draft(), h.draft(scope=h.group, units=[h.unit(statement="群限定故事")])]
    )
    backend = Backend("vector", True)
    h.service.retrieval = Retrieval(backend)
    h.select(query="睡前")
    assert "群限定故事" not in str(backend.calls)
    forgotten = h.post("memory/revise", h.revision(seeded["record_ids"][0], "forget"))
    assert forgotten.status_code == 200
    backend.calls.clear()
    assert h.select(query="睡前")["selected_units"] == []
    assert all(op == "settings" for op, _ in backend.calls)


def test_concurrent_forget_during_model_call_rejected_without_database_lock(h):
    seeded, _, _ = h.seed()
    backend = Backend("vector")
    h.service.retrieval = Retrieval(backend)

    def forget():
        answer = h.post("memory/revise", h.revision(seeded["record_ids"][0], "forget"))
        assert answer.status_code == 200

    backend.hook = forget
    answer = h.post("memory/select", h.selection(query="睡前"))
    assert answer.status_code == 409
    assert answer.json()["code"] == "scope_changed"
    assert not h.service.retrieval.cache


@pytest.mark.parametrize("value", [[], [[0, 0]], [[float("nan")]], [[True]], [[1], [1, 2]]])
def test_invalid_vectors_fail_closed(value):
    with pytest.raises(Fault):
        vectors(value, 2 if len(value) == 2 else 1)


def test_context_query_semantic_path_without_lexical_overlap(request):
    from test_memory_context import post_ok, proposal, query_request

    h = request.getfixturevalue("continuity")
    receipt = post_ok(h, "propose", proposal(h))
    h.service.retrieval = Retrieval(Backend("vector", True))
    answer = post_ok(h, "query", query_request(h, query_text="睡前推荐"))
    assert answer["selected_units"][0]["record_id"] in receipt["record_ids"]
    assert answer["selected_units"][0]["negations"]


def test_exact_and_zero_budget_do_not_depend_on_models(h):
    h.seed()
    backend = Backend("vector", True)
    h.service.retrieval = Retrieval(backend)
    assert h.select(query="field:coffee")["selected_units"]
    assert not h.select(budget=0)["selected_units"]
    assert not backend.calls
