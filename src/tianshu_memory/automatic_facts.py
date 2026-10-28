"""Sourced automatic fact identity, consolidation and temporal retrieval.

All helpers run inside Memory's existing source/permission barrier and transaction.
Inferences never issue revision consent or publish a shared profile.
"""

import json
import unicodedata

from . import fact_entities, fact_names
from .domain import parse_time
from .fact_migration import SUPPORTED_VERSIONS, installed
from .fact_queries import for_groups

FIELDS = {"key", "value", "state", "valid_from", "valid_to", "keywords"}


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def index_text(db, unit, *, fact_schema=None):
    if not (installed(db) if fact_schema is None else fact_schema in SUPPORTED_VERSIONS):
        return ""
    group_id = unit["semantic_group_id"]
    row = db.execute("SELECT payload FROM automatic_facts WHERE group_id=?", (group_id,)).fetchone()
    if row is None:
        return ""
    fact = json.loads(row[0])
    quotes = [
        r[0]
        for r in db.execute(
            "SELECT quote FROM automatic_evidence WHERE group_id=? ORDER BY rowid LIMIT 128",
            (group_id,),
        )
    ]
    mention = fact["entity"]["mention"] if fact.get("entity") else ""
    return " ".join([fact["key"], fact["value"], mention, *fact["keywords"], *quotes])


def filter_current(db, candidates, query, clock, *, fact_snapshot=None, include_history=None):
    if not (installed(db) if fact_snapshot is None else fact_snapshot.installed):
        return candidates
    from .recall_language import history_query

    history = history_query(query) if include_history is None else include_history
    facts = for_groups(db, candidates) if fact_snapshot is None else fact_snapshot.rows
    decoded = None if fact_snapshot is None else fact_snapshot.decoded
    names = None if fact_snapshot is None else fact_snapshot.names
    current_names = names.current(clock, normalize) if names is not None else set()
    result = []
    for entry in candidates:
        row = (
            db.execute(
                "SELECT payload,superseded_by FROM automatic_facts WHERE group_id=?",
                (entry[0]["id"],),
            ).fetchone()
            if facts is None
            else facts.get(entry[0]["id"])
        )
        if row:
            fact = json.loads(row["payload"]) if decoded is None else decoded[entry[0]["id"]]
            if names is not None:
                name = names.entries.get(entry[0]["id"])
                if (fact["key"] == fact_names.KEY and name is None) or (
                    name is not None and not history and entry[0]["id"] not in current_names
                ):
                    continue
            if row["superseded_by"] and not history:
                continue
            if fact["valid_from"] and parse_time(fact["valid_from"]) > clock():
                continue
            if not history and (
                fact["state"] == "historical"
                or (fact["valid_to"] and parse_time(fact["valid_to"]) <= clock())
            ):
                continue
        result.append(entry)
    if not history and fact_snapshot is not None:
        from .canonical_facts import current_groups

        result = current_groups(result, fact_snapshot, clock)
    return fact_entities.filter_targets(db, result, query, facts=facts, decoded=decoded)
