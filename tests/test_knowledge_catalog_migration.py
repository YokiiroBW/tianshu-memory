"""The explicit catalog index migration, and the query plans those two indexes exist for.

Every case runs on its own isolated synthetic database under the test's own temporary directory.
Nothing here opens, copies or writes a real database, and the source-guard checkpoint is only ever
read or advanced through the already-integrated `Store` transaction: this suite proves what the
migration keeps, never that a production migration has happened.

The database an operator would upgrade was written by the *previous* release. This process only
has this release, so the pre-upgrade state is derived the way that release left it: import the
project knowledge schema, then remove the two catalog indexes and the catalog version row through
a plain SQLite connection — never through `Store`, which would (correctly) refuse an index change
made behind the source guard's back. The derivation belongs to the fixture; what is under test is
the upgrade from the state it leaves.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu_memory import knowledge_catalog_migration as catalog_migration
from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_catalog_migration import (
    EXPECTED,
    INDEXES,
    VERSION,
)
from tianshu_memory.knowledge_catalog_migration import inspect as inspect_catalog
from tianshu_memory.knowledge_catalog_migration import migrate as migrate_catalog
from tianshu_memory.knowledge_migration import migrate as migrate_knowledge
from tianshu_memory.store import Store

SECRET = "synthetic-catalog-migration-secret"
ALPHA = "alpha"
BETA = "beta"
GAMMA = "gamma"
DOCUMENTS = (
    "knowledge_projects",
    "knowledge_documents",
    "knowledge_versions",
    "knowledge_blocks",
    "knowledge_states",
    "knowledge_state_history",
    "knowledge_operations",
    "knowledge_imports",
)
PERMISSIONS = ["import", "query", "recover", "check", "write_state", "delete", "status"]
CATALOG_PERMISSIONS = ["document_list", "document_read"]


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def project(project_id, root):
    return {
        "root": str(root),
        "host": "local",
        "default_branch": "main",
        "urls": [f"https://example.com/{project_id}"],
    }


def document_id(project_id, kind, locator):
    return "document:" + fingerprint(["source", project_id, kind, locator])


def derive_pre_catalog(store):
    """Remove the catalog indexes and version row the way the previous release never had them.

    This runs on the database file with no `Store` transaction, which is exactly the error the
    source guard exists to catch: it is only ever done to a fixture, before the release under test
    has opened the database, and the guard is left describing the schema that is really there.
    """
    with closing(sqlite3.connect(store.path)) as plain:
        for name in INDEXES:
            plain.execute(f"DROP INDEX IF EXISTS {name}")
        plain.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
        plain.commit()
        plain.execute("VACUUM")
    with store.transaction() as db:
        assert inspect_catalog(db) == list(INDEXES)
        assert "knowledge_catalog_schema" not in dict(db.execute("SELECT key,value FROM metadata"))


def build(tmp_path, contracts, name="catalog"):
    """Import project knowledge, then derive the state the previous release left behind."""
    store = Store(tmp_path / f"{name}.sqlite")
    store.migrate_profiles(tmp_path / f"{name}-profiles.sqlite")
    store.migrate_sources(tmp_path / f"{name}-sources.sqlite", contracts)
    migrate_knowledge(store, tmp_path / f"{name}-knowledge.sqlite")
    derive_pre_catalog(store)
    return store


def register(store, *projects):
    """The project rows an import would have created, with their registered roots."""
    registered = {}
    for project_id in projects:
        root = Path(store.path).parent / f"{project_id}-root"
        root.mkdir(exist_ok=True)
        registered[project_id] = project(project_id, root)
        with store.transaction() as db:
            db.execute(
                "INSERT INTO knowledge_projects VALUES (?,?,0)",
                (project_id, canonical(registered[project_id])),
            )
    return registered


def configure(tmp_path, store, registered, *, name="catalogue", catalogue=True):
    """A private configuration authorizing one writer per registered project."""
    path = tmp_path / f"{name}-private.json"
    path.write_text(
        canonical(
            {
                "database_path": str(store.path),
                "knowledge": {
                    "projects": registered,
                    "clients": {
                        f"{project_id}-writer": {
                            "credential_sha256": digest(SECRET),
                            "projects": [project_id],
                            "permissions": [
                                *PERMISSIONS,
                                *(CATALOG_PERMISSIONS if catalogue else []),
                            ],
                        }
                        for project_id in registered
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def indexed_snapshot(store):
    with store.transaction() as db:
        return inspect_catalog(db)


def test_the_operator_command_upgrades_a_real_database_in_a_real_process(tmp_path, contracts):
    """The upgrade an operator runs, run as the command an operator types.

    Nothing is called in-process here: the child is `python -m tianshu_memory.knowledge_cli`, the
    same module the documented command names, and it is given only `--config` and `--backup`. The
    assertions are about what that process left on disk: the backup file, the two indexes, the one
    version row and — afterwards — the two operations answering through the public application.
    """
    text = "Evidence that must survive the operator's upgrade.\n"
    store = build(tmp_path, contracts)
    registered = register(store, ALPHA)
    seed(store, ALPHA, "kept.md", text)
    # The source the row claims is really there, because the operation under test re-reads it: a
    # read asserts this file still hashes to the version row, and it would be right to refuse a
    # document whose file was never written.
    (Path(registered[ALPHA]["root"]) / "kept.md").write_bytes(text.encode("utf-8"))
    path = configure(tmp_path, store, registered)
    backup = tmp_path / "operator-backup.sqlite"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianshu_memory.knowledge_cli",
            "--config",
            str(path),
            "migrate-catalog",
            "--backup",
            str(backup),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert process.stderr == ""
    reported = json.loads(process.stdout)
    assert reported == {
        "schema": 3,
        "knowledge_schema": 1,
        "knowledge_catalog_schema": 1,
        "indexes": list(INDEXES),
        "backup": str(backup.resolve()),
    }
    # The backup is the whole database from before the upgrade, and it opens as one.
    assert backup.exists()
    with closing(sqlite3.connect(backup)) as reader:
        assert inspect_catalog(reader) == list(INDEXES)
        assert "knowledge_catalog_schema" not in dict(
            reader.execute("SELECT key,value FROM metadata")
        )
        assert reader.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 1
    assert indexed_snapshot(store) == []
    # And the two operations now answer through the public application, reading the very document
    # the backup shows was already imported before the upgrade.
    application = KnowledgeApplication(path)
    listed = application.execute(
        {
            "operation": "document_list",
            "project_id": ALPHA,
            "arguments": {"limit": 8, "budget_bytes": 32768, "cursor": None},
        },
        client=f"{ALPHA}-writer",
        credential=SECRET,
    )
    assert [item["document_id"] for item in listed["items"]] == [
        document_id(ALPHA, "file", "kept.md")
    ]
    read = application.execute(
        {
            "operation": "document_read",
            "project_id": ALPHA,
            "arguments": {
                "document_id": document_id(ALPHA, "file", "kept.md"),
                "expected_version": 1,
                "expected_hash": None,
                "limit": 8,
                "budget_bytes": 32768,
                "cursor": None,
            },
        },
        client=f"{ALPHA}-writer",
        credential=SECRET,
    )
    assert read["blocks"] and read["version"] == 1


def test_the_operator_command_refuses_a_second_upgrade_without_touching_the_first(
    tmp_path, contracts
):
    """A database that already has the catalog is refused, by the command, with its own backup."""
    store = build(tmp_path, contracts)
    registered = register(store, ALPHA)
    path = configure(tmp_path, store, registered)
    first = tmp_path / "operator-first.sqlite"
    second = tmp_path / "operator-second.sqlite"
    command = [
        sys.executable,
        "-m",
        "tianshu_memory.knowledge_cli",
        "--config",
        str(path),
        "migrate-catalog",
    ]
    assert (
        subprocess.run(
            [*command, "--backup", str(first)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    again = subprocess.run(
        [*command, "--backup", str(second)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert again.returncode != 0
    # The command reports a failure as its own one-line body and never prints the raw exception: a
    # repeated upgrade is `dependency_or_input_error` to a caller, exactly like every other refusal
    # this operator command can produce.
    assert json.loads(again.stdout) == {"status": "failed", "code": "dependency_or_input_error"}
    assert again.stderr == ""
    # The refused run kept its own exclusive backup and added no second version row.
    assert second.exists()
    with store.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM metadata WHERE key='knowledge_catalog_schema'"
            ).fetchone()[0]
            == 1
        )
        assert inspect_catalog(db) == []


def seed(store, project_id, locator, text, *, version=1, kind="file"):
    """One imported document with a real version row and one real block, written by hand."""
    identity = document_id(project_id, kind, locator)
    recorded = text.encode("utf-8")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                project_id,
                "source:" + fingerprint([project_id, kind, locator]),
                kind,
                locator,
                version,
                "ready",
            ),
        )
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                version,
                hashlib.sha256(recorded).hexdigest(),
                recorded,
                text,
                "text/plain",
                canonical({"kind": kind, "locator": locator}),
            ),
        )
        db.execute(
            "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
            (
                f"{identity}:{version}:000",
                identity,
                version,
                canonical({"spans": [[1, 1]], "text": text}),
            ),
        )
    return identity


def seed_many(store, project_id, count, *, first_version=1, offset=0):
    """`count` documents of one project, each carrying `first_version` versions of its own."""
    identities = []
    with store.transaction() as db:
        for index in range(offset, offset + count):
            locator = f"note-{index:04d}.md"
            identity = document_id(project_id, "file", locator)
            identities.append(identity)
            db.execute(
                "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    project_id,
                    "source:" + fingerprint([project_id, "file", locator]),
                    "file",
                    locator,
                    first_version,
                    "ready",
                ),
            )
            for version in range(1, first_version + 1):
                text = f"{project_id} document {index:04d} version {version}"
                db.execute(
                    "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
                    (
                        identity,
                        version,
                        hashlib.sha256(text.encode()).hexdigest(),
                        text.encode(),
                        text,
                        "text/plain",
                        canonical({"kind": "file", "locator": locator}),
                    ),
                )
                db.execute(
                    "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
                    (
                        f"{identity}:{version}:000",
                        identity,
                        version,
                        canonical({"spans": [[1, 1]], "text": text}),
                    ),
                )
    return identities


def snapshot(store):
    """Every authority row this migration must not touch, plus the guard checkpoint."""
    with store.transaction() as db:
        tables = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in DOCUMENTS
        }
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        triggers = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    return SimpleNamespace(
        tables=tables,
        metadata=metadata,
        triggers=triggers,
        guard=Path(store.recovery_path).read_text(encoding="utf-8"),
    )


class Steps:
    """Counts the VM instructions one paged query really costs, bounded by SQLite itself."""

    def __init__(self, connection, limit=10_000_000):
        self.connection = connection
        self.limit = limit
        self.count = 0

    def __enter__(self):
        def handler():
            self.count += 1
            return 1 if self.count > self.limit else 0

        self.connection.set_progress_handler(handler, 1)
        return self

    def __exit__(self, *exc):
        self.connection.set_progress_handler(None, 0)
        return False


# Exactly the statements the catalogue executes, so the plans and the step counts below belong to
# the shipped pagination rather than to a paraphrase of it. The lower bound is always a string —
# the first page starts from `''` — and the index is named, which is what makes the keyset one
# index seek instead of a walk across every other project.
LIST_SQL = (
    "SELECT id,kind,version,state FROM knowledge_documents INDEXED BY "
    "knowledge_documents_project_id WHERE project_id=? AND id>? ORDER BY id LIMIT ?"
)
READ_SQL = (
    "SELECT b.id,b.document_id,b.version,b.payload,v.hash FROM knowledge_blocks b "
    "INDEXED BY knowledge_blocks_document_version_id "
    "JOIN knowledge_versions v ON v.document_id=b.document_id AND v.version=b.version "
    "WHERE b.document_id=? AND b.version=? AND b.id>? ORDER BY b.id LIMIT ?"
)


def plan(db, sql, parameters):
    return " | ".join(
        row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
    )


def backup_path(tmp_path, name):
    return tmp_path / f"{name}.sqlite"


# -- the explicit upgrade -------------------------------------------------------------------


def test_the_upgrade_installs_both_indexes_and_exactly_one_version_row(tmp_path, contracts):
    store = build(tmp_path, contracts)
    before = snapshot(store)
    assert before.metadata["knowledge_schema"] == "1"
    assert "knowledge_catalog_schema" not in before.metadata
    backup = backup_path(tmp_path, "before-catalog")
    assert migrate_catalog(store, backup) == {
        "schema": 3,
        "knowledge_schema": 1,
        "knowledge_catalog_schema": 1,
        "indexes": list(INDEXES),
        "backup": str(backup.resolve()),
    }
    with store.transaction() as db:
        assert inspect_catalog(db) == []
        rows = db.execute(
            "SELECT COUNT(*) FROM metadata WHERE key='knowledge_catalog_schema'"
        ).fetchone()[0]
        assert rows == 1
        assert (
            dict(db.execute("SELECT key,value FROM metadata"))["knowledge_catalog_schema"]
            == VERSION
        )
        # Exactly the shape the plans were fixed against, read back from `PRAGMA index_info`, and
        # the DDL is the one this module defines rather than something merely same-named.
        installed = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?)",
                INDEXES,
            )
        }
        for name, expected in EXPECTED.items():
            found = db.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            columns = tuple(
                row[2]
                for row in db.execute(
                    "SELECT * FROM pragma_index_info(?) ORDER BY seqno", (name,)
                ).fetchall()
            )
            assert (found[0], columns) == (expected["table"], expected["columns"]), name
    assert set(installed) == set(INDEXES)
    for statement in catalog_migration.SCHEMA:
        assert "".join(statement.split()) in ["".join(item.split()) for item in installed.values()]


def test_the_upgrade_moves_source_revision_once_and_keeps_the_guard_forward(tmp_path, contracts):
    store = build(tmp_path, contracts)
    before = snapshot(store)
    migrate_catalog(store, backup_path(tmp_path, "revision"))
    after = snapshot(store)
    assert int(after.metadata["source_revision"]) == int(before.metadata["source_revision"]) + 1, (
        "a schema change is one explicit source revision, not a trigger storm"
    )
    # The guard checkpoint moved forward with that revision instead of being dropped or rebuilt.
    assert after.guard != before.guard
    # No other metadata key appeared, and none of the ones that were already there changed.
    assert set(after.metadata) - set(before.metadata) == {"knowledge_catalog_schema"}
    assert {key: value for key, value in after.metadata.items() if key != "source_revision"} == {
        **{key: value for key, value in before.metadata.items() if key != "source_revision"},
        "knowledge_catalog_schema": VERSION,
    }


def test_the_backup_is_the_whole_pre_migration_database(tmp_path, contracts):
    store = build(tmp_path, contracts)
    register(store, ALPHA)
    seed(store, ALPHA, "kept.md", "Evidence that must survive the upgrade.")
    before = snapshot(store)
    backup = backup_path(tmp_path, "whole")
    migrate_catalog(store, backup)
    assert backup.exists() and backup.stat().st_size > 0
    # The backup was taken inside the migration transaction, so it holds the state *before* the two
    # indexes and the version row, and it opens and reads as an ordinary database.
    with closing(sqlite3.connect(backup)) as reader:
        assert inspect_catalog(reader) == list(INDEXES)
        metadata = dict(reader.execute("SELECT key,value FROM metadata"))
        assert "knowledge_catalog_schema" not in metadata
        assert metadata["source_revision"] == before.metadata["source_revision"]
        for table in DOCUMENTS:
            assert reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == len(
                before.tables[table]
            )


def test_the_upgrade_changes_no_authority_row_no_trigger_and_no_permission(tmp_path, contracts):
    store = build(tmp_path, contracts)
    register(store, ALPHA, BETA)
    seed(store, ALPHA, "a.md", "Alpha evidence.")
    seed(store, BETA, "b.md", "Beta evidence.")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO knowledge_states VALUES (?,?,?)",
            (ALPHA, 1, canonical({"goal": "keep the upgrade inert", "unfinished": []})),
        )
    before = snapshot(store)
    migrate_catalog(store, backup_path(tmp_path, "inert"))
    after = snapshot(store)
    assert after.tables == before.tables
    assert after.triggers == before.triggers
    assert set(after.metadata) - set(before.metadata) == {"knowledge_catalog_schema"}


def test_a_second_upgrade_is_refused_and_leaves_no_partial_schema(tmp_path, contracts):
    store = build(tmp_path, contracts)
    migrate_catalog(store, backup_path(tmp_path, "first"))
    second = backup_path(tmp_path, "second")
    with pytest.raises(ValueError, match="already applied"):
        migrate_catalog(store, second)
    # The refused attempt kept its own exclusive backup and added no second version row.
    assert second.exists()
    with store.transaction() as db:
        rows = db.execute(
            "SELECT COUNT(*) FROM metadata WHERE key='knowledge_catalog_schema'"
        ).fetchone()[0]
        assert rows == 1 and inspect_catalog(db) == []


def test_the_backup_must_be_a_new_distinct_local_file(tmp_path, contracts):
    store = build(tmp_path, contracts)
    with pytest.raises(ValueError, match="distinct local file"):
        migrate_catalog(store, store.path)
    with pytest.raises(ValueError, match="distinct local file"):
        migrate_catalog(store, store.recovery_path)
    with pytest.raises(ValueError, match="distinct local file"):
        migrate_catalog(store, r"\\server\share\catalog.sqlite")
    taken = backup_path(tmp_path, "taken")
    taken.write_bytes(b"a previous rollback artifact")
    with pytest.raises(FileExistsError):
        migrate_catalog(store, taken)
    assert taken.read_bytes() == b"a previous rollback artifact"
    assert indexed_snapshot(store) == list(INDEXES)


def test_a_missing_or_wrong_guard_refuses_the_upgrade(tmp_path, contracts):
    store = build(tmp_path, contracts)
    guard = Path(store.recovery_path)
    assert guard.exists()
    # A checkpoint that is not there at all is a recovery question, not something the migration
    # may answer by initializing one: the missing state is read first and fails closed. The store
    # reports every one of these as the same dependency failure and says which in its log, so the
    # code alone never tells a caller whether a checkpoint is missing or wrong.
    guard.unlink()
    missing = backup_path(tmp_path, "missing-guard")
    with pytest.raises(Fault, match="dependency_unavailable"):
        migrate_catalog(store, missing)
    assert missing.exists(), "the backup is taken before the schema question is asked"
    # A checkpoint that disagrees with the database is not repaired by the migration either. It
    # is a second database because the first can no longer be opened at all: that is exactly what
    # failing closed means here rather than recreating the checkpoint.
    tampered = build(tmp_path, contracts, name="tampered")
    Path(tampered.recovery_path).write_text(
        canonical({"schema": 3, "instance": "tampered", "revision": 0, "recovery": "tampered"}),
        encoding="utf-8",
    )
    with pytest.raises(Fault, match="dependency_unavailable"):
        migrate_catalog(tampered, backup_path(tmp_path, "wrong-guard"))
    # Neither database gained the catalog schema, and neither became readable.
    for broken in (store, tampered):
        with closing(sqlite3.connect(broken.path)) as plain:
            assert "knowledge_catalog_schema" not in dict(
                plain.execute("SELECT key,value FROM metadata")
            )


def test_a_database_without_project_knowledge_is_refused(tmp_path, contracts):
    bare = Store(tmp_path / "pristine.sqlite")
    bare.migrate_profiles(tmp_path / "pristine-profiles.sqlite")
    bare.migrate_sources(tmp_path / "pristine-sources.sqlite", contracts)
    with pytest.raises(ValueError, match="requires project knowledge schema 1"):
        migrate_catalog(bare, backup_path(tmp_path, "without-knowledge"))
    with bare.transaction() as db:
        assert "knowledge_catalog_schema" not in dict(db.execute("SELECT key,value FROM metadata"))
        assert inspect_catalog(db) == list(INDEXES)


def test_an_index_of_the_wrong_shape_is_rebuilt_rather_than_hidden(tmp_path, contracts):
    store = build(tmp_path, contracts)
    # First upgrade, so the guard checkpoint describes a schema this release accepted and what
    # follows is the drift the shape check exists for rather than a state the guard already keeps
    # anyone out of. The catalog row goes with it, exactly as an interrupted earlier attempt would
    # have left the file: the indexes are there, the record of them is not.
    migrate_catalog(store, backup_path(tmp_path, "wrong-shape-first"))
    with closing(sqlite3.connect(store.path)) as plain:
        plain.execute("DROP INDEX knowledge_documents_project_id")
        plain.execute("CREATE INDEX knowledge_documents_project_id ON knowledge_documents(id,kind)")
        plain.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
        plain.commit()
    assert indexed_snapshot(store) == ["knowledge_documents_project_id"]
    migrate_catalog(store, backup_path(tmp_path, "wrong-shape"))
    assert indexed_snapshot(store) == []
    with store.transaction() as db:
        columns = tuple(
            row[2]
            for row in db.execute(
                "SELECT * FROM pragma_index_info('knowledge_documents_project_id') ORDER BY seqno"
            ).fetchall()
        )
    assert columns == ("project_id", "id")


# -- the fresh-database path ----------------------------------------------------------------


def test_a_fresh_knowledge_migration_installs_the_same_indexes(tmp_path, contracts):
    fresh = Store(tmp_path / "fresh.sqlite")
    fresh.migrate_profiles(tmp_path / "fresh-profiles.sqlite")
    fresh.migrate_sources(tmp_path / "fresh-sources.sqlite", contracts)
    migrate_knowledge(fresh, backup_path(tmp_path, "fresh-knowledge"))
    upgraded = build(tmp_path, contracts, name="upgraded")
    migrate_catalog(upgraded, backup_path(tmp_path, "upgraded-backup"))
    with fresh.transaction() as db:
        assert inspect_catalog(db) == []
        assert dict(db.execute("SELECT key,value FROM metadata"))["knowledge_catalog_schema"] == "1"
        generated = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?)", INDEXES
            )
        }
    with upgraded.transaction() as db:
        installed = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?)", INDEXES
            )
        }
    assert generated == installed
    # A repeated install over a database that already has the catalog is refused by the explicit
    # upgrade rather than silently stacking a second version row on top of the first.
    existing = indexed_snapshot(fresh)
    assert existing == []
    with pytest.raises(ValueError, match="already applied"):
        migrate_catalog(fresh, backup_path(tmp_path, "fresh-again"))
    with fresh.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM metadata WHERE key='knowledge_catalog_schema'"
            ).fetchone()[0]
            == 1
        )
        assert inspect_catalog(db) == []


def test_the_new_operations_are_unavailable_before_the_upgrade_and_old_ones_are_not(
    tmp_path, contracts
):
    store = build(tmp_path, contracts)
    registered = register(store, ALPHA)
    (Path(registered[ALPHA]["root"]) / "source.md").write_text(
        "Unmigrated source material.\n", encoding="utf-8"
    )
    client = f"{ALPHA}-writer"

    def run(operation, arguments, *, name):
        path = configure(tmp_path, store, registered, name=name, catalogue=name != "without")
        return KnowledgeApplication(path).execute(
            dict(operation=operation, project_id=ALPHA, arguments=arguments),
            client=client,
            credential=SECRET,
        )

    imported = run(
        "import",
        {
            "key": "unmigrated",
            "kind": "file",
            "locator": "source.md",
            "expected_version": 0,
            "groups": None,
        },
        name="without",
    )
    assert imported["status"] == "imported"
    assert run("query", {"text": "unmigrated", "budget_bytes": 8192}, name="without")["blocks"]
    reads = (
        ("document_list", {"limit": 8, "budget_bytes": 32768, "cursor": None}),
        (
            "document_read",
            {
                "document_id": imported["document_id"],
                "expected_version": 1,
                "expected_hash": None,
                "limit": 8,
                "budget_bytes": 32768,
                "cursor": None,
            },
        ),
    )
    # An identity that holds neither new permission is refused on the permission, before anything
    # about the database is read: this is the existing exact-permission model, not a schema answer.
    for operation, arguments in reads:
        with pytest.raises(Fault, match="forbidden"):
            run(operation, arguments, name="without")
    # Granted the two permissions but on a database without the catalog, the refusal is the missing
    # dependency rather than anything the caller can fix by asking again.
    for operation, arguments in reads:
        with pytest.raises(Fault, match="dependency_unavailable"):
            run(operation, arguments, name="granted")
    assert run("query", {"text": "unmigrated", "budget_bytes": 8192}, name="granted")["blocks"]
    migrate_catalog(store, backup_path(tmp_path, "after-the-import"))
    listed = run(
        "document_list", {"limit": 8, "budget_bytes": 32768, "cursor": None}, name="granted"
    )
    assert [entry["document_id"] for entry in listed["items"]] == [imported["document_id"]]
    read = run(
        "document_read",
        {
            "document_id": imported["document_id"],
            "expected_version": listed["items"][0]["version"],
            "expected_hash": imported["hash"],
            "limit": 8,
            "budget_bytes": 32768,
            "cursor": None,
        },
        name="granted",
    )
    assert read["blocks"][0]["text"].strip() == "Unmigrated source material."


# -- the plans the indexes exist for --------------------------------------------------------


def test_the_pagination_plans_use_the_indexes_without_a_temporary_sort(tmp_path, contracts):
    store = build(tmp_path, contracts)
    register(store, ALPHA, BETA, GAMMA)
    identities = []
    for project_id in (ALPHA, BETA, GAMMA):
        identities.extend(seed_many(store, project_id, 40, first_version=3))
    migrate_catalog(store, backup_path(tmp_path, "planned"))
    with store.transaction() as db:
        listed = plan(db, LIST_SQL, (ALPHA, identities[0], 33))
        blocks = plan(db, READ_SQL, (identities[0], 3, "", 33))
    for text in (listed, blocks):
        assert "SCAN" not in text, text
        assert "TEMP B-TREE" not in text, text
    assert "knowledge_documents_project_id" in listed, listed
    # The keyset columns are part of the index search, not filters applied after it.
    assert "project_id=?" in listed and "id>?" in listed, listed
    assert "knowledge_blocks_document_version_id" in blocks, blocks
    assert "document_id=?" in blocks and "version=?" in blocks, blocks


def measured(db, sql, parameters):
    with Steps(db) as counted:
        db.execute(sql, parameters).fetchall()
    return counted.count


def all_documents(db):
    """The control query: exactly the columns and keyset a page uses, with no project filter.

    Its cost is what reading the whole table costs. The page under test must stay far below it,
    and must stay far below it *by a wider margin* as the database grows — which is the difference
    between a page that seeks into its own range and one that walks rows it then throws away.
    """
    total = 0
    for row in db.execute("SELECT DISTINCT project_id FROM knowledge_documents").fetchall():
        total += measured(db, LIST_SQL, (row[0], "", 100000))
    return total


def test_one_page_scans_only_its_own_candidates_as_the_tables_grow(tmp_path, contracts):
    """A page must not cost more because another project or another version exists.

    The counter counts SQLite's own VM instructions, so this compares the query rather than the
    machine: the same page is taken from a small database and from one with four times the
    documents and eight times the versions, all of it either in another project or in a history the
    page does not name. The control is the same read of the whole table through the same keyset: a
    page whose plan filtered `project_id` after the `id` index, or `version` after the `document_id`
    primary key, would close the gap between the page and that control instead of opening it.
    """
    store = build(tmp_path, contracts)
    register(store, ALPHA, BETA, GAMMA)
    small = seed_many(store, ALPHA, 40, first_version=1)
    for project_id in (BETA, GAMMA):
        seed_many(store, project_id, 40, first_version=1)
    migrate_catalog(store, backup_path(tmp_path, "scaled"))
    with store.transaction() as db:
        small_list = measured(db, LIST_SQL, (ALPHA, small[0], 33))
        small_read = measured(db, READ_SQL, (small[0], 1, "", 33))
        small_table = all_documents(db)
    assert small_list > 0 and small_read > 0 and small_table > 0
    # Four times the documents overall, eight times the versions overall.
    seed_many(store, ALPHA, 60, first_version=8, offset=100)
    for project_id in (BETA, GAMMA):
        seed_many(store, project_id, 60, first_version=8, offset=100)
    with store.transaction() as db:
        large_list = measured(db, LIST_SQL, (ALPHA, small[0], 33))
        large_read = measured(db, READ_SQL, (small[0], 1, "", 33))
        large_table = all_documents(db)
    assert small_list * 2 <= small_table, (
        f"one page already cost half of the whole table: {small_list} of {small_table}"
    )
    assert large_list * 4 <= large_table, (
        f"one page stopped being a small part of the table: {large_list} of {large_table}"
    )
    assert large_read * 8 <= large_table, (
        f"one document's version stopped being a small part of the table: {large_read} of "
        f"{large_table}"
    )
    # The table grew by roughly four times; the page did not, and its growth is nowhere near
    # proportional: a page that walked the project would have grown with it.
    assert large_list <= small_list * 3, (small_list, large_list)


def test_the_index_is_named_by_the_statement_and_required_before_it_runs(tmp_path, contracts):
    """The page's statement names its index, so the shape gate is what decides it can run.

    A planner left to itself may prefer the `id` primary key for a keyset that starts near the top
    of the table, and then walk every other project to find this project's rows: the statement
    names the index so the reviewed plan is the plan. The price of naming it is that the statement
    cannot run at all without that exact index, which is why the same shape check runs first inside
    the serving transaction — a database whose index went missing is refused as an unavailable
    dependency instead of falling back to a scan or failing as a SQL error.
    """
    store = build(tmp_path, contracts)
    register(store, ALPHA, BETA)
    seed_many(store, ALPHA, 40, first_version=3)
    seed_many(store, BETA, 40, first_version=3)
    migrate_catalog(store, backup_path(tmp_path, "named"))
    with store.transaction() as db:
        assert plan(db, LIST_SQL, (ALPHA, "", 33)) == (
            "SEARCH knowledge_documents USING INDEX knowledge_documents_project_id "
            "(project_id=? AND id>?)"
        )
        assert plan(db, READ_SQL, (ALPHA, 1, "", 33)).endswith(
            "SEARCH b USING INDEX knowledge_blocks_document_version_id "
            "(document_id=? AND version=? AND id>?)"
        )
        db.execute("DROP INDEX knowledge_documents_project_id")
        assert inspect_catalog(db) == ["knowledge_documents_project_id"]
        with pytest.raises(sqlite3.OperationalError, match="no such index"):
            db.execute(LIST_SQL, (ALPHA, "", 33)).fetchall()
    # The gate refuses the database in that state, and the shape check is what decides it: the
    # same check runs inside the serving transaction, before any page statement.
    with closing(sqlite3.connect(store.path)) as plain:
        plain.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
        plain.commit()
    migrate_catalog(store, backup_path(tmp_path, "named-again"))
    assert indexed_snapshot(store) == []
    with store.transaction() as db:
        assert db.execute(LIST_SQL, (ALPHA, "", 33)).fetchall()
