"""Local checks of snapshot faults and the PG setup/authority ordering contract.

SQL branch doubles and a fake pipeline do not establish native PG behavior.
"""

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from test_recall_preparation import RelationalPostgres
from tianshu_storage import MemoryCursor

from tianshu_memory import source_recovery
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.postgres import dialect
from tianshu_memory.store import Store


@contextmanager
def knowledge_state():
    project = {"roots": ["synthetic-root"]}
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE knowledge_projects(id TEXT PRIMARY KEY,registration TEXT,revision INTEGER);"
        )
        db.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [("knowledge_schema", "1"), ("knowledge_seal_key", "synthetic-seal")],
        )
        db.execute("INSERT INTO knowledge_projects VALUES ('demo',?,7)", (canonical(project),))
        yield db, project


def snapshot(db, project, operation="query"):
    return KnowledgeApplication._snapshot(
        db, "demo", project, "synthetic-client", "synthetic-credential", operation
    )


@pytest.mark.parametrize(
    ("change", "operation", "fault"),
    [
        (None, "query", None),
        ("missing-schema", "query", ("dependency_unavailable", 503)),
        ("missing-seal", "query", ("dependency_unavailable", 503)),
        ("wrong-schema", "query", ("dependency_unavailable", 503)),
        ("empty-seal", "query", ("dependency_unavailable", 503)),
        ("unregistered", "query", ("project_uninitialized", 409)),
        ("unregistered", "import", None),
        ("unregistered", "directory_scan", None),
        ("registration", "query", ("registration_changed", 409)),
        ("missing-schema-and-registration", "query", ("dependency_unavailable", 503)),
    ],
)
def test_pg_snapshot_join_preserves_sqlite_result_and_fault_order(change, operation, fault):
    with knowledge_state() as (db, project):
        if change in {"missing-schema", "missing-schema-and-registration"}:
            db.execute("DELETE FROM metadata WHERE key='knowledge_schema'")
        elif change == "missing-seal":
            db.execute("DELETE FROM metadata WHERE key='knowledge_seal_key'")
        elif change == "wrong-schema":
            db.execute("UPDATE metadata SET value='2' WHERE key='knowledge_schema'")
        elif change == "empty-seal":
            db.execute("UPDATE metadata SET value='' WHERE key='knowledge_seal_key'")
        elif change == "unregistered":
            db.execute("DELETE FROM knowledge_projects")
        if change in {"registration", "missing-schema-and-registration"}:
            db.execute("UPDATE knowledge_projects SET registration='changed'")

        statements = []
        db.set_trace_callback(statements.append)
        if fault:
            with pytest.raises(Fault) as local:
                snapshot(db, project, operation)
            assert (local.value.code, local.value.status) == fault
        else:
            expected = snapshot(db, project, operation)
            assert expected[0] == (None if change == "unregistered" else 7)
        # SQLite keeps the original SQL and metadata-fault short circuit.
        assert statements[0] == "SELECT key,value FROM metadata"
        assert len(statements) == (1 if fault and fault[0] == "dependency_unavailable" else 2)
        if len(statements) == 2:
            assert statements[1] == "SELECT * FROM knowledge_projects WHERE id='demo'"

        remote = RelationalPostgres(db)
        if fault:
            with pytest.raises(Fault) as joined:
                snapshot(remote, project, operation)
            assert (joined.value.code, joined.value.status) == fault
        else:
            assert snapshot(remote, project, operation) == expected
        assert len(remote.statements) == 1


def test_pg_snapshot_reads_revision_seal_and_registration_again_in_each_phase():
    with knowledge_state() as (db, project):
        remote = RelationalPostgres(db)
        before = snapshot(remote, project)
        db.execute("UPDATE knowledge_projects SET revision=8")
        db.execute("UPDATE metadata SET value='rotated-seal' WHERE key='knowledge_seal_key'")
        after = snapshot(remote, project)
        assert after[0] == 8 and before[0] == 7 and after[1] != before[1]
        db.execute("UPDATE knowledge_projects SET registration='changed'")
        with pytest.raises(Fault, match="registration_changed"):
            snapshot(remote, project)
        db.execute("DELETE FROM metadata WHERE key='knowledge_seal_key'")
        with pytest.raises(Fault, match="dependency_unavailable"):
            snapshot(remote, project)
        assert len(remote.statements) == 4


