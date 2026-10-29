"""Internal retrieval explanations; never part of a signed/public evidence bundle."""

from collections import Counter


def build(query_id, plan, ranked, complete, *, omissions=(), selected=None):
    return dict(
        query_id=query_id,
        candidates=plan.document_count,
        channels=plan.channel_hits
        or dict(lexical=len(plan.lexical_ids), vector=len(plan.available)),
        rrf_count=plan.rrf_count if plan.rrf_count is not None else len(ranked),
        degradation=plan.degradation,
        index_incomplete=not complete,
        truncation_reasons=list(dict.fromkeys(omissions)),
        ranked_ids=[item["id"] for item in ranked],
        selected_ids=list(selected) if selected is not None else [item["id"] for item in ranked],
        distinct_subjects=len({item["scope"] for item in ranked if item.get("scope")}),
    )


def retention_distribution(db):
    """Read-only counts for replay reports, independent of model choices."""
    return {
        name: dict(Counter(row[0] for row in db.execute(f"SELECT {column} FROM {table}")))
        for name, table, column in (
            ("retention", "memory_segment_items", "retention"),
            ("importance", "memory_summaries", "importance"),
        )
    }
