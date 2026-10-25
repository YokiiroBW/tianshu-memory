"""Rebuildable fact/episode projections; source ownership always remains authoritative."""

import hashlib
import json

from .domain import canonical, fingerprint, parse_time, query_terms, terms
from .memory_migration import installed


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def put(db, document):
    previous = db.execute(
        "SELECT content_hash FROM memory_documents WHERE id=?", (document["id"],)
    ).fetchone()
    columns = tuple(document)
    db.execute(
        "INSERT INTO memory_documents("
        + ",".join(columns)
        + ") VALUES ("
        + ",".join("?" for _ in columns)
        + ") ON CONFLICT(id) DO UPDATE SET "
        + ",".join(f"{key}=excluded.{key}" for key in columns if key != "id"),
        tuple(document.values()),
    )
    if previous and previous[0] != document["content_hash"]:
        db.execute(
            "DELETE FROM memory_vectors WHERE document_id=? AND content_hash!=?",
            (document["id"], document["content_hash"]),
        )


def capture_sources(service, db, event, content, rows):
    if not installed(db):
        return
    for index, row in enumerate(rows):
        item = content[index]
        if (
            not item["text"].strip()
            or db.execute("SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)).fetchone()
        ):
            continue
        capture(db, row, item["text"], item["sent_at"])


def capture(db, source, text, observed_at):
    """Small exact overlapping spans fit the local encoder without losing the original."""
    encoded_scope = source["scope"]
    source_key = source["key"]
    db.execute(
        "DELETE FROM memory_vectors WHERE document_id IN (SELECT id FROM memory_documents WHERE source_key=? AND (source_revision!=? OR source_epoch!=?))",
        (source_key, source["revision"], source["epoch"]),
    )
    db.execute(
        "DELETE FROM memory_episode_index WHERE document_id IN (SELECT id FROM memory_documents WHERE source_key=? AND (source_revision!=? OR source_epoch!=?))",
        (source_key, source["revision"], source["epoch"]),
    )
    db.execute(
        "DELETE FROM memory_documents WHERE source_key=? AND (source_revision!=? OR source_epoch!=?)",
        (source_key, source["revision"], source["epoch"]),
    )
    start = 0
    while start < len(text):
        end = min(len(text), start + 240)
        if end < len(text):
            boundary = max(
                (text.rfind(mark, start + 120, end) for mark in "。！？\n；"), default=-1
            )
            if boundary >= start + 120:
                end = boundary + 1
        part = text[start:end]
        span_start, span_end = len(text[:start].encode()), len(text[:end].encode())
        identity = "episode:" + fingerprint(
            [source_key, source["revision"], source["epoch"], span_start, span_end]
        )
        put(
            db,
            dict(
                id=identity,
                kind="episode",
                scope=encoded_scope,
                group_id=None,
                source_key=source_key,
                source_revision=source["revision"],
                source_epoch=source["epoch"],
                content_hash=text_hash(part),
                text=part,
                observed_at=observed_at,
                span_start=span_start,
                span_end=span_end,
            ),
        )
        from .recall_language import topic_tokens

        db.execute("DELETE FROM memory_episode_index WHERE document_id=?", (identity,))
        db.execute(
            "INSERT INTO memory_episode_index VALUES (?,?,?)",
            (identity, encoded_scope, " ".join(terms(part) + topic_tokens(part))),
        )
        if end == len(text):
            break
        start = max(start + 1, end - 40)


def index_group(db, group_id, *, fact_schema=None):
    if not (installed(db) if fact_schema is None else fact_schema == "3"):
        return
    group = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None or group["state"] != "active":
        remove_groups(db, [group_id])
        return
    rows = db.execute(
        "SELECT payload FROM records WHERE group_id=? AND state='active' ORDER BY id", (group_id,)
    ).fetchall()
    units = [json.loads(row[0]) for row in rows]
    if not units:
        return
    from .consolidation_sources import metadata

    summary = metadata(db, [group_id]).get(group_id)
    if summary is not None:
        manifest = json.loads(summary["manifest"])
        observed = max((item["observed_at"] for item in manifest["members"]), key=parse_time)
        document = fact_document(group, units, None, observed)
        document["kind"] = summary["kind"]
        document["text"] = "\n".join(unit["statement"] for unit in units)
        document["content_hash"] = text_hash(document["text"])
        document["span_end"] = len(document["text"].encode())
        put(db, document)
        return
    automatic = db.execute(
        "SELECT payload,observed_at FROM automatic_facts WHERE group_id=?", (group_id,)
    ).fetchone()
    observed = automatic["observed_at"] if automatic else None
    if observed is None:
        times = [
            row[0]
            for row in db.execute(
                "SELECT d.observed_at FROM lineage l JOIN memory_documents d ON d.source_key=l.source_key AND d.source_revision=l.revision AND d.source_epoch=l.epoch WHERE l.group_id=? AND d.observed_at IS NOT NULL",
                (group_id,),
            )
        ]
        observed = max(times, key=parse_time) if times else None
    put(
        db,
        fact_document(
            group, units, json.loads(automatic["payload"]) if automatic else None, observed
        ),
    )


def fact_document(group, units, fact, observed):
    """Build a projection from an already-approved whole group and batched fact snapshot."""
    text = canonical(
        [
            {key: unit[key] for key in ("statement", "conditions", "negations", "valid_time")}
            for unit in units
        ]
    )
    if fact:
        if fact.get("canonical"):
            text = canonical(
                fact["canonical"]
                | {"conditions": units[0]["conditions"], "negations": units[0]["negations"]}
            )
        else:
            text = canonical(
                {
                    key: fact[key]
                    for key in ("key", "value", "state", "valid_from", "valid_to", "keywords")
                }
                | {"conditions": units[0]["conditions"], "negations": units[0]["negations"]}
            )
    return dict(
        id="fact:" + group["id"],
        kind="fact",
        scope=group["scope"],
        group_id=group["id"],
        source_key=None,
        source_revision=None,
        source_epoch=None,
        content_hash=text_hash(text),
        text=text,
        observed_at=observed,
        span_start=0,
        span_end=len(text.encode()),
    )


def remove_groups(db, identities):
    if not installed(db):
        return
    for identity in identities:
        db.execute("DELETE FROM memory_vectors WHERE document_id=?", ("fact:" + identity,))
        db.execute("DELETE FROM memory_documents WHERE group_id=?", (identity,))


def discard(db, identity):
    db.execute("DELETE FROM memory_vectors WHERE document_id=?", (identity,))
    db.execute("DELETE FROM memory_episode_index WHERE document_id=?", (identity,))
    db.execute("DELETE FROM memory_documents WHERE id=?", (identity,))


def episodes(service, db, scopes, *, query_text=None, metadata_only=False, after=None, limit=4097):
    if not installed(db) or not scopes:
        return []
    keys = [canonical(scope) for scope in scopes]
    predicate, params = "", list(keys)
    if query_text is not None:
        from .recall_language import candidate_aliases

        tokens = set(query_terms(query_text))
        for alias in candidate_aliases(query_text):
            tokens.update(terms(alias))
        if not tokens:
            return []
        predicate = " AND d.id IN (SELECT document_id FROM memory_episode_index WHERE text MATCH ?)"
        params.append(" OR ".join('"' + token.replace('"', '""') + '"' for token in sorted(tokens)))
    if after is not None:
        predicate += " AND d.id>?"
        params.append(after)
    columns = (
        "d.id,d.kind,d.scope,d.group_id,d.source_key,d.source_revision,d.source_epoch,d.content_hash,d.observed_at,d.span_start,d.span_end"
        if metadata_only
        else "d.*,s.payload AS source_payload,s.reality"
    )
    origin = " AND json_extract(a.payload,'$.scope.actor_id')=json_extract(d.scope,'$.actor_id') AND json_extract(a.payload,'$.scope.person_id')=json_extract(d.scope,'$.person_id') AND json_extract(a.payload,'$.scope.audience')=json_extract(d.scope,'$.audience') AND json_extract(a.payload,'$.scope.conversation_id')=json_extract(d.scope,'$.conversation_id') AND (json_extract(d.scope,'$.audience')!='group' OR (json_extract(p.payload,'$.audience')='group' AND json_extract(p.payload,'$.conversation_id')=json_extract(d.scope,'$.conversation_id')))"
    from .segment_migration import installed as segments_installed

    joins = (
        " JOIN source_admissions a ON a.key=s.key JOIN physical_sources p ON p.key=a.physical_key "
    )
    admission = " AND a.verified=1" + origin
    if segments_installed(db):
        joins = " LEFT JOIN source_admissions a ON a.key=s.key LEFT JOIN physical_sources p ON p.key=a.physical_key "
        admission = (
            " AND ((a.verified=1"
            + origin
            + ") OR EXISTS (SELECT 1 FROM memory_materials m WHERE m.source_key=s.key AND m.scope=d.scope AND m.kind='observation' AND m.verified=1))"
        )
        predicate += (
            " AND NOT EXISTS (SELECT 1 FROM memory_material_aliases x JOIN memory_materials m ON m.id=x.material_id "
            "JOIN sources os ON os.key=m.source_key JOIN memory_documents od ON od.source_key=os.key "
            "WHERE x.source_key=d.source_key AND m.kind='observation' AND m.verified=1 AND m.scope=d.scope "
            "AND os.scope=d.scope AND os.state='active' AND os.revision=s.revision "
            "AND od.kind='episode' AND od.scope=d.scope AND od.source_revision=os.revision AND od.source_epoch=os.epoch "
            "AND od.span_start=d.span_start AND od.span_end=d.span_end AND od.content_hash=d.content_hash "
            "AND NOT EXISTS (SELECT 1 FROM suppression oz WHERE oz.source_key=os.key))"
        )
    rows = db.execute(
        "SELECT "
        + columns
        + " FROM memory_documents d JOIN sources s ON s.key=d.source_key"
        + joins
        + "WHERE d.kind='episode' AND d.scope IN ("
        + ",".join("?" for _ in keys)
        + ") AND d.scope=s.scope AND s.state='active' AND d.source_revision=s.revision AND d.source_epoch=s.epoch AND NOT EXISTS (SELECT 1 FROM suppression z WHERE z.source_key=s.key)"
        + admission
        + predicate
        + (
            " ORDER BY d.id LIMIT ?"
            if after is not None
            else " ORDER BY d.observed_at DESC,d.id LIMIT ?"
        ),
        [*params, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def eligible_episodes(service, db, rows, subquery):
    """The same temporal/retention candidate domain for proof and actual ranking."""
    from .consolidation_sources import expired_sources

    if "episodes" not in subquery["kinds"]:
        return []
    expired = (
        expired_sources(db, [row["source_key"] for row in rows], service.clock)
        if subquery["intent"] == "current"
        else set()
    )
    period = subquery["time_filter"]
    return [
        row
        for row in rows
        if row["source_key"] not in expired
        and (
            period is None
            or period["basis"] != "observed_at"
            or row["observed_at"] is None
            or parse_time(period["from"])
            <= parse_time(row["observed_at"])
            < parse_time(period["to"])
        )
    ]


def episode_metadata(service, db, scopes, *, query_text=None):
    """Walk the complete authorized index in bounded pages, without loading source bodies."""
    after = ""
    while True:
        page = episodes(
            service, db, scopes, query_text=query_text, metadata_only=True, after=after, limit=512
        )
        yield from page
        if len(page) < 512:
            return
        after = page[-1]["id"]
