"""Local SQL/catalog doubles for TS-256; native cases require explicit test PG."""

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from test_recall_preparation import RelationalPostgres
from test_storage_backends import postgres_store

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.fact_queries import for_groups
from tianshu_memory.postgres import GROUP_READ_BATCH, dialect
from tianshu_memory.recall_index import eligible_groups
from tianshu_memory.source_authority import SourceAuthority
from tianshu_memory.source_lookup_migration import POSTGRES_READY


@contextmanager
def ready_database():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
            "INSERT INTO metadata VALUES ('source_lookup_schema','1'),"
            "('source_revision','7'),('source_recovery','ready');"
            "CREATE TABLE admissions(payload TEXT);"
            "CREATE INDEX admissions_receipt ON admissions(payload);"
            "CREATE INDEX sources_scope ON admissions(payload);"
            "ATTACH DATABASE ':memory:' AS pg_catalog;"
            "CREATE TABLE pg_catalog.pg_namespace(oid INTEGER,nspname TEXT);"
            "INSERT INTO pg_catalog.pg_namespace VALUES (1,'current'),(2,'other');"
            "CREATE TABLE pg_catalog.pg_class(relname TEXT,relnamespace INTEGER,relkind TEXT);"
            "INSERT INTO pg_catalog.pg_class VALUES ('admissions_receipt',1,'i'),"
            "('sources_scope',1,'i'),('admissions_receipt',2,'i'),('sources_scope',2,'i');"
        )
        db.create_function("current_schema", 0, lambda: "current")
        db.commit()
        yield db


def damage_state(db, damage):
    if damage in {"missing-marker", "missing-revision", "missing-ready"}:
        key = {
            "missing-marker": "source_lookup_schema",
            "missing-revision": "source_revision",
            "missing-ready": "source_recovery",
        }[damage]
        db.execute("DELETE FROM metadata WHERE key=?", (key,))
    if damage in {"wrong-marker", "invalid-revision", "pending", "invalid-and-pending"}:
        if damage == "wrong-marker":
            db.execute("UPDATE metadata SET value='0' WHERE key='source_lookup_schema'")
        if damage in {"invalid-revision", "invalid-and-pending"}:
            db.execute("UPDATE metadata SET value='not-an-integer' WHERE key='source_revision'")
        if damage in {"pending", "invalid-and-pending"}:
            db.execute("UPDATE metadata SET value='pending' WHERE key='source_recovery'")
    if damage in {"missing-index", "renamed-index", "other-schema-index", "same-name-table"}:
        db.execute("DROP INDEX sources_scope")
        if damage == "missing-index":
            db.execute(
                "DELETE FROM pg_catalog.pg_class WHERE relname='sources_scope' AND relnamespace=1"
            )
        elif damage == "renamed-index":
            db.execute(
                "UPDATE pg_catalog.pg_class SET relname='renamed' WHERE relname='sources_scope' AND relnamespace=1"
            )
        elif damage == "other-schema-index":
            db.execute(
                "UPDATE pg_catalog.pg_class SET relnamespace=2 WHERE relname='sources_scope' AND relnamespace=1"
            )
        else:
            db.execute(
                "UPDATE pg_catalog.pg_class SET relkind='r' WHERE relname='sources_scope' AND relnamespace=1"
            )


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "missing-marker",
        "wrong-marker",
        "missing-index",
        "renamed-index",
        "other-schema-index",
        "same-name-table",
        "missing-revision",
        "missing-ready",
        "pending",
        "invalid-revision",
        "invalid-and-pending",
    ],
)
def test_combined_ready_probe_keeps_sqlite_fault_and_conversion_order(damage):
    with ready_database() as db:
        damage_state(db, damage)
        remote = RelationalPostgres(db)
        for connection in (db, remote):
            if damage is None:
                assert SourceAuthority.revision(connection) == 7
            elif damage == "invalid-revision":
                with pytest.raises(ValueError):
                    SourceAuthority.revision(connection)
            else:
                with pytest.raises(Fault, match="dependency_unavailable") as failure:
                    SourceAuthority.revision(connection)
                assert failure.value.status == 503
        assert remote.statements == [POSTGRES_READY]


def test_ready_probe_never_reuses_a_pre_dml_or_previous_transaction_result():
    with ready_database() as db:
        remote = RelationalPostgres(db)
        assert SourceAuthority.revision(remote) == 7
        db.execute("UPDATE metadata SET value='8' WHERE key='source_revision'")
        assert SourceAuthority.revision(remote) == 8
        damage_state(db, "missing-index")
        with pytest.raises(Fault, match="dependency_unavailable"):
            SourceAuthority.revision(remote)
        db.rollback()
        assert SourceAuthority.revision(remote) == 7
        damage_state(db, "missing-marker")
        db.commit()
        with pytest.raises(Fault, match="dependency_unavailable"):
            SourceAuthority.revision(remote)
        assert remote.statements == [POSTGRES_READY] * 5


