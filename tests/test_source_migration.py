"""Isolated SQLite migration, rollback, and recovery gate; no remote products."""

import json
import sqlite3

import pytest

from tianshu_memory.contracts import Contracts
from tianshu_memory.domain import Fault, admission_key, canonical, source_key
from tianshu_memory.source_authority import SourceAuthority
from tianshu_memory.store import Store


def prepare(h):
    seeded, job, event = h.seed()
    h.store.migrate_profiles(h.directory / "schema1.sqlite")
    loaded = Contracts(h.contracts.directory)
    loaded.load_sources()
    return loaded, seeded, job, event


def test_migration_retains_unverified_A_lineage_ledgers_and_complete_backup(h):
    contracts, seeded, job, event = prepare(h)
    h.revision(seeded["record_ids"][0])  # Old proof has no independently bound binding version.
    backup = h.directory / "schema2.sqlite"
    result = h.store.migrate_sources(backup, contracts)
    assert result["schema"] == 3 and result["unverified_admissions"] == 1
    key = admission_key(h.source(), h.private)
    with Store(h.store.path).transaction() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT key FROM sources").fetchone()[0] == key
        assert db.execute("SELECT source_key FROM lineage").fetchone()[0] == key
        assert db.execute("SELECT source_key FROM source_writes").fetchone()[0] == key
        assert list(json.loads(db.execute("SELECT source_snapshot FROM jobs").fetchone()[0])) == [
            key
        ]
        admission = db.execute("SELECT * FROM source_admissions").fetchone()
        assert admission["payload"] is None and admission["verified"] == 0
        physical = db.execute("SELECT * FROM physical_sources").fetchone()
        assert physical["payload"] is None and physical["state"] == "unverified"
        assert db.execute("SELECT COUNT(*) FROM owner_heads").fetchone()[0] == 0
        assert db.execute("SELECT binding_version FROM confirmations").fetchone()[0] is None
        authority = SourceAuthority(None, contracts)
        with pytest.raises(Fault, match="dependency_unavailable"):
            authority.verify(db, [h.source()], h.private)
        before = authority.revision(db)
        db.execute("UPDATE accounts SET version=version+1")
        assert authority.revision(db) > before
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == "2"
        assert db.execute("SELECT key FROM sources").fetchone()[0] == source_key(h.source())
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    with pytest.raises(FileExistsError):
        h.store.migrate_sources(backup, contracts)


def test_old_withdrawn_only_suppresses_proven_actor_not_physical_tombstone(h):
    contracts, seeded, _, _ = prepare(h)
    request = h.revision(seeded["record_ids"][0], "forget")
    assert h.post("memory/revise", request).status_code == 200
    h.store.migrate_sources(h.directory / "schema2.sqlite", contracts)
    with h.store.transaction() as db:
        suppression = db.execute("SELECT * FROM suppression").fetchone()
        assert suppression["source_key"] == admission_key(h.source(), h.private)
        assert db.execute("SELECT state FROM physical_sources").fetchone()[0] == "unverified"
        assert db.execute("SELECT state FROM records").fetchone()[0] == "tombstoned"


@pytest.mark.parametrize(
    "edge", ["lineage", "audience", "conversation", "job", "write", "bad_key", "missing_person"]
)
def test_ambiguous_legacy_ownership_rolls_back_and_keeps_backup(h, edge):
    contracts, _, _, _ = prepare(h)
    other_scope = dict(h.private, actor_id="another-actor")
    with h.store.transaction() as db:
        if edge in {"lineage", "audience", "conversation"}:
            if edge == "audience":
                other_scope = dict(h.private, audience="group")
            if edge == "conversation":
                other_scope = dict(h.private, conversation_id="another-conversation")
            db.execute("UPDATE groups SET scope=?", (canonical(other_scope),))
        elif edge == "job":
            event = json.loads(db.execute("SELECT event FROM jobs").fetchone()[0])
            event["scope"] = other_scope
            db.execute("UPDATE jobs SET event=?", (canonical(event),))
        elif edge == "write":
            db.execute("UPDATE source_writes SET scope=?", (canonical(other_scope),))
        else:
            row = db.execute("SELECT * FROM sources").fetchone()
            payload, scope = json.loads(row["payload"]), json.loads(row["scope"])
            if edge == "bad_key":
                payload["message_key"]["message_id"] = "unproven"
            else:
                scope["person_id"] = "nonexistent-person"
            db.execute(
                "UPDATE sources SET payload=?,scope=?", (canonical(payload), canonical(scope))
            )
    backup = h.directory / "failed-migration.sqlite"
    with pytest.raises(ValueError, match="legacy"):
        h.store.migrate_sources(backup, contracts)
    with h.store.transaction() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == "2"
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='source_admissions'").fetchone()
            is None
        )
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_explicit_recovery_gate_survives_restart_and_blocks_empty_probe(h):
    contracts, _, _, _ = prepare(h)
    h.store.migrate_sources(h.directory / "schema2.sqlite", contracts)
    with h.store.transaction() as db:
        db.execute("UPDATE metadata SET value='pending' WHERE key='source_recovery'")
    with Store(h.store.path).transaction() as db:
        with pytest.raises(Fault, match="dependency_unavailable"):
            SourceAuthority(None, contracts).coverage(db, h.private, [], False)


def test_restoring_older_database_cannot_restore_lost_suppression(h):
    contracts, _, _, _ = prepare(h)
    h.store.migrate_sources(h.directory / "schema2.sqlite", contracts)
    before_forget = h.directory / "before-forget.sqlite"
    with sqlite3.connect(h.store.path) as live, sqlite3.connect(before_forget) as backup:
        live.backup(backup)
    with h.store.transaction() as db:
        db.execute(
            "INSERT INTO suppression VALUES (?, 'forget')", (admission_key(h.source(), h.private),)
        )
    retained = h.store.recovery_path.read_bytes()
    with sqlite3.connect(before_forget) as backup, sqlite3.connect(h.store.path) as live:
        backup.backup(live)
    assert h.store.recovery_path.read_bytes() == retained
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(h.store.path)


def test_missing_independent_checkpoint_never_bootstraps_from_old_database(h):
    contracts, _, _, _ = prepare(h)
    h.store.migrate_sources(h.directory / "schema2.sqlite", contracts)
    h.store.recovery_path.unlink()
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(h.store.path)
    assert not h.store.recovery_path.exists()


def test_checkpoint_persisted_before_failed_database_commit_closes_recovery(h, monkeypatch):
    from tianshu_memory import source_recovery

    contracts, _, _, _ = prepare(h)
    h.store.migrate_sources(h.directory / "schema2.sqlite", contracts)
    persist = source_recovery.persist

    def interrupt_after_guard(*args, **kwargs):
        persist(*args, **kwargs)
        raise OSError("synthetic interruption after independent checkpoint")

    monkeypatch.setattr(source_recovery, "persist", interrupt_after_guard)
    with pytest.raises(OSError, match="synthetic interruption"):
        with h.store.transaction() as db:
            db.execute(
                "INSERT INTO suppression VALUES (?, 'forget')",
                (admission_key(h.source(), h.private),),
            )
    with sqlite3.connect(h.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(h.store.path)
