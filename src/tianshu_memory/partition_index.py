"""Rebuildable lexical terms and exact BM25 within each authorized owner."""

import json
import math
from collections import Counter

from .domain import terms


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_partition_schema'").fetchone()
    return row is not None and row[0] == "1"


def refresh(db, group_id):
    if not installed(db):
        return
    rows = db.execute(
        "SELECT payload FROM records WHERE group_id=? AND state='active' ORDER BY id", (group_id,)
    )
    text = " ".join(token for row in rows for token in terms(json.loads(row[0])["statement"]))
    db.execute(
        "INSERT INTO memory_recall_documents VALUES (?,?) ON CONFLICT(group_id) "
        "DO UPDATE SET terms=excluded.terms",
        (group_id, text),
    )


def rank(db, per_owner, search_terms):
    if not search_terms:
        return {owner: [] for owner in per_owner}
    by_id = {entry[0]["id"]: entry for rows in per_owner.values() for entry in rows}
    indexed = {}
    if installed(db):
        identities = list(by_id)
        for start in range(0, len(identities), 256):
            batch = identities[start : start + 256]
            indexed.update(
                (row[0], row[1])
                for row in db.execute(
                    "SELECT group_id,terms FROM memory_recall_documents WHERE group_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                )
            )
    # A missing derived index never grants access, writes on a read, or hides a legacy group.
    for identity, entry in by_id.items():
        if identity not in indexed:
            indexed[identity] = " ".join(
                token for unit in entry[2] for token in terms(unit["statement"])
            )
    return rank_terms(per_owner, indexed, search_terms)


def rank_terms(per_owner, indexed, search_terms):
    result = {}
    for owner, entries in per_owner.items():
        tokens = {entry[0]["id"]: Counter(indexed[entry[0]["id"]].split()) for entry in entries}
        lengths = {key: sum(value.values()) for key, value in tokens.items()}
        average = sum(lengths.values()) / len(tokens) if tokens else 0
        frequencies = {
            term: sum(term in values for values in tokens.values()) for term in search_terms
        }
        scores = {}
        for identity, values in tokens.items():
            hits = [term for term in search_terms if values[term]]
            if not hits or not average:
                continue
            scores[identity] = sum(
                max(
                    1e-6,
                    math.log((len(tokens) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)),
                )
                * values[term]
                * 2.2
                / (values[term] + 1.2 * (0.25 + 0.75 * lengths[identity] / average))
                for term in hits
            )
        result[owner] = sorted(
            (entry for entry in entries if entry[0]["id"] in scores),
            key=lambda entry: (-scores[entry[0]["id"]], entry[0]["id"]),
        )
    return result
