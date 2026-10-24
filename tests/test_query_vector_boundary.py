"""Query-only embedding precedes fresh authority; document work stays authorized."""

import copy
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_memory_v2 import SyntheticVectors, candidate, commit, post, query, request, setup
from test_memory_v2_boundaries import source_calls, verify
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import Fault
from tianshu_memory.memory_reindex import MemoryReindexer
from tianshu_memory.read_lifecycle import cancellable_read
from tianshu_memory.retrieval import Retrieval


class BoundedVectors(SyntheticVectors):
    def __init__(self):
        super().__init__()
        self.config = super().call("settings", {})
        self.inputs, self.reranks = [], []
        self.probe = lambda: None
        self.on_query = lambda: None
        self.answer = {"vectors": [[1, 0]]}
        self.settings_failure = False

    def call(self, operation, payload):
        self.probe()
        if operation == "settings":
            if self.settings_failure:
                raise Fault("dependency_unavailable", 503)
            return dict(self.config)
        if operation == "rerank":
            self.reranks.append(copy.deepcopy(payload))
            return {"scores": list(range(len(payload["documents"])))}
        self.document_batches.append(payload["texts"])
        return {"vectors": [[1, 0] if "乌龙茶" in text else [0, 1] for text in payload["texts"]]}

    def embed_query_before(self, text, revision, deadline):
        return self._query(text, revision, deadline)

    def embed_query(self, text, revision):
        return self._query(text, revision, None)

    def _query(self, text, revision, deadline):
        self.probe()
        self.query_calls += 1
        self.inputs.append((text, revision, deadline))
        callback, self.on_query = self.on_query, lambda: None
        callback()
        if self.fail_query:
            raise TimeoutError("synthetic query failure")
        return copy.deepcopy(self.answer)


def indexed(sync):
    setup(sync)
    for serial, text in enumerate(("我喜欢乌龙茶。", "我周末骑行。"), 1):
        job, _ = candidate(sync, text, serial)
        assert commit(sync, job, text)["state"] == "completed"
    backend = BoundedVectors()
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    assert MemoryReindexer(sync.service).index_step()["indexed"] == 2
    sync.calls.clear()
    return backend


def outside_boundaries(sync, monkeypatch, backend):
    depth = threading.local()
    original = sync.store.transaction

    @contextmanager
    def transaction(*args, **kwargs):
        with original(*args, **kwargs) as db:
            depth.value = getattr(depth, "value", 0) + 1
            try:
                yield db
            finally:
                depth.value -= 1

    def probe():
        assert not getattr(depth, "value", 0), "backend ran within a Store transaction"
        assert sync.service.source_authority._operation_order._owner != threading.get_ident()

    monkeypatch.setattr(sync.store, "transaction", transaction)
    backend.probe = probe


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_bounded_query_uses_one_fresh_boundary_with_same_ranking(sync, monkeypatch, mode):
    backend = indexed(sync)
    backend.config["mode"] = mode
    outside_boundaries(sync, monkeypatch, backend)
    documents_before = copy.deepcopy(backend.document_batches)
    backend.on_query = lambda: source_calls(sync) == [] or pytest.fail("source read preceded BGE")
    selected = query(sync, "睡前选什么")
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert backend.query_calls == 1 and backend.inputs[0][:2] == ("睡前选什么", "synthetic:1")
    assert backend.document_batches == documents_before
    assert all(result["coverage"]["index_complete"] for result in selected["query_results"])
    assert [item["text"] for item in selected["evidence"]] == ["我喜欢乌龙茶。", "我周末骑行。"]
    assert verify(sync, selected["bundle"]).status_code == 200

    # The backend without its bounded capability retains the original external
    # phase and later complete source/domain recheck, with identical ranking.
    monkeypatch.setattr(backend, "embed_query_before", None)
    sync.calls.clear()
    original = query(sync, "睡前选什么")
    assert source_calls(sync) == ["snapshot", "current", "head"] * 2
    assert original["evidence"] == selected["evidence"]
    assert original["query_results"] == selected["query_results"]


