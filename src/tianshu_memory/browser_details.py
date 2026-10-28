"""Read models over existing learning receipts and source-checked personal records."""

import json

from .consolidation_migration import installed as summaries_installed
from .domain import canonical, parse_time
from .segment_migration import installed as segments_installed
from .segment_sources import frame


def timeline_ready(db):
    return segments_installed(db) and summaries_installed(db)


def personal_rows(db, scope, last, limit, *, timeline):
    if not timeline:
        return db.execute(
            "SELECT * FROM groups WHERE scope=? AND state='active' AND id>? ORDER BY id LIMIT ?",
            (canonical(scope), last or "", limit),
        ).fetchall()
    epoch, identity = last or [0, ""]
    return db.execute(
        "SELECT g.*,COALESCE(i.observed_epoch,0) AS browser_epoch,"
        "s.kind AS summary_kind,s.expires_at,i.kind AS memory_kind,i.state AS memory_state,"
        "i.effective_at,i.valid_until FROM groups g "
        "LEFT JOIN memory_summaries s ON s.group_id=g.id "
        "LEFT JOIN memory_segment_items i ON i.group_id=g.id "
        "WHERE g.scope=? AND g.state='active' "
        "AND (?='' OR (COALESCE(i.observed_epoch,0),g.id)<(?,?)) "
        "ORDER BY COALESCE(i.observed_epoch,0) DESC,g.id DESC LIMIT ?",
        (canonical(scope), identity, epoch, identity, limit),
    ).fetchall()


def record_position(item):
    effective = (item.get("provenance") or {}).get("effective_at")
    return [parse_time(effective).timestamp() if effective else 0, item["semantic_group_id"]]


def review(service, rows, *, truncated):
    """A reading view of verified records, never a new summary or evidence source."""
    periods = {str(days): dict(count=0, items=[]) for days in (7, 30)}
    agreements, agreement_count, portrait = [], 0, None
    valid_count = 0
    now = service.clock()
    for row, item in rows:
        valid_count += 1
        if not row["summary_kind"] or parse_time(row["expires_at"]) <= now:
            continue
        text = "\n".join(unit["statement"] for unit in item["units"])
        excerpt = text.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        entry = dict(
            semantic_group_id=item["semantic_group_id"],
            text=excerpt,
            truncated=excerpt != text,
            effective_at=row["effective_at"],
            state=row["memory_state"],
            valid_until=row["valid_until"],
        )
        if row["summary_kind"] == "portrait":
            if portrait is None:
                portrait = entry
            continue
        age = now.timestamp() - row["browser_epoch"]
        for days, period in periods.items():
            if 0 <= age <= int(days) * 86400:
                period["count"] += 1
                if len(period["items"]) < 5:
                    period["items"].append(entry)
        if row["memory_kind"] == "agreement" and row["memory_state"] in {
            "considering",
            "confirmed",
            "unknown",
        }:
            agreement_count += 1
            if len(agreements) < 5:
                agreements.append(
                    dict(
                        entry,
                        overdue=bool(row["valid_until"] and parse_time(row["valid_until"]) < now),
                    )
                )
    return valid_count, dict(
        periods=periods,
        open_agreements=dict(count=agreement_count, items=agreements),
        portrait=portrait,
        truncated=truncated,
    )


def learning_progress(db, scope, *, limit=200):
    if not segments_installed(db):
        return None
    rows = db.execute(
        "SELECT m.id,EXISTS (SELECT 1 FROM memory_segment_claims c JOIN jobs j ON j.id=c.job_id "
        "WHERE c.material_id=m.id AND j.state='committed') AS completed "
        "FROM memory_materials m JOIN sources s ON s.key=m.source_key "
        "WHERE m.frame=? AND m.scope=? AND m.verified=1 AND s.scope=m.scope AND s.state='active' "
        "AND s.revision=CAST(json_extract(m.payload,'$.source_revision') AS BIGINT) "
        "AND s.epoch=CAST(json_extract(m.payload,'$.source_epoch') AS BIGINT) "
        "AND NOT EXISTS (SELECT 1 FROM suppression z WHERE z.source_key=s.key) "
        "ORDER BY m.source_key DESC,m.id DESC LIMIT ?",
        (canonical(frame(scope)), canonical(scope), limit + 1),
    ).fetchall()
    sampled = rows[:limit]
    completed = sum(bool(row["completed"]) for row in sampled)
    return dict(
        received=len(sampled),
        completed=completed,
        waiting=len(sampled) - completed,
        truncated=len(rows) > limit,
    )


def provenance(service, db, group_id, scope):
    if not summaries_installed(db):
        return None
    row = db.execute(
        "SELECT * FROM memory_summaries WHERE group_id=? AND scope=?",
        (group_id, canonical(scope)),
    ).fetchone()
    if row is None:
        return None
    manifest = json.loads(row["manifest"])
    result = dict(
        kind=row["kind"],
        subject_person_id=scope["person_id"],
        organized_at=row["recorded_at"],
        expires_at=row["expires_at"],
        expired=parse_time(row["expires_at"]) <= service.clock(),
        importance=row["importance"],
        effective_at=None,
        memory_kind=None,
        memory_state=None,
        retention=None,
        valid_until=None,
        replacement_count=0,
        related_memory_count=len(json.loads(row["summary_refs"])),
        source_count=db.execute(
            "SELECT COUNT(DISTINCT source_key) FROM lineage WHERE group_id=?", (group_id,)
        ).fetchone()[0],
        context_count=len(manifest.get("members", [])),
        source_excerpts=[],
        excerpts_truncated=False,
    )
    if not segments_installed(db):
        return result
    item = db.execute("SELECT * FROM memory_segment_items WHERE group_id=?", (group_id,)).fetchone()
    if item:
        result.update(
            effective_at=item["effective_at"],
            memory_kind=item["kind"],
            memory_state=item["state"],
            retention=item["retention"],
            valid_until=item["valid_until"],
            replacement_count=len(json.loads(item["replaces"])),
        )
    # Shared profiles carry no permission to expose anyone's original speech.
    # A personal view can quote only its exact owner's direct lineage, never
    # the surrounding conversation stored in the multi-person manifest.
    if scope["audience"] != "self_private":
        return result
    materials = db.execute(
        "SELECT m.id,m.payload FROM memory_materials m JOIN lineage l ON l.source_key=m.source_key "
        "AND l.revision=CAST(json_extract(m.payload,'$.source_revision') AS BIGINT) "
        "AND l.epoch=CAST(json_extract(m.payload,'$.source_epoch') AS BIGINT) "
        "WHERE l.group_id=? AND m.scope=? AND m.verified=1 ORDER BY m.created_at,m.id LIMIT 13",
        (group_id, canonical(scope)),
    ).fetchall()
    for material in materials[:12]:
        member = json.loads(material["payload"])["member"]
        if (
            member["source_scope"] != scope
            or member["role"] != "human"
            or member["speaker"].get("person_id") != scope["person_id"]
        ):
            continue
        text = member["text"]
        excerpt = text.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        result["source_excerpts"].append(
            dict(
                observed_at=member["observed_at"],
                text=excerpt,
                truncated=excerpt != text,
            )
        )
    result["excerpts_truncated"] = len(materials) > 12
    return result
