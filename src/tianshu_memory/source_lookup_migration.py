"""Explicit indexes for source coverage and receipt identity, without rewriting authority."""

from pathlib import Path

from .domain import require
from .postgres import INDEX_CATALOG, is_postgres

POSTGRES_READY = (
    "SELECT max(value) FILTER (WHERE key='source_lookup_schema') AS source_lookup_schema,"
    "max(value) FILTER (WHERE key='source_revision') AS source_revision,"
    "max(value) FILTER (WHERE key='source_recovery') AS source_recovery,"
    f"(SELECT count(*) FROM {INDEX_CATALOG} "
    "WHERE name IN ('admissions_receipt','sources_scope')) AS index_count "
    "FROM metadata WHERE key IN ('source_lookup_schema','source_revision','source_recovery')"
)


def install(db):
    db.execute(
        "CREATE INDEX admissions_receipt ON source_admissions("
        "json_extract(payload,'$.source.receipt_id')) WHERE payload IS NOT NULL"
    )
    db.execute("CREATE INDEX sources_scope ON sources(scope)")
    db.execute("INSERT INTO metadata VALUES ('source_lookup_schema','1')")
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")


def require_ready(db):
    if is_postgres(db):
        # Aggregate without GROUP BY always returns one row, including missing
        # markers. Every invocation gets its own current statement snapshot.
        row = db.native(POSTGRES_READY).fetchone()
        require(row["source_lookup_schema"] == "1", "dependency_unavailable", 503)
        require(row["index_count"] == 2, "dependency_unavailable", 503)
        return row
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
        store.backup(backup, db=db)
        install(db)
    return {"schema": 3, "source_lookup_schema": 1, "backup": str(backup)}
