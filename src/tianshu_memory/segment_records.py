"""Per-experience state and retention using the existing records/history/lineage."""

import json
from datetime import timedelta

from .domain import canonical, new_id, parse_time, require, utc
from .memory_documents import index_group, remove_groups
from .workflow import _WorkflowBase

RETENTION = {"short": "low", "medium": "normal", "long": "high"}


def record_refs(db, group_id):
    return [
        dict(record_id=row["id"], record_version=row["version"])
        for row in db.execute(
            "SELECT id,version FROM records WHERE group_id=? ORDER BY id", (group_id,)
        )
    ]


def expires(service, effective_at, memory):
    result = parse_time(effective_at) + timedelta(
        days=service.consolidation_ttl_days[RETENTION[memory["retention"]]]
    )
    if (
        memory["kind"] == "agreement"
        and memory["state"] in {"considering", "confirmed", "unknown"}
        and memory["valid_until"]
    ):
        result = max(result, parse_time(memory["valid_until"]))
    return utc(result)


def write(
    service, db, batch, target, memory, source_rows, dependencies, *, kind="summary", refs=()
):
    scope = target["scope"]
    require(
        source_rows and all(row["scope"] == canonical(scope) for row in source_rows),
        "scope_changed",
        409,
    )
    group_id, record_id = new_id("group"), new_id("record")
    shared = scope["audience"] == "group"
    unit_sources = (
        [
            dict(
                kind="shareable_projection",
                owner="memory",
                projection_ref=new_id("projection"),
                projection_version=1,
            )
        ]
        if shared
        else [
            json.loads(row["payload"])
            if json.loads(row["payload"]).get("kind") == "observation"
            else dict(kind="raw_message", source=json.loads(row["payload"]))
            for row in source_rows
        ]
    )
    unit = dict(
        record_id=record_id,
        record_version=1,
        semantic_group_id=group_id,
        subject_person_id=scope["person_id"],
        statement=memory["text"].strip(),
        conditions=[],
        negations=[],
        valid_time=memory["valid_until"] or "未说明有效时间",
        uncertainty="inferred",
        reality=source_rows[0]["reality"],
        visibility="shared_projection" if shared else "self_private",
        sources=unit_sources,
    )
    service.contracts.validate("memory-extraction#segment_unit", unit)
    db.execute(
        "INSERT INTO groups VALUES (?,?,'active',?,'evidence',?,NULL)",
        (group_id, canonical(scope), canonical([record_id]), "memory." + kind),
    )
    _WorkflowBase(service)._store_unit(db, unit, group_id, scope, "memory." + kind, None)
    for source in source_rows:
        db.execute(
            "INSERT INTO lineage VALUES (?,?,?,?)",
            (group_id, source["key"], source["revision"], source["epoch"]),
        )
    manifest = json.loads(batch["manifest"])
    members = {member["member_id"]: member for member in manifest["members"]}
    effective_at = max(
        (
            members[item]["observed_at"]
            for item in memory["source_member_ids"]
            if item in target["new_member_ids"]
        ),
        key=parse_time,
    )
    expiry = expires(service, effective_at, memory)
    db.execute(
        "INSERT INTO memory_summaries VALUES (?,?,?,?,?,?,?,?,?)",
        (
            group_id,
            kind,
            canonical(scope),
            batch["window_id"],
            batch["manifest"],
            canonical(list(refs)),
            memory["importance"],
            batch["recorded_at"],
            expiry,
        ),
    )
    db.execute(
        "INSERT INTO memory_segment_items VALUES (?,?,?,?,?,?,?,?)",
        (
            group_id,
            memory["kind"],
            memory["state"],
            memory["retention"],
            memory["valid_until"],
            effective_at,
            canonical(memory.get("prior_refs", []) if memory.get("operation") == "replace" else []),
            parse_time(effective_at).timestamp(),
        ),
    )
    all_dependencies = {row["key"]: row for row in dependencies}
    for ref in refs:
        prior = db.execute(
            "SELECT group_id FROM records WHERE id=?", (ref["record_id"],)
        ).fetchone()
        require(prior is not None, "scope_changed", 409)
        for row in db.execute(
            "SELECT s.* FROM memory_summary_sources d JOIN sources s ON s.key=d.source_key WHERE d.group_id=?",
            (prior[0],),
        ):
            all_dependencies[row["key"]] = row
    require(len(all_dependencies) <= 256, "budget_exceeded", 413)
    for row in all_dependencies.values():
        db.execute(
            "INSERT INTO memory_summary_sources VALUES (?,?,?,?,?,?)",
            (group_id, row["key"], row["revision"], row["epoch"], row["scope"], "context"),
        )
    index_group(db, group_id)
    return group_id, record_refs(db, group_id), expiry


def supersede(db, group_id):
    """Replace a derived view while preserving exact original source and history."""
    db.execute("UPDATE groups SET state='superseded' WHERE id=?", (group_id,))
    remove_groups(db, [group_id])
    db.execute(
        "DELETE FROM search_index WHERE record_id IN (SELECT id FROM records WHERE group_id=?)",
        (group_id,),
    )
    db.execute(
        "UPDATE projections SET state='invalidated',version=version+1 WHERE record_id IN (SELECT id FROM records WHERE group_id=?)",
        (group_id,),
    )


def lifecycle(db, row, importance, effective_at, expiry):
    """All results contribute; an earlier writer never chooses a shared source TTL."""
    from .segment_sources import equivalent_keys

    keys = sorted(equivalent_keys(db, [row["key"]]))
    for source in db.execute(
        "SELECT key,revision FROM sources WHERE key IN ("
        + ",".join("?" for _ in keys)
        + ") AND scope=? AND state='active' AND revision=?",
        [*keys, row["scope"], row["revision"]],
    ):
        db.execute(
            "INSERT INTO memory_source_lifecycle VALUES (?,?,?,?,?) ON CONFLICT(source_key,revision) DO UPDATE SET "
            "importance=excluded.importance,expires_at=excluded.expires_at WHERE excluded.expires_at>memory_source_lifecycle.expires_at",
            (source["key"], source["revision"], importance, effective_at, expiry),
        )
