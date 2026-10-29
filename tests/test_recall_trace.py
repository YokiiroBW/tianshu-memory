"""Tracing observes ranking without changing the result or the public bytes."""

from tianshu_memory.domain import canonical
from tianshu_memory.persistent_retrieval import PreparedQueryVector, PreparedRanking, rank_prepared


def test_trace_keeps_result_bytes_and_records_incomplete_index():
    settings = dict(mode="keyword", candidates=8, rerank=False)
    documents = {"fact:a": dict(id="fact:a", scope="owner-a")}
    plan = PreparedRanking(documents, 1, ["fact:a"], settings, {}, [])
    plain = canonical(rank_prepared(None, None, "query", plan))
    assert plan.trace is None
    settings["trace"] = True
    traced = canonical(rank_prepared(None, None, "query", plan, query_id="q1"))
    assert traced == plain
    assert plan.trace["query_id"] == "q1"
    assert plan.trace["selected_ids"] == ["fact:a"]
    assert plan.trace["distinct_subjects"] == 1


def test_diagnostic_buffer_is_bounded_and_uses_the_existing_operator_token():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tianshu_memory.diagnostics import CHAT_SERVICE, DEFAULT_TOKEN_ENV, Diagnostics

    diagnostics = Diagnostics(
        CHAT_SERVICE, {}, environ={DEFAULT_TOKEN_ENV: "synthetic-operator-token"}
    )
    try:
        app = diagnostics.install(FastAPI())
        for serial in range(140):
            diagnostics.recall_traces.append(dict(query_id=str(serial)))
        with TestClient(app) as client:
            path = "/internal/v1/diagnostics/recall-traces"
            assert client.get(path).status_code == 401
            response = client.get(
                path, headers={"Authorization": "Bearer synthetic-operator-token"}
            )
            assert response.status_code == 200
            assert len(response.json()["traces"]) == 128
            assert response.json()["traces"][0]["query_id"] == "12"
    finally:
        diagnostics.shutdown()


def test_channel_hits_report_the_shortlist_instead_of_all_indexed_vectors():
    settings = dict(mode="hybrid", candidates=1, rerank=False, trace=True)
    docs = {
        identity: dict(id=identity, group_id=identity, scope="owner")
        for identity in ("a", "b", "c")
    }
    query = PreparedQueryVector("q", canonical(settings), (1.0, 0.0))
    plan = PreparedRanking(
        docs, 3, ["a", "b", "c"], settings, {identity: (1.0, 0.0) for identity in docs}, [], query
    )
    result, complete = rank_prepared(None, None, "q", plan)
    assert complete and len(result) == 1
    assert plan.trace["candidates"] == 3
    assert plan.trace["channels"] == dict(lexical=1, vector=1)
    assert plan.trace["rrf_count"] == 1
