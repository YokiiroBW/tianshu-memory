"""Tracing observes ranking without changing the result or the public bytes."""

from tianshu_memory.domain import canonical
from tianshu_memory.persistent_retrieval import PreparedRanking, rank_prepared


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
