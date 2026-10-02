"""Explicit indexes for source coverage and receipt identity, without rewriting authority."""

import sqlite3
from contextlib import closing
from pathlib import Path

from .domain import require


def install(db):
    db.execute(
        "CREATE INDEX admissions_receipt ON source_admissions("
        "json_extract(payload,'$.source.receipt_id')) WHERE payload IS NOT NULL"
    )
    db.execute("CREATE INDEX sources_scope ON sources(scope)")
    db.execute("INSERT INTO metadata VALUES ('source_lookup_schema','1')")
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")


def require_ready(db):
    row = db.execute("SELECT value FROM metadata WHERE key='source_lookup_schema'").fetchone()
    require(row is not None and row[0] == "1", "dependency_unavailable", 503)
    require(
        db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name IN ('admissions_receipt','sources_scope')"
        ).fetchone()[0]
        == 2,
        "dependency_unavailable",
        503,
    )


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or "source_lookup_schema" in metadata:
            raise ValueError(
                "Source lookup migration requires schema 3 without source_lookup_schema"
            )
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        install(db)
    return {"schema": 3, "source_lookup_schema": 1, "backup": str(backup)}
