"""Ranking metadata stays outside document identities and authorization digests."""

from datetime import UTC, datetime

from .domain import parse_time

WEIGHTS = {"relevance": 1.0, "importance": 0.3, "recency": 0.2}
IMPORTANCE = {"low": 0.2, "normal": 0.5, "high": 1.0}


def _normalize(values):
    low, high = min(values.values()), max(values.values())
    return {
        key: (value - low) / (high - low) if high > low else 0.5 for key, value in values.items()
    }


def combine(scores, documents, summaries=None, now=None, *, weights=None):
    if not scores:
        return {}
    summaries, now = summaries or {}, now or datetime.now(UTC)
    by_id = {item["id"]: item for item in documents}
    importance, recency = {}, {}
    for identity in scores:
        document = by_id[identity]
        summary = summaries.get(document.get("group_id"), {})
        importance[identity] = IMPORTANCE.get(summary.get("importance"), 0.5)
        observed = document.get("observed_at")
        recency[identity] = (
            0.5 ** (max(0, (now - parse_time(observed)).total_seconds()) / (720 * 3600))
            if observed
            else 0.5
        )
    components = {
        "relevance": _normalize(scores),
        "importance": _normalize(importance),
        "recency": _normalize(recency),
    }
    weights = WEIGHTS if weights is None else weights
    return {
        identity: sum(weights[name] * values[identity] for name, values in components.items())
        for identity in scores
    }


def order(scores, documents):
    by_id = {item["id"]: item for item in documents}
    return sorted(
        scores,
        key=lambda identity: (
            -scores[identity],
            by_id[identity].get("group_id") or identity,
            identity,
        ),
    )
