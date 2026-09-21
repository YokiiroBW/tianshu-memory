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

import re
import sqlite3
from contextlib import closing
from pathlib import Path

VERSION = "1"
# The two indexes the pagination plans need, in the order their keysets are compared. The name
# is part of the schema: a later request checks both the name and the exact shape below.
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
# The statement each index must have been created by, with only formatting differences forgiven.
# `PRAGMA index_info` names the columns but not what makes them answer — a collation, a partial
# `WHERE`, an expression, a different sort order — and `PRAGMA index_list` reports uniqueness and
# partiality but nothing about the columns. The reviewed statement is the one text that pins all of
# them at once, so a same-named index is accepted only when it is the same declaration.
STATEMENT = {name: statement for name, statement in zip(INDEXES, SCHEMA, strict=True)}


def _statement(sql):
    """One index declaration with formatting removed, for comparison only.

    Two declarations that differ only in spacing — around parentheses, after commas, at the start
    of a line — describe the same index, and a database whose index was written out by hand or by
    another tool must not be refused for that. Everything that changes what the index answers
    stays: the columns, their order, any collation or expression, `UNIQUE`, and a partial `WHERE`.
    """
    collapsed = re.sub(r"\s+", " ", (sql or "").strip())
    return re.sub(r"\s*([(),])\s*", r"\1", collapsed).casefold()


def _indexed(db, name):
    """The full shape of one index as it really exists, or None when it is not there at all."""
    found = db.execute(
        "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    if found is None:
        return None
    # `PRAGMA index_list` carries the flags: a unique or partial index answers the same name and
    # columns while answering a different question, so both are read here rather than assumed.
    listing = db.execute(
        "SELECT * FROM pragma_index_list(?)", (EXPECTED[name]["table"],)
    ).fetchall()
    flags = next((row for row in listing if row[1] == name), None)
    columns = tuple(
        row[2] for row in db.execute("SELECT * FROM pragma_index_info(?) ORDER BY seqno", (name,))
    )
    return {
        "table": found[0],
        "columns": columns,
        "statement": _statement(found[1]),
        "unique": bool(flags[2]) if flags is not None else None,
        "partial": bool(flags[4]) if flags is not None else None,
    }


def _expected(name):
    return {
        "table": EXPECTED[name]["table"],
        "columns": EXPECTED[name]["columns"],
        "statement": _statement(STATEMENT[name]),
        "unique": False,
        "partial": False,
    }


def inspect(db):
    """Whether both catalog indexes exist with exactly the shape the plans use.

    Returns the list of problems, so a caller can report "the catalog is not installed" without
    ever reporting *which* index is wrong to a request. A missing index is a problem; so is a
    same-named index over the wrong table, the wrong column order or with different index flags,
    because that one would silently produce a worse plan than the card fixed rather than a visible
    error.
    """
    return [name for name in INDEXES if _indexed(db, name) != _expected(name)]


def conflicts(db):
    """The same-named indexes that exist now but are not the reviewed ones.

    Split out from `inspect` so an explicit upgrade can tell the two situations apart and refuse
    the conflicting one *before* it writes anything. A missing index is work to do; an index of the
    wrong shape is a database whose schema disagrees with this release, and no automatic repair of
    it is authorized.
    """
    existing = {name: _indexed(db, name) for name in INDEXES}
    return [
        name for name, found in existing.items() if found is not None and found != _expected(name)
    ]


def install(db):
    """Create the missing catalog indexes and record the catalog schema version.

    The caller owns the transaction and has already checked the metadata precondition *and* that no
    conflicting index exists: a same-named index of the wrong shape is never dropped, replaced or
    hidden behind `IF NOT EXISTS` here, because silently rewriting an index this release does not
    recognize would be repairing an unknown schema. An index of the correct shape is left exactly
    where it is — it is already the index the plans were reviewed against, and re-creating it would
    discard and rebuild it for nothing.
    """
    for name in INDEXES:
        if _indexed(db, name) is None:
            db.execute(STATEMENT[name])
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

    A same-named index that is not the reviewed one refuses the whole upgrade before the first
    write: this release does not recognize that schema, and dropping and re-creating it would be
    repairing a database nobody authorized it to repair. The refusal keeps the existing index, the
    metadata, the source revision and the guard checkpoint exactly as they were.
    """
    backup = _backup(store, backup_path)
    with store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != "3" or metadata.get("knowledge_schema") != "1":
            raise ValueError("Catalog migration requires project knowledge schema 1")
        if "knowledge_catalog_schema" in metadata:
            raise ValueError("Catalog migration already applied")
        conflicted = conflicts(db)
        if conflicted:
            raise ValueError(
                "Catalog index already exists with a different definition: " + ", ".join(conflicted)
            )
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
