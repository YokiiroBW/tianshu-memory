"""Explicit backed-up extension of the existing jobs and source authority."""

from pathlib import Path

from .domain import parse_time, require
from .postgres import is_postgres

SCHEMA = """
CREATE TABLE IF NOT EXISTS observation_payloads (
 source_ref TEXT PRIMARY KEY REFERENCES observation_sources(source_ref), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_materials (
 id TEXT PRIMARY KEY, digest TEXT NOT NULL, frame TEXT NOT NULL, scope TEXT NOT NULL,
 source_key TEXT NOT NULL, kind TEXT NOT NULL, binding_version INTEGER NOT NULL,
 payload TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memory_material_frame ON memory_materials(frame,source_key);
CREATE INDEX IF NOT EXISTS memory_material_source ON memory_materials(source_key);
CREATE TABLE IF NOT EXISTS memory_material_aliases (
 source_key TEXT PRIMARY KEY REFERENCES sources(key), material_id TEXT NOT NULL REFERENCES memory_materials(id));
CREATE INDEX IF NOT EXISTS memory_material_alias_target ON memory_material_aliases(material_id);
CREATE TABLE IF NOT EXISTS memory_segments (
 segment_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
 digest TEXT NOT NULL, frame TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_segment_claims (
 material_id TEXT NOT NULL REFERENCES memory_materials(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(material_id,job_id));
CREATE TABLE IF NOT EXISTS memory_segment_items (
 group_id TEXT PRIMARY KEY REFERENCES groups(id), kind TEXT NOT NULL, state TEXT NOT NULL,
 retention TEXT NOT NULL, valid_until TEXT, effective_at TEXT NOT NULL,
 replaces TEXT NOT NULL DEFAULT '[]', observed_epoch DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS memory_observation_heads (
 owner_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, revision INTEGER NOT NULL);
"""
TABLES = frozenset(
    {
        "observation_payloads",
        "memory_materials",
        "memory_segments",
        "memory_segment_claims",
        "memory_segment_items",
        "memory_observation_heads",
        "memory_material_aliases",
    }
)


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_segment_schema'").fetchone()
    return row is not None and row[0] == "2"


def has_event_column(db):
    if is_postgres(db):
        return (
            db.native(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='turn_inputs' AND column_name='event'"
            ).fetchone()
            is not None
        )
    return "event" in {row[1] for row in db.execute("PRAGMA table_info(turn_inputs)")}


def compatible(db):
    """An installed journal must match the current layout and original-event column."""
    row = db.execute("SELECT value FROM metadata WHERE key='memory_segment_schema'").fetchone()
    return row is None or (row[0] == "2" and has_event_column(db))


def install(db):
    if is_postgres(db):
        db.native("ALTER TABLE turn_inputs ADD COLUMN IF NOT EXISTS event TEXT")
    elif "event" not in {row[1] for row in db.execute("PRAGMA table_info(turn_inputs)")}:
        db.execute("ALTER TABLE turn_inputs ADD COLUMN event TEXT")
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for table in sorted(TABLES):
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} "
                "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
            )
    db.execute("INSERT INTO metadata VALUES ('memory_segment_schema','2')")


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    require(
        not str(backup).startswith("\\\\")
        and backup not in {Path(store.path), store.recovery_path},
        "invalid_input",
        400,
    )
    with store.transaction() as db:
        marker = db.execute(
            "SELECT value FROM metadata WHERE key='memory_segment_schema'"
        ).fetchone()
        previous = marker[0] if marker else None
        require(previous in {None, "1", "2"}, "dependency_unavailable", 503)
        event_present = has_event_column(db)
        if previous == "2" and event_present:
            return dict(memory_segment_schema=2, already_installed=True)
        # Earlier PG imports copied the segment marker but omitted this column.
        # Only the explicit, backed-up migration may repair that known omission.
        if previous is not None and not event_present:
            require(is_postgres(db), "dependency_unavailable", 503)
        for key, value in (
            ("schema", "3"),
            ("memory_consolidation_schema", "1"),
            ("observation_schema", "1"),
        ):
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            require(row is not None and row[0] == value, "dependency_unavailable", 503)
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        store.backup(backup, db=db)
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as output:
            output.write(store.recovery_path.read_bytes())
        if previous is not None and not event_present:
            db.native("ALTER TABLE turn_inputs ADD COLUMN event TEXT")
        if previous == "1":
            columns = (
                "replaces TEXT NOT NULL DEFAULT '[]'",
                "observed_epoch DOUBLE PRECISION NOT NULL DEFAULT 0",
            )
            for column in columns:
                # PG import creates the current physical tables before copying
                # the prior metadata marker. Its columns may already exist.
                if is_postgres(db):
                    db.native("ALTER TABLE memory_segment_items ADD COLUMN IF NOT EXISTS " + column)
                else:
                    db.execute("ALTER TABLE memory_segment_items ADD COLUMN " + column)
            for row in db.execute(
                "SELECT group_id,effective_at FROM memory_segment_items"
            ).fetchall():
                db.execute(
                    "UPDATE memory_segment_items SET observed_epoch=? WHERE group_id=?",
                    (parse_time(row["effective_at"]).timestamp(), row["group_id"]),
                )
            db.execute("UPDATE metadata SET value='2' WHERE key='memory_segment_schema'")
        elif previous is None:
            install(db)
    result = dict(
        memory_segment_schema=2,
        backup=str(backup),
        backup_guard=str(guard),
        existing_records_preserved=True,
    )
    if previous is not None and not event_present:
        result["repaired_event_column"] = True
    if previous == "1":
        result["upgraded_from"] = 1
    return result
