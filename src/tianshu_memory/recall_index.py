"""Whole-group validation and ranking over the current authorized snapshot."""

import json
from collections import defaultdict

from .domain import Fault, canonical, terms
from .fact_migration import installed
from .fact_queries import for_groups
from .postgres import is_postgres
from .recall_language import normalize_objects, topic_tokens
from .recall_relevance import LexicalQuery


def eligible_groups(service, db, groups, scope):
    """Validate whole groups, batching remote reads without prefetching stale content."""
    if service.source_authority is None:
        raise Fault("dependency_unavailable", 503)
    if not is_postgres(db):
        # Local SQLite keeps its inexpensive, lazy reads. In particular, a revoked
        # source must short-circuit before any record payload/projection is read.
        result = []
        for group in groups:
            if not service._group_current(db, group["id"]):
                continue
            rows = db.execute(
                "SELECT * FROM records WHERE group_id=? ORDER BY id", (group["id"],)
            ).fetchall()
            units = _units(group, rows, scope)
            if units is not None and service._projections_current(db, units):
                result.append((group, rows, units))
        return result
    result = []
    for offset in range(0, len(groups), 128):
        batch = groups[offset : offset + 128]
        keys = [group["id"] for group in batch]
        placeholders = ",".join("?" for _ in keys)
        records, lineage = defaultdict(list), defaultdict(list)
        for row in db.execute(
            "SELECT l.group_id,l.revision AS required_revision,l.epoch AS required_epoch,"
            "s.key,s.state,s.revision,s.epoch "
            f"FROM lineage l JOIN sources s ON s.key=l.source_key WHERE l.group_id IN ({placeholders})",
            keys,
        ):
            lineage[row["group_id"]].append(row)
        current_many = getattr(service.source_authority, "current_many", None)
        current = (
            current_many(db, lineage)
            if current_many is not None
            else {key for key in keys if service._group_current(db, key, rows=lineage[key])}
        )
        selected_keys = [key for key in keys if key in current]
        if not selected_keys:
            continue
        placeholders = ",".join("?" for _ in selected_keys)
        for row in db.execute(
            f"SELECT * FROM records WHERE group_id IN ({placeholders}) ORDER BY group_id,id",
            selected_keys,
        ):
            records[row["group_id"]].append(row)
        units = {
            group["id"]: _units(group, records[group["id"]], scope)
            for group in batch
            if group["id"] in current
        }
        refs = list(
            {
                source["projection_ref"]
                for group_units in units.values()
                if group_units is not None
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
            group_units = units.get(group["id"])
            if group_units is not None and service._projections_current(
                db, group_units, projections=projections
            ):
                result.append((group, records[group["id"]], group_units))
    return result


def _units(group, rows, scope):
    """One integrity rule for local and batched reads; an incomplete group is omitted."""
    if (
        group["state"] != "active"
        or group["scope"] != canonical(scope)
        or not rows
        or set(json.loads(group["members"])) != {row["id"] for row in rows}
        or any(row["state"] != "active" for row in rows)
    ):
        return None
    units = [json.loads(row["payload"]) for row in rows]
    if any(
        unit["record_id"] != row["id"]
        or unit["record_version"] != row["version"]
        or unit["semantic_group_id"] != group["id"]
        or unit["subject_person_id"] != scope["person_id"]
        for row, unit in zip(rows, units, strict=True)
    ):
        return None
    if scope["audience"] == "group" and any(
        unit["visibility"] != "shared_projection"
        or any(source["kind"] != "shareable_projection" for source in unit["sources"])
        for unit in units
    ):
        return None
    return units


def rank_groups(service, db, eligible, search_terms, query, *, fact_snapshot=None):
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

    facts_installed = installed(db) if fact_snapshot is None else fact_snapshot.installed
    if fact_snapshot is not None:
        facts = fact_snapshot.rows
    else:
        facts = for_groups(db, eligible) if facts_installed else None
    decoded = None if fact_snapshot is None else fact_snapshot.decoded
    historical = (
        historical_title_groups(db, eligible, query, facts=facts, decoded=decoded)
        if facts_installed
        else set()
    )
    references = (
        reference_terms(service, db, eligible, indexed, facts=facts)
        if facts_installed and not history_query(query)
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

    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search_terms)
    documents = (document(group, rows) for group, rows, _ in eligible)
    if is_postgres(db):
        from .postgres_recall import rank

        ranked = rank(db, documents, expression)
    else:
        db.execute("CREATE VIRTUAL TABLE temp.allowed_search USING fts5(group_id UNINDEXED,text)")
        db.executemany("INSERT INTO temp.allowed_search VALUES (?,?)", documents)
        ranked = db.execute(
            "SELECT group_id,text,bm25(allowed_search) AS relevance FROM temp.allowed_search "
            "WHERE allowed_search MATCH ?",
            (expression,),
        )
    query_plan = LexicalQuery(query)
    matches = {}
    clauses_by_group = {}
    best_time = {}
    for match in ranked:
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
