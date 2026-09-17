"""Add research notes and project decisions to an installed knowledge database.

A database that already carries the TS-080 project knowledge tables gains the new objects
only after a reviewed backup. A fresh database receives them in one step from
`knowledge_migration.migrate`, which shares this installer. Neither path can rebuild or
overwrite the schema 3 source-guard checkpoint, and the pre-migration database is retained
for rollback review.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from .research_notes_schema import RESEARCH_NOTES_SCHEMA_VERSION, SCHEMA, TRACKED


def _tracked_triggers(db, tables):
    for table in tables:
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} "
                f"ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='source_revision'; END"
            )


def install(db):
    """Create the research-note objects with source_revision triggers.

    The caller owns the transaction and has already checked the metadata precondition.
    """
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    _tracked_triggers(db, TRACKED)
    db.execute(
        "INSERT INTO metadata VALUES ('research_notes_schema',?)", (RESEARCH_NOTES_SCHEMA_VERSION,)
    )
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

    Requires schema 3 with project knowledge installed and no research-note schema. Stop
    writers first; the complete pre-migration database is retained for rollback review.
    """
    backup = _backup(store, backup_path)
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or metadata.get("knowledge_schema") != "1":
            raise ValueError("Research-note migration requires project knowledge schema 1")
        if "research_notes_schema" in metadata:
            raise ValueError("Research-note migration already applied")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        install(db)
    return {
        "schema": 3,
        "knowledge_schema": 1,
        "research_notes_schema": 1,
        "backup": str(backup),
    }
