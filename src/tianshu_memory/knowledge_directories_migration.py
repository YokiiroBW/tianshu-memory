"""Add registered-directory import plans to an installed knowledge database.

The plan book needs its own explicit migration: a database that already carries the TS-080
project knowledge tables gains the new objects only after a reviewed backup. A fresh database
receives them in the same step as knowledge and lessons from `knowledge_migration.migrate`,
which shares this installer. Neither path can rebuild or overwrite the schema 3 source-guard
checkpoint.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from .knowledge_directories_schema import SCHEMA, TRACKED, VERSION


def _tracked_triggers(db, tables):
    for table in tables:
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} "
                f"ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='source_revision'; END"
            )


def install(db):
    """Create the plan book with source_revision triggers.

    The caller owns the transaction and has already checked the metadata precondition.
    """
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    _tracked_triggers(db, TRACKED)
    db.execute("INSERT INTO metadata VALUES ('knowledge_directories_schema',?)", (VERSION,))
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")


def _backup(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    return backup


def migrate(store, backup_path):
    """Explicitly upgrade an installed project knowledge database with the plan book.

    Requires schema 3 with knowledge_schema 1 and no knowledge_directories_schema. Stop
    writers first; the complete pre-migration database is retained for rollback review.
    """
    backup = _backup(store, backup_path)
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or metadata.get("knowledge_schema") != "1":
            raise ValueError("Directory migration requires project knowledge schema 1")
        if "knowledge_directories_schema" in metadata:
            raise ValueError("Directory migration already applied")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        install(db)
    return {
        "schema": 3,
        "knowledge_schema": 1,
        "knowledge_directories_schema": 1,
        "backup": str(backup),
    }
