"""Explicit additive local user approval migration on guarded source schema 3."""

import sqlite3
from contextlib import closing
from pathlib import Path


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or "local_users_schema" in metadata:
            raise ValueError(
                "User approval migration requires source schema 3 without local_users_schema"
            )
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as dest,
        ):
            reader.backup(dest)
        db.execute(
            "CREATE TABLE profile_approval_authorities ("
            "ref TEXT PRIMARY KEY REFERENCES profile_approvals(ref), authority TEXT NOT NULL, "
            "draft TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"
        )
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_profile_approval_authorities_{action} "
                f"AFTER {action} ON profile_approval_authorities "
                "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
            )
        db.execute("INSERT INTO metadata VALUES ('local_users_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return {"schema": 3, "local_users_schema": 1, "backup": str(backup)}
