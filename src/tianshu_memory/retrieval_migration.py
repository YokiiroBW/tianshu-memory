"""Additive, explicitly backed-up retrieval storage upgrade."""

import json
from pathlib import Path

from .domain import Fault, require
from .postgres import is_postgres
from .retrieval_vectors import pack, vectors


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_retrieval_schema'").fetchone()
    return row is not None and row[0] == "1"


def backup_before(store, db, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    store.backup(backup, db=db)
    guard = backup.with_name(backup.name + ".source-guard.json")
    with guard.open("xb") as output:
        output.write(store.recovery_path.read_bytes())
    return backup, guard


def migrate(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    with store.transaction() as db:
        markers = dict(db.execute("SELECT key,value FROM metadata"))
        require(
            markers.get("schema") == "3" and markers.get("automatic_fact_schema") == "3",
            "dependency_unavailable",
            503,
        )
        if installed(db):
            return {"memory_retrieval_schema": 1, "already_installed": True}
        backup, guard = backup_before(store, db, backup_path)
        if is_postgres(db):
            db.native("ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS vector_bytes BYTEA")
        elif "vector_bytes" not in {
            row[1] for row in db.execute("PRAGMA table_info(memory_vectors)")
        }:
            db.execute("ALTER TABLE memory_vectors ADD COLUMN vector_bytes BLOB")
        converted = 0
        offset = 0
        while True:
            batch = db.execute(
                "SELECT document_id,content_hash,embedding_revision,vector FROM memory_vectors "
                "ORDER BY document_id,content_hash,embedding_revision LIMIT 256 OFFSET ?",
                (offset,),
            ).fetchall()
            for row in batch:
                try:
                    value = pack(vectors([json.loads(row["vector"])], 1)[0])
                except (Fault, ValueError, TypeError):
                    continue
                db.execute(
                    "UPDATE memory_vectors SET vector_bytes=? WHERE document_id=? AND content_hash=? AND embedding_revision=?",
                    (value, row["document_id"], row["content_hash"], row["embedding_revision"]),
                )
                converted += 1
            if len(batch) < 256:
                break
            offset += len(batch)
        db.execute("INSERT INTO metadata VALUES ('memory_retrieval_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return dict(
        memory_retrieval_schema=1,
        backup=str(backup),
        backup_guard=str(guard),
        vectors_converted=converted,
    )


def rollback_binary(store, backup_path):
    """Restore the five-column layout required by pre-upgrade INSERT ... VALUES writers.

    JSON contains the complete normalized vectors; the float32 copy is rebuildable.
    Preserve a paired backup and advance the existing guard transaction, never restore
    an older authority snapshot or edit its checkpoint by hand.
    """
    with store.transaction() as db:
        if not installed(db):
            return dict(memory_retrieval_schema=0, already_rolled_back=True)
        backup, guard = backup_before(store, db, backup_path)
        db.execute("ALTER TABLE memory_vectors DROP COLUMN vector_bytes")
        db.execute("DELETE FROM metadata WHERE key='memory_retrieval_schema'")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return dict(
        memory_retrieval_schema=0,
        backup=str(backup),
        backup_guard=str(guard),
        json_vectors_preserved=True,
    )
