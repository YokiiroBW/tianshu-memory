"""Add project lessons to an installed knowledge database without touching chat authority.

The lesson book and the promoted experience book need their own explicit migration: a
database that already carries the TS-080 project knowledge tables gains the new objects
only after a reviewed backup. A fresh database receives both in one step from
`knowledge_migration.migrate`, which shares this installer. Neither path can rebuild or
overwrite the schema 3 source-guard checkpoint.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from .lessons_schema import LESSONS_SCHEMA_VERSION, SCHEMA, TRACKED


def _tracked_triggers(db, tables):
    for table in tables:
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} "
                f"ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='source_revision'; END"
            )


def install(db):
    """Create lessons/experience objects with source_revision triggers.

    The caller owns the transaction and has already checked the metadata precondition.
    """
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    _tracked_triggers(db, TRACKED)
    db.execute("INSERT INTO metadata VALUES ('lessons_schema',?)", (LESSONS_SCHEMA_VERSION,))
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
    """Explicitly upgrade an installed project knowledge database.

    Requires schema 3 with knowledge_schema 1 and no lessons_schema. Stop writers first; the
    complete pre-migration database is retained for rollback review.
    """
    backup = _backup(store, backup_path)
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or metadata.get("knowledge_schema") != "1":
            raise ValueError("Lessons migration requires project knowledge schema 1")
        if "lessons_schema" in metadata:
            raise ValueError("Lessons migration already applied")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        install(db)
    return {"schema": 3, "knowledge_schema": 1, "lessons_schema": 1, "backup": str(backup)}