@pytest.mark.parametrize("failure", ["timeout", "shape", "zero", "nan", "dimension"])
def test_failed_precompute_falls_back_without_lock_local_backend_retry(sync, monkeypatch, failure):
    backend = indexed(sync)
    outside_boundaries(sync, monkeypatch, backend)
    backend.fail_query = failure == "timeout"
    backend.answer = {
        "shape": {},
        "zero": {"vectors": [[0, 0]]},
        "nan": {"vectors": [[float("nan"), 0]]},
        "dimension": {"vectors": [[1, 0, 0]]},
    }.get(failure, backend.answer)
    selected = query(sync, "乌龙茶")
    assert selected["evidence"] and selected["query_results"][0]["status"] == "incomplete"
    assert not selected["query_results"][0]["coverage"]["index_complete"]
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert backend.query_calls == 1


@pytest.mark.parametrize("state", ["partial", "corrupt", "missing", "empty"])
def test_partial_corrupt_and_empty_index_keep_coverage_semantics(sync, state):
    backend = indexed(sync)
    with sync.store.transaction() as db:
        identity = db.execute(
            "SELECT document_id FROM memory_vectors ORDER BY document_id LIMIT 1"
        ).fetchone()[0]
        if state == "partial":
            db.execute("DELETE FROM memory_vectors WHERE document_id=?", (identity,))
        elif state == "corrupt":
            db.execute(
                "UPDATE memory_vectors SET vector='broken-vector' WHERE document_id=?", (identity,)
            )
        elif state == "missing":
            db.execute("DELETE FROM memory_vectors")
    if state == "empty":
        for grant in sync.grants:
            grant["state"] = "denied"
        sync.platform_head["sequence"] += 1
    selected = query(sync, "乌龙茶")
    assert selected["query_results"][0]["coverage"]["index_complete"] == (state == "empty")
    assert bool(selected["evidence"]) == (state != "empty")
    assert backend.query_calls == 1
    assert source_calls(sync) == ["snapshot", "current", "head"]
    if state == "corrupt":
        with sync.store.transaction() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM memory_vectors WHERE document_id=?", (identity,)
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize("kind", ["keyword", "settings_failure"])
def test_keyword_and_settings_failure_never_embed_query(sync, kind):
    backend = indexed(sync)
    if kind == "keyword":
        backend.config["mode"] = "keyword"
    else:
        backend.settings_failure = True
    selected = query(sync, "乌龙茶")
    assert selected["evidence"]
    assert selected["query_results"][0]["coverage"]["index_complete"] == (kind == "keyword")
    assert not backend.inputs
    assert source_calls(sync) == ["snapshot", "current", "head"]


def test_fresh_authority_after_precompute_observes_source_denial(sync, monkeypatch):
    backend = indexed(sync)
    outside_boundaries(sync, monkeypatch, backend)

    def deny():
        for grant in sync.grants:
            grant["state"] = "denied"
        sync.platform_head["sequence"] += 1

    backend.on_query = deny
    selected = query(sync, "乌龙茶")
    assert selected["evidence"] == []
    assert selected["query_results"][0]["status"] == "no_match"
    assert selected["query_results"][0]["coverage"]["index_complete"]
    assert source_calls(sync) == ["snapshot", "current", "head"]


@pytest.mark.parametrize("change", ["none", "withdraw"])
def test_bounded_backend_with_rerank_keeps_external_revalidation(sync, monkeypatch, change):
    backend = indexed(sync)
    backend.config["rerank"] = True
    outside_boundaries(sync, monkeypatch, backend)

    def during_query():
        assert source_calls(sync) == ["snapshot", "current", "head"]
        if change == "withdraw":
            for grant in sync.grants:
                grant["state"] = "denied"
            sync.platform_head["sequence"] += 1

    backend.on_query = during_query
    response = post(sync, "query", request(sync, "乌龙茶"))
    assert response.status_code == (200 if change == "none" else 409), response.text
    if change != "none":
        assert response.json()["code"] == "scope_changed"
    assert backend.query_calls == 1 and backend.reranks
    assert source_calls(sync) == ["snapshot", "current", "head"] * 2


