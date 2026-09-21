"""Install the two catalog indexes a paginated document listing needs.

The catalog operations page a project's imported documents and one document's current blocks
by keyset. Without a composite index whose prefix is exactly the keyset, SQLite can only use
the `id` primary key and then *filter* `project_id`, `document_id` and `version`, so the cost
of one page would grow with every other project and every historical version in the table.
Two indexes fix that shape; nothing else about the database changes.

This module owns the whole catalog schema: its version constant, its two statements, its
installer and its explicit upgrade. `install` is shared by the explicit upgrade below and by
`knowledge_migration.migrate`, so a fresh database and an installed one get byte-identical
indexes from one reviewed definition.

No authority row is created here — an index is not data — so there is no new table to track
with source-revision triggers. The metadata version and one explicit `source_revision`
increment are written inside the caller's existing transaction, which is what makes the
already-integrated `Store` transaction update the schema 3 source-guard checkpoint. The guard
is never dropped, rebuilt or bypassed; a failed or interrupted commit keeps failing closed in
exactly the way the existing recovery review describes.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

VERSION = "1"
# The two indexes the pagination plans need, in the order their keysets are compared. The name
# is part of the schema: a later request checks both the name and the exact column order below.
INDEXES = (
    "knowledge_documents_project_id",
    "knowledge_blocks_document_version_id",
)
SCHEMA = (
    "CREATE INDEX knowledge_documents_project_id ON knowledge_documents(project_id,id)",
    "CREATE INDEX knowledge_blocks_document_version_id ON knowledge_blocks(document_id,version,id)",
)
# The exact shape each index must have, per `PRAGMA index_info`. A same-named index over other
# columns answers the schema gate but not the query plan, so it is refused rather than trusted.
EXPECTED = {
    "knowledge_documents_project_id": {
        "table": "knowledge_documents",
        "columns": ("project_id", "id"),
    },
    "knowledge_blocks_document_version_id": {
        "table": "knowledge_blocks",
        "columns": ("document_id", "version", "id"),
    },
}


def _indexed(db, name):
    """The table and ordered columns of one index, or None when it is not there at all."""
    found = db.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    if found is None:
        return None
    columns = tuple(
        row[2]
        for row in db.execute(
            "SELECT * FROM pragma_index_info(?) ORDER BY seqno", (name,)
        ).fetchall()
    )
    return {"table": found[0], "columns": columns}


def inspect(db):
    """Whether both catalog indexes exist with exactly the shape the plans use.

    Returns the list of problems, so a caller can report "the catalog is not installed" without
    ever reporting *which* index is wrong to a request. A missing index is a problem; so is a
    same-named index over the wrong table or the wrong column order, because that one would
    silently produce a worse plan than the card fixed rather than a visible error.
    """
    problems = []
    for name in INDEXES:
        found = _indexed(db, name)
        if found is None:
            problems.append(name)
        elif found != {"table": EXPECTED[name]["table"], "columns": EXPECTED[name]["columns"]}:
            problems.append(name)
    return problems


def install(db):
    """Create both indexes and record the catalog schema version.

    The caller owns the transaction and has already checked the metadata precondition. A
    same-named index is never hidden behind `IF NOT EXISTS`: it is dropped and rebuilt from
    this definition, because an index of the wrong shape that the version constant claims is
    installed would make every later request believe a plan it does not have.
    """
    for name in INDEXES:
        if _indexed(db, name) is not None:
            db.execute(f"DROP INDEX {name}")
    for statement in SCHEMA:
        db.execute(statement)
    problems = inspect(db)
    if problems:
        raise ValueError("Catalog indexes could not be installed as specified")
    db.execute("INSERT INTO metadata VALUES ('knowledge_catalog_schema',?)", (VERSION,))
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")


def _backup(store, backup_path):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation prevents accidental overwrite of a prior rollback artifact.
    with backup.open("xb"):
        pass
    return backup


def migrate(store, backup_path):
    """Explicitly upgrade an installed project knowledge database with the catalog indexes.

    Requires schema 3 with project knowledge installed and no catalog schema yet. Stop writers
    first; the complete pre-migration database is retained for rollback review. Nothing here
    changes a document, a version, a block, a project revision or any client permission.
    """
    backup = _backup(store, backup_path)
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or metadata.get("knowledge_schema") != "1":
            raise ValueError("Catalog migration requires project knowledge schema 1")
        if "knowledge_catalog_schema" in metadata:
            raise ValueError("Catalog migration already applied")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as destination,
        ):
            reader.backup(destination)
        install(db)
    return {
        "schema": 3,
        "knowledge_schema": 1,
        "knowledge_catalog_schema": 1,
        "indexes": list(INDEXES),
        "backup": str(backup),
    }
