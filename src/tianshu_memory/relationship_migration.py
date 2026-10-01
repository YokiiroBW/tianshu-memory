"""Explicit relationship addon installation; only a reviewed local DB is accepted."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .relationships.legacy import import_legacy, migration_report
from .relationships.policy import Policy
from .relationships.schema import install, installed


def migrate(store, backup_path, *, clock, policy=Policy()):
    """Writers are serialized by Store. Repeat installation never imports twice.

    Rollback needs this complete DB plus its matching source guard checkpoint;
    no live downgrade, production CLI, default target, or restore approval is added.
    """
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    with store.transaction() as db:
        if dict(db.execute("SELECT key,value FROM metadata")).get("schema") != "3":
            raise ValueError("Relationships require guarded schema 3")
        if installed(db):
            return {"already_installed": True, **migration_report(db)}
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        # Save the pre-migration checkpoint as part of this exact backup set.
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as file:
            file.write(store.recovery_path.read_bytes())
        install(db)
        result = import_legacy(db, clock(), policy)
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Relationship foreign key mismatch")
    return {"relationships_schema": 1, "backup": str(backup), "backup_guard": str(guard), **result}
