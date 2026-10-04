"""Explicit guarded migration: Knowledge originals gain real actor/person owners."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .source_recovery import checkpoint, persist

SCHEMA = """
CREATE TABLE knowledge_content_documents (document_id TEXT PRIMARY KEY REFERENCES knowledge_documents(id),
 owner TEXT NOT NULL, metadata TEXT NOT NULL, access_version INTEGER NOT NULL);
CREATE TABLE knowledge_content_grants (document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 reader TEXT NOT NULL, version INTEGER NOT NULL, hash TEXT NOT NULL, enabled INTEGER NOT NULL,
 PRIMARY KEY(document_id,reader));
CREATE TABLE knowledge_content_uploads (id TEXT PRIMARY KEY, owner TEXT NOT NULL, descriptor TEXT NOT NULL,
 raw BLOB, state TEXT NOT NULL, expires_at TEXT NOT NULL, document_id TEXT REFERENCES knowledge_documents(id));
CREATE TABLE knowledge_content_operations (owner TEXT NOT NULL, operation TEXT NOT NULL, request_id TEXT NOT NULL,
 digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(owner,operation,request_id));
"""
TRACKED = (
    "knowledge_content_documents",
    "knowledge_content_grants",
    "knowledge_content_uploads",
    "knowledge_content_operations",
)


def ready(db):
    row = db.execute("SELECT value FROM metadata WHERE key='knowledge_content_schema'").fetchone()
    if row is None or row[0] != "1":
        raise ValueError("Explicit knowledge content migration required")


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if (
            metadata.get("schema") != "3"
            or metadata.get("knowledge_schema") != "1"
            or "knowledge_content_schema" in metadata
        ):
            raise ValueError("Content migration requires schema 3 project Knowledge")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as dest,
        ):
            reader.backup(dest)
        guard_backup = Path(str(backup) + ".source-guard.json")
        # The writer lock and Store's verified checkpoint bind both copies to exactly the
        # same pre-upgrade state. Never derive a new guard from an unverified restored DB.
        persist(guard_backup, checkpoint(db), initialize=True)
        # NULL project is a real non-project original, not a fabricated project registration.
        # Rebuild the three-table original graph child-first with foreign keys always enabled.
        # Merely dropping/recreating a populated parent leaves SQLite's deferred FK counter
        # nonzero even when foreign_key_check sees no remaining broken reference.
        db.execute(
            "CREATE TABLE knowledge_documents_content (id TEXT PRIMARY KEY, project_id TEXT REFERENCES knowledge_projects(id), source_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, locator TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL)"
        )
        db.execute("INSERT INTO knowledge_documents_content SELECT * FROM knowledge_documents")
        for table, parent in (
            ("knowledge_versions", "knowledge_documents"),
            ("knowledge_blocks", "knowledge_versions"),
        ):
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            db.execute(
                sql.replace(
                    "CREATE TABLE " + table, "CREATE TABLE " + table + "_content", 1
                ).replace("REFERENCES " + parent, "REFERENCES " + parent + "_content")
            )
            db.execute(f"INSERT INTO {table}_content SELECT * FROM {table}")
        db.execute("DROP TABLE knowledge_blocks")
        db.execute("DROP TABLE knowledge_versions")
        db.execute("DROP TABLE knowledge_documents")
        db.execute("ALTER TABLE knowledge_documents_content RENAME TO knowledge_documents")
        db.execute("ALTER TABLE knowledge_versions_content RENAME TO knowledge_versions")
        db.execute("ALTER TABLE knowledge_blocks_content RENAME TO knowledge_blocks")
        db.execute(
            "CREATE INDEX knowledge_documents_project_id ON knowledge_documents(project_id,id)"
        )
        db.execute(
            "CREATE INDEX knowledge_blocks_document_version_id ON knowledge_blocks(document_id,version,id)"
        )
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        for table in ("knowledge_documents", "knowledge_versions", "knowledge_blocks", *TRACKED):
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
                )
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Knowledge original migration broke a reference")
        db.execute("INSERT INTO metadata VALUES ('knowledge_content_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return {
        "schema": 3,
        "knowledge_content_schema": 1,
        "backup": str(backup),
        "guard_backup": str(guard_backup),
    }
