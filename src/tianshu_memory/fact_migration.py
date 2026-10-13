"""Explicit additive metadata migration; original records and authority remain intact."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .domain import require

SCHEMA = """
CREATE TABLE automatic_facts (
 group_id TEXT PRIMARY KEY REFERENCES groups(id), scope TEXT NOT NULL,
 fact_key TEXT NOT NULL, value_key TEXT NOT NULL, observed_at TEXT NOT NULL,
 payload TEXT NOT NULL, superseded_by TEXT REFERENCES groups(id));
CREATE INDEX automatic_fact_identity ON automatic_facts(scope,fact_key,value_key);
CREATE TABLE automatic_evidence (
 group_id TEXT NOT NULL REFERENCES groups(id), source_key TEXT NOT NULL REFERENCES sources(key),
 quote TEXT NOT NULL, PRIMARY KEY(group_id,source_key,quote));
"""
TABLES = ("automatic_facts", "automatic_evidence")


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='automatic_fact_schema'").fetchone()
    return row is not None and row[0] == "1"


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    with store.transaction() as db:
        require(
            dict(db.execute("SELECT key,value FROM metadata")).get("schema") == "3",
            "dependency_unavailable",
            503,
        )
        if installed(db):
            return {"automatic_fact_schema": 1, "already_installed": True}
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as target,
        ):
            reader.backup(target)
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as target:
            target.write(store.recovery_path.read_bytes())
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        for table in TABLES:
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} "
                    "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                    "WHERE key='source_revision'; END"
                )
        db.execute("INSERT INTO metadata VALUES ('automatic_fact_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
        require(
            db.execute("PRAGMA foreign_key_check").fetchone() is None, "dependency_unavailable", 503
        )
    return dict(
        automatic_fact_schema=1,
        backup=str(backup),
        backup_guard=str(guard),
        existing_records_preserved=True,
        historical_backfill=False,
    )
