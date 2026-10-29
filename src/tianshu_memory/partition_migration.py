"""Install an owner-neutral derived token index; authority remains in original records."""

from .domain import require
from .partition_index import installed, refresh
from .retrieval_migration import backup_before

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_recall_documents (
 group_id TEXT PRIMARY KEY, terms TEXT NOT NULL);
"""


def migrate(store, backup_path):
    with store.transaction() as db:
        if installed(db):
            return dict(memory_partition_schema=1, already_installed=True)
        require(
            dict(db.execute("SELECT key,value FROM metadata")).get("schema") == "3",
            "dependency_unavailable",
            503,
        )
        backup, guard = backup_before(store, db, backup_path)
        db.execute(SCHEMA)
        db.execute("INSERT INTO metadata VALUES ('memory_partition_schema','1')")
        after, count = "", 0
        while True:
            batch = db.execute(
                "SELECT id FROM groups WHERE state='active' AND id>? ORDER BY id LIMIT 256",
                (after,),
            ).fetchall()
            for row in batch:
                refresh(db, row[0])
                count += 1
            if len(batch) < 256:
                break
            after = batch[-1][0]
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return dict(
        memory_partition_schema=1, backup=str(backup), backup_guard=str(guard), indexed_groups=count
    )
