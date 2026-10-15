"""Keyword ranking over one complete, currently authorized scope snapshot."""

from .domain import terms
from .fact_migration import installed
from .recall_constraints import time_specificity
from .recall_language import normalize_objects, topic_tokens
from .recall_relevance import matching_clauses


def rank_groups(db, eligible, search_terms, query):
    if not search_terms or not eligible:
        return []
    # FTS metadata columns are UNINDEXED. Looking up each record separately scans
    # the whole virtual table for every record. Read this scope once, retaining
    # exact record versions before constructing the authorized ranking corpus.
    indexed = {}
    for scope in {group["scope"] for group, _, _ in eligible}:
        for row in db.execute(
            "SELECT record_id,record_version,text FROM search_index WHERE scope=?", (scope,)
        ):
            indexed.setdefault((row["record_id"], row["record_version"]), row["text"])
    from .fact_entities import historical_title_groups

    historical = historical_title_groups(db, eligible, query) if installed(db) else set()
    raw_text = {
        group["id"]: " ".join(u["statement"] for u in units) for group, _, units in eligible
    }

    def document(group, rows):
        if any((row["id"], row["version"]) not in indexed for row in rows):
            return group["id"], ""
        text = " ".join(indexed.get((row["id"], row["version"]), "") for row in rows)
        normalized = normalize_objects(text)
        if normalized != text:
            text += " " + " ".join(terms(normalized))
        # Query-time vocabulary expansion keeps older persisted indexes usable.
        text += " " + " ".join(topic_tokens(raw_text[group["id"]]))
        if group["id"] in historical:
            text += " " + " ".join(search_terms)
        return group["id"], text

    db.execute("CREATE VIRTUAL TABLE temp.allowed_search USING fts5(group_id UNINDEXED,text)")
    db.executemany(
        "INSERT INTO temp.allowed_search VALUES (?,?)",
        (document(group, rows) for group, rows, _ in eligible),
    )
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search_terms)
    matches = {}
    clauses_by_group = {}
    best_time = {}
    for match in db.execute(
        "SELECT group_id,text,bm25(allowed_search) AS relevance FROM temp.allowed_search "
        "WHERE allowed_search MATCH ?",
        (expression,),
    ):
        identity = match["group_id"]
        clauses = matching_clauses(
            normalize_objects(query), set(match["text"].split()), raw_text[identity]
        )
        if identity not in historical and not clauses:
            continue
        clauses_by_group[identity] = clauses
        for clause in clauses:
            best_time[clause] = max(
                best_time.get(clause, 0), time_specificity(clause, raw_text[identity])
            )
        coverage = len(set(search_terms).intersection(match["text"].split()))
        matches[match["group_id"]] = (-coverage, match["relevance"], match["group_id"])
    return sorted(
        (
            entry
            for entry in eligible
            if (identity := entry[0]["id"]) in matches
            and (
                identity in historical
                or any(
                    time_specificity(clause, raw_text[identity]) == best_time[clause]
                    for clause in clauses_by_group[identity]
                )
            )
        ),
        key=lambda entry: matches[entry[0]["id"]],
    )
