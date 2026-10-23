"""Explicit, backed-up upgrade of sourced facts and rebuildable retrieval projections."""

import secrets
from pathlib import Path

from .domain import require
from .postgres import is_postgres

DERIVED = frozenset({"memory_documents", "memory_vectors", "memory_episode_index"})
TABLES = frozenset({"fact_subjects", *DERIVED})
SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_subjects (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, type TEXT NOT NULL,
 anchor TEXT NOT NULL, source_key TEXT NOT NULL REFERENCES sources(key));
CREATE INDEX IF NOT EXISTS fact_subject_anchor ON fact_subjects(scope,kind,type,anchor);
CREATE TABLE IF NOT EXISTS memory_documents (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, group_id TEXT,
 source_key TEXT, source_revision INTEGER, source_epoch INTEGER, content_hash TEXT NOT NULL,
 text TEXT NOT NULL, observed_at TEXT, span_start INTEGER NOT NULL, span_end INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS memory_document_scope ON memory_documents(scope,kind,id);
CREATE INDEX IF NOT EXISTS memory_document_group ON memory_documents(group_id);
CREATE INDEX IF NOT EXISTS memory_document_source ON memory_documents(source_key);
CREATE TABLE IF NOT EXISTS memory_vectors (
 document_id TEXT NOT NULL, content_hash TEXT NOT NULL, embedding_revision TEXT NOT NULL,
 dimension INTEGER NOT NULL, vector TEXT NOT NULL,
 PRIMARY KEY(document_id,content_hash,embedding_revision));
CREATE VIRTUAL TABLE IF NOT EXISTS memory_episode_index USING fts5(document_id UNINDEXED,scope UNINDEXED,text);
"""


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='automatic_fact_schema'").fetchone()
    return row is not None and row[0] == "3"


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    require(
        not str(backup).startswith("\\\\")
        and backup not in {Path(store.path), store.recovery_path},
        "invalid_input",
        400,
    )
    with store.transaction() as db:
        if installed(db):
            return {"automatic_fact_schema": 3, "already_installed": True}
        markers = dict(db.execute("SELECT key,value FROM metadata"))
        require(
            markers.get("schema") == "3"
            and markers.get("automatic_fact_schema") == "2"
            and markers.get("memory_context_schema") == "1",
            "dependency_unavailable",
            503,
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        store.backup(backup, db=db)
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as stream:
            stream.write(store.recovery_path.read_bytes())
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        if is_postgres(db):
            db.native("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS extraction_schema BIGINT")
            db.native(
                "CREATE INDEX IF NOT EXISTS memory_episode_index_terms ON memory_episode_index USING gin(to_tsvector('simple',text))"
            )
        elif "extraction_schema" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
            db.execute("ALTER TABLE jobs ADD COLUMN extraction_schema INTEGER")
        db.execute("UPDATE jobs SET extraction_schema=3 WHERE extraction_schema IS NULL")
        db.execute(
            "INSERT INTO metadata VALUES ('memory_selection_proof_key',?)", (secrets.token_hex(32),)
        )
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_fact_subjects_{action} AFTER {action} ON fact_subjects BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
            )
        db.execute("UPDATE metadata SET value='3' WHERE key='automatic_fact_schema'")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
        require(
            db.execute("PRAGMA foreign_key_check").fetchone() is None, "dependency_unavailable", 503
        )
    return {
        "automatic_fact_schema": 3,
        "backup": str(backup),
        "backup_guard": str(guard),
        "existing_records_preserved": True,
        "historical_backfill": False,
        "old_jobs_schema": 3,
    }
