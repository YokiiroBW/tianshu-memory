"""Derived coverage statistics; a receipt still owns proof of completed work."""

from .domain import canonical, require
from .retrieval_migration import backup_before

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_target_rotation (
 scope TEXT PRIMARY KEY, person_id TEXT NOT NULL, last_covered_at TEXT NOT NULL,
 covered_count BIGINT NOT NULL);
"""


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_rotation_schema'").fetchone()
    return row is not None and row[0] == "1"


def migrate(store, backup_path):
    with store.transaction() as db:
        if installed(db):
            return dict(memory_rotation_schema=1, already_installed=True)
        require(
            dict(db.execute("SELECT key,value FROM metadata")).get("memory_segment_schema") == "2",
            "dependency_unavailable",
            503,
        )
        backup, guard = backup_before(store, db, backup_path)
        db.execute(SCHEMA)
        db.execute("INSERT INTO metadata VALUES ('memory_rotation_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return dict(memory_rotation_schema=1, backup=str(backup), backup_guard=str(guard))


def order(db, targets):
    if not targets or not installed(db):
        return targets
    scopes = [canonical(target["scope"]) for target in targets]
    covered = {
        row["scope"]: (row["covered_count"], row["last_covered_at"])
        for row in db.execute(
            "SELECT * FROM memory_target_rotation WHERE scope IN ("
            + ",".join("?" for _ in scopes)
            + ")",
            scopes,
        )
    }
    return sorted(
        targets,
        key=lambda target: (
            *covered.get(canonical(target["scope"]), (0, "")),
            canonical(target["scope"]),
        ),
    )


def covered(db, scope, now):
    if installed(db):
        db.execute(
            "INSERT INTO memory_target_rotation VALUES (?,?,?,1) ON CONFLICT(scope) "
            "DO UPDATE SET last_covered_at=excluded.last_covered_at,covered_count=memory_target_rotation.covered_count+1",
            (canonical(scope), scope["person_id"], now),
        )