def test_multiple_queries_share_external_budget_without_refilling_in_source(sync, monkeypatch):
    from tianshu_memory import persistent_retrieval
    from tianshu_memory.memory_context import read_v2

    backend = indexed(sync)
    outside_boundaries(sync, monkeypatch, backend)
    clock = [100.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(persistent_retrieval, "time", fake_time)
    monkeypatch.setattr(read_v2, "time", fake_time)
    original = backend._query

    def delayed(text, revision, deadline):
        response = original(text, revision, deadline)
        clock[0] += 6 if len(backend.inputs) == 1 else 3
        return response

    monkeypatch.setattr(backend, "_query", delayed)
    payload = request(sync, "乌龙茶")
    payload["queries"] = [
        dict(payload["queries"][0], query_id=f"q{i}", text=f"乌龙茶{i}") for i in range(3)
    ]
    response = post(sync, "query", payload)
    assert response.status_code == 200, response.text
    assert [item[2] for item in backend.inputs] == [108.0, 108.0]
    assert [item["coverage"]["index_complete"] for item in response.json()["query_results"]] == [
        True,
        False,
        False,
    ]
    assert source_calls(sync) == ["snapshot", "current", "head"]


def test_completed_derived_query_vector_survives_later_source_queue_time(sync, monkeypatch):
    from tianshu_memory import persistent_retrieval
    from tianshu_memory.memory_context import read_v2

    backend = indexed(sync)
    outside_boundaries(sync, monkeypatch, backend)
    clock = [100.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(persistent_retrieval, "time", fake_time)
    monkeypatch.setattr(read_v2, "time", fake_time)
    order = sync.service.source_authority._operation_order
    original = order.turn

    @contextmanager
    def delayed_turn():
        # Simulate elapsed FIFO time, not an authority clock/deadline extension.
        with original():
            clock[0] = 120.0
            yield

    monkeypatch.setattr(order, "turn", delayed_turn)
    selected = query(sync, "乌龙茶")
    assert selected["query_results"][0]["coverage"]["index_complete"]
    assert backend.query_calls == 1 and backend.inputs[0][2] == 108.0
    assert source_calls(sync) == ["snapshot", "current", "head"]


def test_cancelled_during_precompute_stops_before_source_read(sync):
    backend = indexed(sync)
    payload = request(sync, "乌龙茶")
    auth = sync.client.app.state.auth
    service, caller = auth.authenticate("Bearer test-only-companion-secret")
    context = auth.resolve(
        service, caller, payload["query"]["origin"]["assertion_ref"], payload["query"]["request_id"]
    )
    abandoned = threading.Event()
    backend.on_query = abandoned.set
    with cancellable_read(abandoned.is_set):
        with pytest.raises(Fault) as stopped:
            sync.client.app.state.memory_context.query_v2(payload, context)
    assert (stopped.value.code, stopped.value.status) == ("timeout", 408)
    assert backend.query_calls == 1 and source_calls(sync) == []


@pytest.mark.parametrize("mismatch", ["text", "revision", "embedding_revision", "candidates"])
def test_query_vector_binding_mismatch_never_reuses_or_calls_backend(mismatch):
    from tianshu_memory.persistent_retrieval import (
        PreparedRanking,
        prepare_query_vector,
        rank_prepared,
    )

    backend = BoundedVectors()
    retrieval = Retrieval(backend)
    settings = retrieval.settings()
    prepared = prepare_query_vector(retrieval, "原查询", settings, deadline=float("inf"))
    current, text = dict(settings), "原查询"
    if mismatch == "text":
        text = "另一查询"
    elif mismatch == "candidates":
        current[mismatch] += 1
    else:
        current[mismatch] += ":changed"
    document = dict(id="episode:one", content_hash="unused")
    plan = PreparedRanking(
        {document["id"]: document},
        1,
        [document["id"]],
        current,
        {document["id"]: (1, 0)},
        [],
        prepared,
    )
    ranked, complete = rank_prepared(retrieval, None, text, plan)
    assert ranked == [document] and not complete
    assert backend.query_calls == 1
