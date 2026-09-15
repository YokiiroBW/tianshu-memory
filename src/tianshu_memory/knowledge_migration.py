"""Add project knowledge without modifying historical chat authority."""

import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from .knowledge_directories_migration import install as install_directories
from .lessons_migration import install as install_lessons

SCHEMA = """
CREATE TABLE knowledge_projects (id TEXT PRIMARY KEY, registration TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE knowledge_documents (id TEXT PRIMARY KEY, project_id TEXT NOT NULL
  REFERENCES knowledge_projects(id), source_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
  locator TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE knowledge_versions (document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
  version INTEGER NOT NULL, hash TEXT NOT NULL, raw BLOB NOT NULL, text TEXT NOT NULL,
  media_type TEXT NOT NULL, provenance TEXT NOT NULL, PRIMARY KEY(document_id,version));
CREATE TABLE knowledge_blocks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
  version INTEGER NOT NULL, payload TEXT NOT NULL,
  FOREIGN KEY(document_id,version) REFERENCES knowledge_versions(document_id,version));
CREATE TABLE knowledge_states (project_id TEXT PRIMARY KEY REFERENCES knowledge_projects(id),
  version INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE knowledge_state_history (project_id TEXT NOT NULL REFERENCES knowledge_projects(id),
  version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project_id,version));
CREATE TABLE knowledge_operations (client TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
  result TEXT NOT NULL, PRIMARY KEY(client,key));
CREATE TABLE knowledge_imports (client TEXT NOT NULL, key TEXT NOT NULL, project_id TEXT NOT NULL,
  status TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(client,key));
CREATE VIRTUAL TABLE knowledge_index USING fts5(block_id UNINDEXED, project_id UNINDEXED, text);
"""
TRACKED = (
    "knowledge_projects knowledge_documents knowledge_versions knowledge_blocks "
    "knowledge_states knowledge_state_history knowledge_operations knowledge_imports"
).split()


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or "knowledge_schema" in metadata:
            raise ValueError("Knowledge migration requires schema 3 without knowledge_schema")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as dest,
        ):
            reader.backup(dest)
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        for table in TRACKED:
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} "
                    f"ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                    "WHERE key='source_revision'; END"
                )
        db.execute("INSERT INTO metadata VALUES ('knowledge_schema','1')")
        db.execute("INSERT INTO metadata VALUES ('knowledge_seal_key',?)", (secrets.token_hex(32),))
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
        # The same reviewed step installs the lesson book, the promoted experience book and
        # the registered-directory plan book.
        install_lessons(db)
        install_directories(db)
    return {
        "schema": 3,
        "knowledge_schema": 1,
        "lessons_schema": 1,
        "knowledge_directories_schema": 1,
        "backup": str(backup),
    }
