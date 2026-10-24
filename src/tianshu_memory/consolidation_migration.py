"""Explicit upgrade for context summaries using Memory's existing authority store."""

from pathlib import Path

from .domain import require

TABLES = frozenset(
    {
        "memory_batches",
        "memory_summaries",
        "memory_summary_sources",
        "memory_portraits",
        "memory_source_lifecycle",
    }
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_batches (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
 window_id TEXT NOT NULL, manifest TEXT NOT NULL, portrait_input TEXT, portrait_basis TEXT NOT NULL,
 recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_summaries (
 group_id TEXT PRIMARY KEY REFERENCES groups(id), kind TEXT NOT NULL, scope TEXT NOT NULL,
 window_id TEXT NOT NULL, manifest TEXT NOT NULL, summary_refs TEXT NOT NULL,
 importance TEXT NOT NULL, recorded_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_summary_scope ON memory_summaries(scope,kind,recorded_at);
CREATE TABLE IF NOT EXISTS memory_summary_sources (
 group_id TEXT NOT NULL REFERENCES groups(id), source_key TEXT NOT NULL REFERENCES sources(key),
 revision INTEGER NOT NULL, epoch INTEGER NOT NULL, source_scope TEXT NOT NULL,
 purpose TEXT NOT NULL, PRIMARY KEY(group_id,source_key));
CREATE INDEX IF NOT EXISTS memory_summary_source ON memory_summary_sources(source_key);
CREATE TABLE IF NOT EXISTS memory_portraits (
 scope TEXT PRIMARY KEY, revision INTEGER NOT NULL, group_id TEXT NOT NULL REFERENCES groups(id));
CREATE TABLE IF NOT EXISTS memory_source_lifecycle (
 source_key TEXT NOT NULL REFERENCES sources(key), revision INTEGER NOT NULL,
 importance TEXT NOT NULL, recorded_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 PRIMARY KEY(source_key,revision));
"""


def installed(db):
    row = db.execute(
        "SELECT value FROM metadata WHERE key='memory_consolidation_schema'"
    ).fetchone()
    return row is not None and row[0] == "1"


def install(db):
    """Called only inside the existing stopped-writer, backed-up migration."""
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for table in sorted(TABLES):
        for action in ("INSERT", "UPDATE", "DELETE"):
            # The existing PostgreSQL adapter owns the equivalent native trigger.
            db.execute(
                f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} "
                "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
            )
    db.execute("INSERT INTO metadata VALUES ('memory_consolidation_schema','1')")
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    require(
        not str(backup).startswith("\\\\")
        and backup not in {Path(store.path), store.recovery_path},
        "invalid_input",
        400,
    )
    with store.transaction() as db:
        if installed(db):
            return {
                "automatic_fact_schema": 3,
                "memory_consolidation_schema": 1,
                "already_installed": True,
            }
        marker = db.execute(
            "SELECT value FROM metadata WHERE key='automatic_fact_schema'"
        ).fetchone()
        require(marker is not None and marker[0] == "3", "dependency_unavailable", 503)
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        store.backup(backup, db=db)
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as output:
            output.write(store.recovery_path.read_bytes())
        install(db)
    return {
        "automatic_fact_schema": 3,
        "memory_consolidation_schema": 1,
        "backup": str(backup),
        "backup_guard": str(guard),
        "existing_records_preserved": True,
    }
