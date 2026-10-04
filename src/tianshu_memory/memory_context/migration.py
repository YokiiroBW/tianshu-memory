"""Add guarded continuity state without replacing existing people or source lineage."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..domain import fingerprint, require

SCHEMA = """
CREATE TABLE context_operations (
 service TEXT NOT NULL, scope TEXT NOT NULL, operation_id TEXT NOT NULL,
 digest TEXT NOT NULL, receipt TEXT NOT NULL, batch_ref TEXT, item_id TEXT NOT NULL,
 PRIMARY KEY(service,scope,operation_id));
CREATE INDEX context_batch_page ON context_operations(service,scope,batch_ref,operation_id);
CREATE TABLE context_applications (
 source_key TEXT NOT NULL REFERENCES sources(key), revision INTEGER NOT NULL,
 scope TEXT NOT NULL, target_key TEXT NOT NULL, operation_id TEXT NOT NULL,
 digest TEXT NOT NULL, group_id TEXT REFERENCES groups(id),
 PRIMARY KEY(source_key,revision,scope,target_key,operation_id));
CREATE INDEX context_application_content ON context_applications(scope,target_key,digest);
CREATE TABLE context_associations (
 id TEXT PRIMARY KEY, source_account TEXT NOT NULL, target_account TEXT NOT NULL,
 source_person TEXT NOT NULL REFERENCES people(id), target_person TEXT NOT NULL REFERENCES people(id),
 actor_id TEXT NOT NULL, scopes TEXT NOT NULL, source_binding INTEGER NOT NULL,
 target_binding INTEGER NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
 proof_ref TEXT NOT NULL UNIQUE);
CREATE INDEX context_association_source ON context_associations(actor_id,source_person,state,id);
CREATE INDEX context_association_target ON context_associations(actor_id,target_person,state,id);
CREATE TABLE context_identity_epochs (
 actor_id TEXT NOT NULL, person_id TEXT NOT NULL REFERENCES people(id), version INTEGER NOT NULL,
 PRIMARY KEY(actor_id,person_id));
"""
TABLES = (
    "context_operations",
    "context_applications",
    "context_associations",
    "context_identity_epochs",
)


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_context_schema'").fetchone()
    return row is not None and row[0] == "1"


def ready(db):
    require(installed(db), "dependency_unavailable", 503)


def target_key(draft):
    return fingerprint({k: draft.get(k) for k in ("category", "field_key", "item_key")})


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
            return {"memory_context_schema": 1, "already_installed": True}
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        guard = backup.with_name(backup.name + ".source-guard.json")
        with guard.open("xb") as output:
            output.write(store.recovery_path.read_bytes())
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        migrated = 0
        for row in db.execute("SELECT * FROM source_writes").fetchall():
            old = db.execute(
                "SELECT result,digest FROM write_ledger WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            # Historical rows without a provable output retain their original legacy ledger.
            if old is None:
                continue
            for group_id in json.loads(old["result"]).get("group_ids", []):
                group = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
                if group is None or group["scope"] != row["scope"]:
                    continue
                if (
                    db.execute(
                        "SELECT 1 FROM lineage WHERE group_id=? AND source_key=?",
                        (group_id, row["source_key"]),
                    ).fetchone()
                    is None
                ):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO context_applications VALUES (?,?,?,?,?,?,?)",
                    (
                        row["source_key"],
                        row["revision"],
                        row["scope"],
                        target_key(dict(group)),
                        row["job_id"],
                        old["digest"],
                        group_id,
                    ),
                )
                migrated += 1
        for table in TABLES:
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'; END"
                )
        db.execute("INSERT INTO metadata VALUES ('memory_context_schema','1')")
        # Additive DDL also changes authority. Old backups must not erase the new
        # ledger when no applications happened to be imported into its empty tables.
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Memory context foreign key mismatch")
    return {
        "memory_context_schema": 1,
        "backup": str(backup),
        "backup_guard": str(guard),
        "mapped_applications": migrated,
    }


def identity_version(db, scope):
    row = db.execute(
        "SELECT version FROM context_identity_epochs WHERE actor_id=? AND person_id=?",
        (scope["actor_id"], scope["person_id"]),
    ).fetchone()
    return row[0] if row else 1


def bump_identity(db, actor, person):
    db.execute(
        "INSERT INTO context_identity_epochs VALUES (?,?,2) ON CONFLICT(actor_id,person_id) DO UPDATE SET version=version+1",
        (actor, person),
    )


def semantic_payload(request):
    return {k: v for k, v in request.items() if k not in {"query", "command", "proof_ref"}}
