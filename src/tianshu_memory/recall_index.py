"""Whole-group validation and ranking over the current authorized snapshot."""

import json
from collections import defaultdict

from .domain import Fault, canonical, terms
from .fact_migration import installed
from .fact_queries import FactSnapshot, for_groups, group_queries, snapshot_for_groups
from .postgres import GROUP_READ_BATCH, is_postgres
from .recall_language import author_name_query, normalize_objects, topic_tokens
from .recall_relevance import LexicalQuery


def eligible_groups(service, db, groups, scope, *, scopes_by_group=None):
    """Validate whole groups, batching remote reads without prefetching stale content."""
    if service.source_authority is None:
        raise Fault("dependency_unavailable", 503)
    if not groups:
        return []
    scope_key = canonical(scope) if scopes_by_group is None else None
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
            owner = scope if scopes_by_group is None else scopes_by_group[group["id"]]
            owner_key = scope_key if scopes_by_group is None else canonical(owner)
            units = _units(group, rows, owner, owner_key)
            if units is not None and service._projections_current(db, units):
                result.append((group, rows, units))
        return result
    result = []
    for offset in range(0, len(groups), GROUP_READ_BATCH):
        batch = groups[offset : offset + GROUP_READ_BATCH]
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
        current = current_groups(service, db, keys, lineage)
        selected_keys = [key for key in keys if key in current]
        if not selected_keys:
            continue
        placeholders = ",".join("?" for _ in selected_keys)
        for row in db.execute(
            f"SELECT * FROM records WHERE group_id IN ({placeholders}) ORDER BY group_id,id",
            selected_keys,
        ):
            records[row["group_id"]].append(row)
        units = {}
        for group in batch:
            if group["id"] in current:
                owner = scope if scopes_by_group is None else scopes_by_group[group["id"]]
                owner_key = scope_key if scopes_by_group is None else canonical(owner)
                units[group["id"]] = _units(group, records[group["id"]], owner, owner_key)
        projections = {}
        for sql, parameters in projection_queries(units):
            for row in db.execute(sql, parameters):
                projections[row["ref"]] = row
        for group in batch:
            group_units = units.get(group["id"])
            if group_units is not None and service._projections_current(
                db, group_units, projections=projections
            ):
                result.append((group, records[group["id"]], group_units))
    return result


def exact_groups(service, db, scope, selection, kind, value):
    """PG-only exact recall: bounded metadata, then confirmed authorized reads."""
    if service.source_authority is None:
        raise Fault("dependency_unavailable", 503)
    scope_key = canonical(scope)
    from .fact_names import field_values

    placeholders = ",".join("?" for _ in selection)
    field = {"field": "field_key", "item": "item_key"}[kind]
    values = field_values(kind, value)
    predicate = field + (
        "=?" if len(values) == 1 else " IN (" + ",".join("?" for _ in values) + ")"
    )
    rows = db.native(
        "WITH candidates AS MATERIALIZED ("
        "SELECT * FROM groups WHERE scope=? AND state='active' AND category IN ("
        + placeholders
        + ") AND "
        + predicate
        + "),"
        "candidate_count AS MATERIALIZED (SELECT count(*) AS total FROM candidates),"
        "lineage_rows AS MATERIALIZED ("
        "SELECT l.group_id,l.revision AS required_revision,l.epoch AS required_epoch,"
        "s.key,s.state,s.revision,s.epoch FROM lineage l JOIN sources s ON s.key=l.source_key "
        "WHERE (SELECT total FROM candidate_count)<=? "
        "AND l.group_id IN (SELECT id FROM candidates)) "
        "SELECT g.id,g.scope,g.state,g.members,g.category,g.field_key,g.item_key,n.total AS exact_count,"
        "l.group_id AS exact_group_id,l.required_revision AS exact_required_revision,"
        "l.required_epoch AS exact_required_epoch,l.key AS exact_key,l.state AS exact_state,"
        "l.revision AS exact_revision,l.epoch AS exact_epoch "
        "FROM candidates g CROSS JOIN candidate_count n "
        "LEFT JOIN lineage_rows l ON l.group_id=g.id ORDER BY g.id",
        [scope_key, *selection, *values, GROUP_READ_BATCH],
    ).fetchall()
    groups, lineage = [], defaultdict(list)
    group_fields = ("id", "scope", "state", "members", "category", "field_key", "item_key")
    lineage_fields = ("required_revision", "required_epoch", "key", "state", "revision", "epoch")
    for row in rows:
        if not groups or groups[-1]["id"] != row["id"]:
            groups.append({key: row[key] for key in group_fields})
        if row["exact_group_id"] is not None:
            lineage[row["id"]].append({key: row["exact_" + key] for key in lineage_fields})
    if not groups or rows[0]["exact_count"] > GROUP_READ_BATCH:
        # The oversized metadata query emits no lineage rows. Retain the original
        # 512-group reads and complete candidate set, with no LIMIT/truncation.
        eligible = eligible_groups(service, db, groups, scope)
        return eligible, snapshot_for_groups(db, eligible, service=service)

    current = current_groups(service, db, [group["id"] for group in groups], lineage)
    selected = [group["id"] for group in groups if group["id"] in current]
    if not selected:
        return [], snapshot_for_groups(db, [], service=service)
    # Never queue record bodies before current_many/current has accepted every
    # source in the group. This batch contains no admission or suppression reads.
    record_rows, marker_rows = db.read_batch(
        [
            (
                "SELECT * FROM records WHERE group_id IN ("
                + ",".join("?" for _ in selected)
                + ") ORDER BY group_id,id",
                selected,
            ),
            ("SELECT value FROM metadata WHERE key='automatic_fact_schema'", ()),
        ]
    )
    records = defaultdict(list)
    for row in record_rows:
        records[row["group_id"]].append(row)
    units = {
        group["id"]: _units(group, records[group["id"]], scope, scope_key)
        for group in groups
        if group["id"] in current
    }
    marker = marker_rows.fetchone()
    fact_installed = marker is not None and marker[0] in {"1", "2", "3"}
    projection_reads = list(projection_queries(units))
    valid_keys = [key for key, value in units.items() if value is not None]
    requests = projection_reads + (list(group_queries(valid_keys)) if fact_installed else [])
    cursors = db.read_batch(requests) if requests else []
    projections = {row["ref"]: row for cursor in cursors[: len(projection_reads)] for row in cursor}
    eligible = [
        (group, records[group["id"]], units[group["id"]])
        for group in groups
        if units.get(group["id"]) is not None
        and service._projections_current(db, units[group["id"]], projections=projections)
    ]
    # A bad projection may coexist with malformed fact JSON. The old path never
    # decodes that discarded group's fact; retain the same omission/error order.
    valid_keys = {group["id"] for group, _, _ in eligible}
    facts = {
        row["group_id"]: row
        for cursor in cursors[len(projection_reads) :]
        for row in cursor
        if row["group_id"] in valid_keys
    }
    return eligible, (
        FactSnapshot.from_rows(facts, service=service, db=db, candidates=eligible)
        if fact_installed
        else FactSnapshot(False, {}, {})
    )