def test_sqlite_revision_retains_the_original_four_fresh_reads():
    with ready_database() as db:
        statements = []
        db.set_trace_callback(statements.append)
        assert SourceAuthority.revision(db) == 7
        assert statements == [
            "SELECT value FROM metadata WHERE key='source_lookup_schema'",
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name IN ('admissions_receipt','sources_scope')",
            "SELECT value FROM metadata WHERE key='source_revision'",
            "SELECT value FROM metadata WHERE key='source_recovery'",
        ]


def test_catalog_translation_keeps_namespace_and_index_kind_without_pg_indexes():
    with ready_database() as db:
        remote = RelationalPostgres(db)
        statement = (
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name IN ('admissions_receipt','sources_scope')"
        )
        assert dialect(remote, statement, ()).fetchone()[0] == 2
        damage_state(db, "same-name-table")
        assert dialect(remote, statement, ()).fetchone()[0] == 1
        assert all(
            "pg_catalog.pg_class" in sql and "pg_indexes" not in sql for sql in remote.statements
        )


def seed_groups(h, count):
    """Clone a sourced synthetic group, keeping distinct complete record identities."""
    h.seed()
    with h.store.transaction() as db:
        original = db.execute("SELECT * FROM groups LIMIT 1").fetchone()
        record = db.execute("SELECT * FROM records LIMIT 1").fetchone()
        lineage = db.execute("SELECT source_key,revision,epoch FROM lineage LIMIT 1").fetchone()
        for index in range(count):
            identity, record_id = f"batch-group-{index:04}", f"batch-record-{index:04}"
            unit = dict(
                json.loads(record["payload"]), record_id=record_id, semantic_group_id=identity
            )
            db.execute(
                "INSERT INTO groups VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    original["scope"],
                    "active",
                    canonical([record_id]),
                    "evidence",
                    identity,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO records VALUES (?,?,1,'active',?)",
                (record_id, identity, canonical(unit)),
            )
            db.execute("INSERT INTO lineage VALUES (?,?,?,?)", (identity, *lineage))


def assert_group_batches(h, db):
    groups = db.execute("SELECT * FROM groups WHERE id LIKE 'batch-group-%' ORDER BY id").fetchall()
    statements, bindings = [], []
    execute = db.execute

    class Reads:
        native = getattr(db, "native", None)

        def execute(self, sql, parameters=()):
            statements.append(sql)
            bindings.append((sql, tuple(parameters)))
            return execute(sql, parameters)

    remote = Reads()
    result = eligible_groups(h.service, remote, groups, h.private)
    assert [entry[0]["id"] for entry in result] == [group["id"] for group in groups]
    assert len([sql for sql in statements if "FROM lineage l JOIN sources" in sql]) == 2
    assert len([sql for sql in statements if "FROM records WHERE group_id IN" in sql]) == 2
    assert max(len(parameters) for _, parameters in bindings) <= GROUP_READ_BATCH
    db.execute("UPDATE lineage SET epoch=epoch+1 WHERE group_id='batch-group-0000'")
    for identity, key, value in [
        ("batch-group-0511", "record_version", 2),
        ("batch-group-0512", "semantic_group_id", "unrelated"),
    ]:
        unit = json.loads(
            db.execute("SELECT payload FROM records WHERE group_id=?", (identity,)).fetchone()[0]
        )
        db.execute(
            "UPDATE records SET payload=? WHERE group_id=?",
            (canonical(dict(unit, **{key: value})), identity),
        )
    bindings.clear()
    result = eligible_groups(h.service, remote, groups, h.private)
    assert {entry[0]["id"] for entry in result} == {
        group["id"]
        for group in groups
        if group["id"] not in {"batch-group-0000", "batch-group-0511", "batch-group-0512"}
    }
    assert not any(
        "batch-group-0000" in parameters
        for sql, parameters in bindings
        if "FROM records WHERE group_id IN" in sql
    )


def test_pg_group_batch_boundary_keeps_authority_before_payload_and_complete_identity(h):
    seed_groups(h, GROUP_READ_BATCH + 1)
    with h.store.transaction() as db:
        assert_group_batches(h, db)


def test_pg_fact_metadata_batch_is_bounded_without_adding_sqlite_reads():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE automatic_facts(group_id TEXT PRIMARY KEY,payload TEXT)")
        keys = [f"fact-{index}" for index in range(GROUP_READ_BATCH + 1)]
        db.executemany("INSERT INTO automatic_facts VALUES (?,?)", [(key, "{}") for key in keys])
        candidates = [({"id": key}, [], []) for key in keys]
        statements = []
        db.set_trace_callback(statements.append)
        assert for_groups(db, candidates) is None
        assert statements == []
        remote = RelationalPostgres(db)
        assert set(for_groups(remote, candidates)) == set(keys)
        assert len(remote.statements) == 2


def test_native_pg_group_batch_boundary_keeps_the_whole_group(h):
    seed_groups(h, GROUP_READ_BATCH + 1)
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        with store.transaction() as db:
            assert_group_batches(h, db)
