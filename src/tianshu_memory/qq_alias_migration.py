"""Explicit guarded QQ display-name migration; never runs during normal startup."""

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
        if metadata.get("schema") != "3" or "qq_alias_schema" in metadata:
            raise ValueError("QQ alias migration requires guarded schema 3 without qq_alias_schema")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as dest,
        ):
            reader.backup(dest)
        db.execute(
            "CREATE TABLE qq_aliases (account_key TEXT NOT NULL REFERENCES accounts(account_key), kind TEXT NOT NULL, bot_id TEXT NOT NULL, group_id TEXT NOT NULL, value TEXT NOT NULL, observed_at TEXT NOT NULL, event_ref TEXT NOT NULL, PRIMARY KEY(account_key,kind,bot_id,group_id))"
        )
        db.execute(
            "CREATE TABLE qq_alias_events (event_ref TEXT PRIMARY KEY, digest TEXT NOT NULL)"
        )
        for table in ("qq_aliases", "qq_alias_events"):
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} "
                    "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
                )
        db.execute("INSERT INTO metadata VALUES ('qq_alias_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return {"schema": 3, "qq_alias_schema": 1, "backup": str(backup)}