def current_groups(service, db, keys, lineage):
    current_many = getattr(service.source_authority, "current_many", None)
    return (
        current_many(db, lineage)
        if current_many is not None
        else {key for key in keys if service._group_current(db, key, rows=lineage[key])}
    )


def projection_queries(units):
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
    for start in range(0, len(refs), 256):
        selected = refs[start : start + 256]
        yield (
            "SELECT * FROM projections WHERE ref IN (" + ",".join("?" for _ in selected) + ")",
            selected,
        )


def _units(group, rows, scope, scope_key):
    """One integrity rule for local and batched reads; an incomplete group is omitted."""
    if (
        group["state"] != "active"
        or group["scope"] != scope_key
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


def rank_groups(
    service, db, eligible, search_terms, query, *, fact_snapshot=None, authorized_group_ids=None
):
    if not search_terms or not eligible:
        return []
    if author_name_query(query):
        # The aliases express a person-name question, not an object's name.
        # Reuse the source-proven view inside this already authorized domain.
        if fact_snapshot is None or fact_snapshot.names is None:
            fact_snapshot = snapshot_for_groups(db, eligible, service=service)
        names = fact_snapshot.names if fact_snapshot is not None else None
        eligible = [entry for entry in eligible if names and entry[0]["id"] in names.entries]
        if not eligible:
            return []
    # FTS metadata columns are UNINDEXED. Looking up each record separately scans
    # the whole virtual table for every record. Read only eligible scopes in bounded
    # batches, retaining exact versions before constructing the authorized corpus.
    indexed = {}
    scopes = sorted({group["scope"] for group, _, _ in eligible})
    for offset in range(0, len(scopes), 256):
        batch = scopes[offset : offset + 256]
        for row in db.execute(
            "SELECT record_id,record_version,text FROM search_index WHERE scope IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ):
            indexed.setdefault((row["record_id"], row["record_version"]), row["text"])
    from .fact_entities import OBJECT_NOUNS, historical_title_groups
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
        reference_terms(
            service,
            db,
            eligible,
            indexed,
            facts=facts,
            authorized_group_ids=authorized_group_ids,
            names=None if fact_snapshot is None else fact_snapshot.names,
        )
        if facts_installed and not history_query(query)
        else {}
    )
    raw_text = {
        group["id"]: " ".join(u["statement"] for u in units) for group, _, units in eligible
    }
    constraint_text = dict(raw_text)
    if fact_snapshot is not None:
        from .fact_labels import candidate as object_label
        from .fact_labels import constraint_head

        for group, _, units in eligible:
            row = fact_snapshot.rows.get(group["id"])
            fact = fact_snapshot.decoded.get(group["id"])
            if (
                row is not None
                and row["scope"] == group["scope"]
                and len(units) == 1
                and fact is not None
                and object_label(fact)
                and fact.get("entity") is not None
                and units[0]["statement"] == fact["value"]
            ):
                # A literal label can itself contain 狗/妹妹/否定. It is not an
                # object assertion; constrain only by its already bound entity,
                # without borrowing another object's words from the source quote.
                entity = fact["entity"]
                nouns = OBJECT_NOUNS.get(entity["kind"], (None, ()))[1]
                mention = constraint_head(fact)
                constraint_text[group["id"]] = " ".join([mention, *nouns])

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
        try:
            db.executemany("INSERT INTO temp.allowed_search VALUES (?,?)", documents)
            ranked = db.execute(
                "SELECT group_id,text,bm25(allowed_search) AS relevance FROM temp.allowed_search "
                "WHERE allowed_search MATCH ?",
                (expression,),
            ).fetchall()
        finally:
            # One transaction may rank several subqueries with different scopes.
            # Keep this temporary corpus owned by this invocation, not the connection.
            db.execute("DROP TABLE temp.allowed_search")
    query_plan = LexicalQuery(query)
    matches = {}
    clauses_by_group = {}
    best_time = {}
    for match in ranked:
        identity = match["group_id"]
        clauses = query_plan.match(set(match["text"].split()), constraint_text[identity])
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
