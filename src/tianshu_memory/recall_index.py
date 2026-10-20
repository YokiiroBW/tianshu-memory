"""Whole-group validation and ranking over the current authorized snapshot."""

import json
from collections import defaultdict

from .domain import Fault, canonical, terms
from .fact_migration import installed
from .recall_language import normalize_objects, topic_tokens
from .recall_relevance import LexicalQuery


def eligible_groups(service, db, groups, scope):
    """Batch existing authoritative reads, preserving group order and all checks."""
    if service.source_authority is None:
        raise Fault("dependency_unavailable", 503)
    result = []
    for offset in range(0, len(groups), 128):
        batch = groups[offset : offset + 128]
        keys = [group["id"] for group in batch]
        placeholders = ",".join("?" for _ in keys)
        records, lineage = defaultdict(list), defaultdict(list)
        for row in db.execute(
            f"SELECT * FROM records WHERE group_id IN ({placeholders}) ORDER BY group_id,id",
            keys,
        ):
            records[row["group_id"]].append(row)
        for row in db.execute(
            "SELECT l.group_id,l.revision AS required_revision,l.epoch AS required_epoch,s.* "
            f"FROM lineage l JOIN sources s ON s.key=l.source_key WHERE l.group_id IN ({placeholders})",
            keys,
        ):
            lineage[row["group_id"]].append(row)
        units = {key: [json.loads(row["payload"]) for row in rows] for key, rows in records.items()}
        refs = list(
            {
                source["projection_ref"]
                for group_units in units.values()
                for unit in group_units
                for source in unit["sources"]
                if source["kind"] == "shareable_projection"
            }
        )
        projections = {}
        for start in range(0, len(refs), 256):
            selected = refs[start : start + 256]
            for row in db.execute(
                "SELECT * FROM projections WHERE ref IN (" + ",".join("?" for _ in selected) + ")",
                selected,
            ):
                projections[row["ref"]] = row
        for group in batch:
            rows, group_units = records[group["id"]], units.get(group["id"], [])
            if (
                group["state"] != "active"
                or group["scope"] != canonical(scope)
                or not rows
                or set(json.loads(group["members"])) != {row["id"] for row in rows}
                or any(
                    row["state"] != "active"
                    or unit["record_id"] != row["id"]
                    or unit["record_version"] != row["version"]
                    or unit["semantic_group_id"] != group["id"]
                    or unit["subject_person_id"] != scope["person_id"]
                    for row, unit in zip(rows, group_units, strict=True)
                )
            ):
                continue
            if scope["audience"] == "group" and any(
                unit["visibility"] != "shared_projection"
                or any(source["kind"] != "shareable_projection" for source in unit["sources"])
                for unit in group_units
            ):
                continue
            if service._group_current(
                db, group["id"], rows=lineage[group["id"]]
            ) and service._projections_current(db, group_units, projections=projections):
                result.append((group, rows, group_units))
    return result


def rank_groups(service, db, eligible, search_terms, query):
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
    from .fact_references import reference_terms
    from .recall_language import history_query

    historical = historical_title_groups(db, eligible, query) if installed(db) else set()
    references = (
        reference_terms(service, db, eligible, indexed)
        if installed(db) and not history_query(query)
        else {}
    )
    raw_text = {
        group["id"]: " ".join(u["statement"] for u in units) for group, _, units in eligible
    }

    def document(group, rows):
        if any((row["id"], row["version"]) not in indexed for row in rows):
            return group["id"], ""
        text = " ".join(indexed.get((row["id"], row["version"]), "") for row in rows)
        text += " " + references.get(group["id"], "")
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
    query_plan = LexicalQuery(query)
    matches = {}
    clauses_by_group = {}
    best_time = {}
    for match in db.execute(
        "SELECT group_id,text,bm25(allowed_search) AS relevance FROM temp.allowed_search "
        "WHERE allowed_search MATCH ?",
        (expression,),
    ):
        identity = match["group_id"]
        clauses = query_plan.match(set(match["text"].split()), raw_text[identity])
        if identity not in historical and not clauses:
            continue
        clauses_by_group[identity] = clauses
        for clause, specificity in clauses.items():
            best_time[clause] = max(best_time.get(clause, 0), specificity)
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
                    specificity == best_time[clause]
                    for clause, specificity in clauses_by_group[identity].items()
                )
            )
        ),
        key=lambda entry: matches[entry[0]["id"]],
    )