class SetupConnection:
    """Setup queue double: metadata is unavailable until the lock is confirmed."""

    def __init__(self, guard, failure):
        self.guard, self.failure = guard, failure
        self.events = []
        self.pipelining, self.locked = False, False
        self.metadata = dict(
            schema="3",
            postgres_memory_schema="1",
            source_instance="synthetic-instance",
            source_revision="0",
            source_recovery="ready",
        )

    @contextmanager
    def pipeline(self):
        self.events.append("pipeline-enter")
        if self.failure == "enter":
            raise RuntimeError("synthetic enter failure")
        self.pipelining = True
        try:
            yield self
            if self.failure == "flush":
                raise RuntimeError("synthetic flush failure")
            # A writer committed while our advisory lock was waiting. A metadata
            # snapshot from within setup would still see the earlier revision 0.
            self.metadata["source_revision"] = "1"
            self.locked = True
        finally:
            self.pipelining = False
            self.events.append("pipeline-exit")

    def execute(self, sql, parameters=()):
        assert sql == "BEGIN IMMEDIATE"
        return dialect(self, sql, parameters)

    def native(self, sql, parameters=()):
        self.events.append(sql)
        if sql == "SELECT key,value FROM metadata":
            assert not self.pipelining and self.locked
            return MemoryCursor(self.metadata.items(), columns=["key", "value"])
        assert self.pipelining
        assert sql in {"SET synchronous_commit=on", "BEGIN"} or "pg_advisory_xact_lock" in sql
        return MemoryCursor()

    def commit(self):
        self.events.append("commit")
        assert json.loads(self.guard.read_bytes())["revision"] == int(
            self.metadata["source_revision"]
        )
        if self.failure == "commit":
            raise RuntimeError("synthetic commit failure")

    def rollback(self):
        assert not self.pipelining
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


@pytest.mark.parametrize("failure", [None, "enter", "flush", "body", "commit"])
def test_pg_setup_finishes_before_fresh_metadata_and_keeps_checkpoint_order(
    tmp_path, monkeypatch, failure
):
    guard = tmp_path / "synthetic.source-guard.json"
    guard.write_text(
        canonical(dict(schema=3, instance="synthetic-instance", revision=1, recovery="ready")),
        encoding="utf-8",
    )
    connection = SetupConnection(guard, failure)
    store = object.__new__(Store)
    store.backend, store.recovery_path = "postgresql", guard
    store._connect = lambda: connection
    persist = source_recovery.persist

    def checkpoint(path, value, **options):
        connection.events.append("persist")
        persist(path, value, **options)

    monkeypatch.setattr(source_recovery, "persist", checkpoint)

    def operation():
        with store.transaction() as db:
            assert db is connection
            connection.events.append("body")
            if failure == "body":
                raise RuntimeError("synthetic body failure")
            db.metadata["source_revision"] = "2"

    if failure:
        with pytest.raises(RuntimeError, match=f"synthetic {failure} failure"):
            operation()
    else:
        operation()
    events = connection.events
    assert events[-1] == "close"
    assert ("rollback" in events) == bool(failure)
    if failure in {"enter", "flush"}:
        assert not any(
            event in events for event in ["body", "SELECT key,value FROM metadata", "commit"]
        )
    else:
        assert events.index("pipeline-exit") < events.index("SELECT key,value FROM metadata")
        assert events.index("SELECT key,value FROM metadata") < events.index("body")
        if failure == "body":
            assert events.count("SELECT key,value FROM metadata") == 1
            assert "persist" not in events and "commit" not in events
        else:
            assert events.count("SELECT key,value FROM metadata") == 2
            assert events.index("body") < events.index("persist") < events.index("commit")
    assert json.loads(guard.read_bytes())["revision"] == (
        1 if failure in {"enter", "flush", "body"} else 2
    )
