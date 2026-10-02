"""Additive source indexes retain authority and the independent recovery checkpoint."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_source_migration import prepare

from tianshu_memory.domain import Fault
from tianshu_memory.source_lookup_migration import require_ready
from tianshu_memory.store import Store


def old_guarded_store(h):
    contracts, _, _, _ = prepare(h)
    h.store.migrate_sources(h.directory / "before-sources.sqlite", contracts)
    with h.store.transaction() as db:
        db.execute("DROP INDEX admissions_receipt")
        db.execute("DROP INDEX sources_scope")
        db.execute("DELETE FROM metadata WHERE key='source_lookup_schema'")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return h.store


def authorities(db):
    return {
        name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
        for name in (
            "sources",
            "source_admissions",
            "physical_sources",
            "suppression",
            "owner_heads",
            "lineage",
            "source_writes",
            "groups",
            "records",
            "jobs",
        )
    }


def test_additive_indexes_keep_null_legacy_receipts_backup_and_guard(h):
    store = old_guarded_store(h)
    with store.transaction() as db:
        before = authorities(db)
        revision = int(
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        )
        with pytest.raises(Fault, match="dependency_unavailable"):
            require_ready(db)
    backup = h.directory / "before-lookup.sqlite"
    result = store.migrate_source_lookup(backup)
    assert result["source_lookup_schema"] == 1
    with Store(store.path).transaction() as db:
        require_ready(db)
        assert authorities(db) == before
        assert (
            int(db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0])
            == revision + 1
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    guard = json.loads(store.recovery_path.read_text(encoding="utf-8"))
    assert guard["revision"] == revision + 1
    with closing(sqlite3.connect(backup)) as db:
        assert authorities(db) == before
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_lookup_schema'").fetchone()
            is None
        )
    with pytest.raises(FileExistsError):
        store.migrate_source_lookup(backup)
    with pytest.raises(ValueError, match="requires schema 3 without"):
        store.migrate_source_lookup(h.directory / "second-backup.sqlite")


@pytest.mark.parametrize("target", ["database", "guard"])
def test_migration_rejects_authority_or_checkpoint_as_backup(h, target):
    store = old_guarded_store(h)
    with pytest.raises(ValueError, match="distinct local file"):
        store.migrate_source_lookup(store.path if target == "database" else store.recovery_path)


def test_migration_never_reconstructs_missing_guard(h):
    store = old_guarded_store(h)
    store.recovery_path.unlink()
    with pytest.raises(Fault, match="dependency_unavailable"):
        store.migrate_source_lookup(h.directory / "failed-backup.sqlite")
    assert not store.recovery_path.exists()
    with closing(sqlite3.connect(store.path)) as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_lookup_schema'").fetchone()
            is None
        )


def test_index_install_failure_rolls_back_without_authority_loss(h, monkeypatch):
    from tianshu_memory import source_lookup_migration

    store = old_guarded_store(h)
    with store.transaction() as db:
        before = authorities(db)
    guard = store.recovery_path.read_bytes()
    install = source_lookup_migration.install

    def interrupted(db):
        install(db)
        raise OSError("synthetic migration interruption")

    monkeypatch.setattr(source_lookup_migration, "install", interrupted)
    backup = h.directory / "failed-backup.sqlite"
    with pytest.raises(OSError, match="synthetic migration interruption"):
        store.migrate_source_lookup(backup)
    with Store(store.path).transaction() as db:
        assert authorities(db) == before
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_lookup_schema'").fetchone()
            is None
        )
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='admissions_receipt'").fetchone()
            is None
        )
    assert store.recovery_path.read_bytes() == guard
    with closing(sqlite3.connect(backup)) as db:
        assert authorities(db) == before
