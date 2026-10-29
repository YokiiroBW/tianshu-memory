from datetime import UTC, datetime

from tianshu_memory.retrieval_scoring import combine, order


def test_fresh_important_memories_displace_old_low_importance_without_changing_bindings():
    documents = [
        dict(
            id=str(i),
            group_id=str(i),
            observed_at=("2026-01-01T00:00:00Z" if i < 8 else "2026-10-30T00:00:00Z"),
        )
        for i in range(16)
    ]
    summaries = {str(i): dict(importance="low" if i < 8 else "high") for i in range(16)}
    scores = {str(i): 1.0 for i in range(16)}
    before = repr(documents)
    ranked = order(
        combine(scores, documents, summaries, datetime(2026, 10, 30, tzinfo=UTC)), documents
    )
    assert all(int(identity) >= 8 for identity in ranked[:8])
    assert repr(documents) == before
    assert ranked == order(
        combine(scores, documents, summaries, datetime(2026, 10, 30, tzinfo=UTC)), documents
    )


def test_missing_time_and_equal_components_are_neutral_and_group_ties_stable():
    docs = [dict(id="b", group_id="b"), dict(id="a", group_id="a")]
    assert order(combine({"b": 1, "a": 1}, docs), docs) == ["a", "b"]
