"""Explicit additive metadata migration; original records and authority remain intact."""

from pathlib import Path

from .domain import require
from .postgres import is_postgres

SCHEMA = """
CREATE TABLE automatic_facts (
 group_id TEXT PRIMARY KEY REFERENCES groups(id), scope TEXT NOT NULL,
 fact_key TEXT NOT NULL, value_key TEXT NOT NULL, observed_at TEXT NOT NULL,
 payload TEXT NOT NULL, superseded_by TEXT REFERENCES groups(id));
CREATE INDEX automatic_fact_identity ON automatic_facts(scope,fact_key,value_key);
CREATE TABLE automatic_evidence (
 group_id TEXT NOT NULL REFERENCES groups(id), source_key TEXT NOT NULL REFERENCES sources(key),
 quote TEXT NOT NULL, source_text TEXT, PRIMARY KEY(group_id,source_key,quote));
"""
TABLES = ("automatic_facts", "automatic_evidence")


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='automatic_fact_schema'").fetchone()
    return row is not None and row[0] in {"1", "2"}


def source_text_installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='automatic_fact_schema'").fetchone()
    return row is not None and row[0] == "2"


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
        version = db.execute(
            "SELECT value FROM metadata WHERE key='automatic_fact_schema'"
        ).fetchone()
        require(version is None or version[0] in {"1", "2"}, "dependency_unavailable", 503)
        if version is not None and version[0] == "2":
            return {"automatic_fact_schema": 2, "already_installed": True}
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        store.backup(backup, db=db)
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as target:
            target.write(store.recovery_path.read_bytes())
        if version is None:
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
            db.execute("INSERT INTO metadata VALUES ('automatic_fact_schema','2')")
        else:
            if is_postgres(db):
                # Importing SQLite v1 into a new PG schema already creates this
                # nullable column, but its explicit logical migration is pending.
                db.native(
                    "ALTER TABLE automatic_evidence ADD COLUMN IF NOT EXISTS source_text TEXT"
                )
            elif "source_text" not in {
                row[1] for row in db.execute("PRAGMA table_info(automatic_evidence)")
            }:
                db.execute("ALTER TABLE automatic_evidence ADD COLUMN source_text TEXT")
            db.execute("UPDATE metadata SET value='2' WHERE key='automatic_fact_schema'")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
        require(
            db.execute("PRAGMA foreign_key_check").fetchone() is None, "dependency_unavailable", 503
        )
    return dict(
        automatic_fact_schema=2,
        backup=str(backup),
        backup_guard=str(guard),
        existing_records_preserved=True,
        historical_backfill=False,
    )
