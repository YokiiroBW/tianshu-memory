"""High similarity is a review hint, never permission to replace or delete a memory."""

import json
from datetime import UTC, datetime

from .domain import Fault, canonical, require, utc
from .retrieval_migration import backup_before
from .retrieval_vectors import dot, unpack, vectors

THRESHOLD = 0.92
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_merge_candidates (
 new_group_id TEXT NOT NULL, prior_group_id TEXT NOT NULL, embedding_revision TEXT NOT NULL,
 new_hash TEXT NOT NULL, prior_hash TEXT NOT NULL, score DOUBLE PRECISION NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(new_group_id,prior_group_id,embedding_revision));
CREATE INDEX IF NOT EXISTS memory_merge_prior ON memory_merge_candidates(prior_group_id,new_group_id);
"""


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_merge_schema'").fetchone()
    return row is not None and row[0] == "1"


def migrate(store, backup_path):
    with store.transaction() as db:
        if installed(db):
            return dict(memory_merge_schema=1, already_installed=True)
        require(
            dict(db.execute("SELECT key,value FROM metadata")).get("memory_retrieval_schema")
            == "1",
            "dependency_unavailable",
            503,
        )
        backup, guard = backup_before(store, db, backup_path)
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("INSERT INTO metadata VALUES ('memory_merge_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return dict(memory_merge_schema=1, backup=str(backup), backup_guard=str(guard))


def _save(db, document, candidates):
    for score, prior, revision in sorted(candidates, key=lambda row: (-row[0], row[1]["group_id"]))[
        :5
    ]:
        if score <= THRESHOLD:
            continue
        db.execute(
            "INSERT INTO memory_merge_candidates VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(new_group_id,prior_group_id,embedding_revision) DO UPDATE SET "
            "new_hash=excluded.new_hash,prior_hash=excluded.prior_hash,score=excluded.score,created_at=excluded.created_at",
            (
                document["group_id"],
                prior["group_id"],
                revision,
                document["content_hash"],
                prior["content_hash"],
                min(1.0, score),
                utc(datetime.now(UTC)),
            ),
        )


def mark_exact(db, group_id):
    if not installed(db):
        return
    document = db.execute("SELECT * FROM memory_documents WHERE group_id=?", (group_id,)).fetchone()
    if document is None:
        return
    rows = db.execute(
        "SELECT d.*,v.embedding_revision FROM memory_documents d JOIN groups g ON g.id=d.group_id "
        "JOIN memory_vectors v ON v.document_id=d.id AND v.content_hash=d.content_hash "
        "WHERE d.scope=? AND d.group_id!=? AND d.text=? AND g.state='active' ORDER BY d.group_id LIMIT 5",
        (document["scope"], group_id, document["text"]),
    )
    _save(db, document, [(1.0, row, row["embedding_revision"]) for row in rows])


def mark_vector(db, document, revision, value):
    if document.get("kind") == "episode" or not document.get("group_id") or not installed(db):
        return
    rows = db.execute(
        "SELECT d.*,v.vector,v.vector_bytes FROM memory_documents d JOIN groups g ON g.id=d.group_id "
        "JOIN memory_vectors v ON v.document_id=d.id AND v.content_hash=d.content_hash "
        "WHERE d.scope=? AND d.group_id!=? AND v.embedding_revision=? AND g.state='active'",
        (document["scope"], document["group_id"], revision),
    )
    candidates = []
    for row in rows:
        try:
            prior = (
                unpack(row["vector_bytes"])
                if row["vector_bytes"] is not None
                else vectors([json.loads(row["vector"])], 1)[0]
            )
            if len(prior) == len(value):
                candidates.append((dot(value, prior), row, revision))
        except (Fault, ValueError, TypeError):
            continue
    _save(db, document, candidates)


def for_window(service, db, manifest):
    if not installed(db):
        return []
    result = []
    for target in manifest["targets"]:
        by_group = {}
        for known in target["known_memories"]:
            row = db.execute(
                "SELECT r.group_id,r.version,r.state,g.scope,g.state AS group_state FROM records r "
                "JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                (known["record_id"],),
            ).fetchone()
            if (
                row is not None
                and row["version"] == known["record_version"]
                and row["state"] == "active"
                and row["group_state"] == "active"
                and row["scope"] == canonical(target["scope"])
                and service._group_current(db, row["group_id"])
            ):
                by_group[row["group_id"]] = {
                    key: known[key] for key in ("record_id", "record_version")
                }
        for group, ref in by_group.items():
            for row in db.execute(
                "SELECT c.* FROM memory_merge_candidates c "
                "JOIN memory_documents n ON n.group_id=c.new_group_id AND n.content_hash=c.new_hash "
                "JOIN memory_documents p ON p.group_id=c.prior_group_id AND p.content_hash=c.prior_hash "
                "WHERE c.new_group_id=? AND c.score>? ORDER BY c.score DESC,c.prior_group_id LIMIT 5",
                (group, THRESHOLD),
            ):
                if row["prior_group_id"] in by_group:
                    result.append(
                        dict(
                            subject_person_id=target["scope"]["person_id"],
                            new=ref,
                            prior=by_group[row["prior_group_id"]],
                            score=row["score"],
                        )
                    )
    return result[:40]
