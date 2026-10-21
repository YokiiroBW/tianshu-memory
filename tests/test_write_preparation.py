"""Local SQL equivalence plus an explicitly gated native PG scope-bump test."""

import json
import sqlite3
from contextlib import closing, contextmanager

import pytest
from test_recall_preparation import RelationalPostgres

from tianshu_memory.automatic_facts import index_text
from tianshu_memory.domain import canonical, terms
from tianshu_memory.memory_context.history import metadata_key
from tianshu_memory.recall_language import topic_tokens
from tianshu_memory.service import MemoryService
from tianshu_memory.workflow import _WorkflowBase

SCOPE = dict(
    actor_id="synthetic-actor",
    person_id="synthetic-person",
    audience="self_private",
    conversation_id="synthetic-conversation",
)


@contextmanager
def history_database(version, history):
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE scopes(key TEXT PRIMARY KEY,version INTEGER NOT NULL);"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
        )
        if version is not None:
            db.execute("INSERT INTO scopes VALUES (?,?)", (canonical(SCOPE), version))
        if history is not None:
            db.execute("INSERT INTO metadata VALUES (?,?)", (metadata_key(SCOPE), history))
        db.commit()
        yield db


HISTORY_CASES = [
    (None, None),
    (7, None),
    (7, '{"scope_version":7,"history_version":3,"source_history_version":4}'),
    (7, '{"scope_version":7,"history_version":3}'),
    (8, '{"scope_version":7,"history_version":3,"source_history_version":4}'),
    (7, '{"scope_version":6}'),
    (7, "invalid-json"),
    (7, "null"),
    (7, '{"scope_version":7,"source_history_version":4}'),
]


@pytest.mark.parametrize(("version", "history"), HISTORY_CASES)
@pytest.mark.parametrize(
    ("changed", "source_changed"), [(True, None), (False, None), (True, False)]
)
def test_pg_bump_matches_sqlite_fallback_errors_and_history_epochs(
    version, history, changed, source_changed
):
    service = MemoryService(None, None)
    results, statements = [], []
    for postgres in (False, True):
        with history_database(version, history) as local:
            trace = []
            db = RelationalPostgres(local) if postgres else local
            if not postgres:
                local.set_trace_callback(trace.append)
            try:
                current = service._bump(
                    db,
                    dict(
                        SCOPE, profile_subject={"kind": "person", "person_id": "synthetic-person"}
                    ),
                    history_changed=changed,
                    source_history_changed=source_changed,
                )
                result = (current, None)
            except (ValueError, TypeError, KeyError) as error:
                result = (None, type(error))
            statements.append(
                list(
                    db.statements
                    if postgres
                    else [sql for sql in trace if not sql.startswith("BEGIN")]
                )
            )
            local.set_trace_callback(None)
            results.append(
                (
                    result,
                    [tuple(row) for row in local.execute("SELECT * FROM scopes ORDER BY key")],
                    [tuple(row) for row in local.execute("SELECT * FROM metadata ORDER BY key")],
                )
            )
    assert results[0] == results[1]
    if results[0][0][1] is None:
        assert len(statements[0]) == 7
        assert len(statements[1]) == 3
        assert all("RETURNING" not in sql for sql in statements[0])
    else:
        assert all(not sql.startswith("INSERT") for sql in statements[0] + statements[1])


def check_bump_freshness_and_rollback(db):
    service = MemoryService(None, None)
    assert service._bump(db, SCOPE, history_changed=False) == 8
    stored = json.loads(
        db.execute("SELECT value FROM metadata WHERE key=?", (metadata_key(SCOPE),)).fetchone()[0]
    )
    assert stored == dict(scope_version=8, history_version=3, source_history_version=4)
    # A different writer changes the content version in this same transaction.
    # The next bump must reread and conservatively invalidate both retained epochs.
    db.execute("UPDATE scopes SET version=9 WHERE key=?", (canonical(SCOPE),))
    assert service._bump(db, SCOPE, history_changed=False) == 10
    stored = json.loads(
        db.execute("SELECT value FROM metadata WHERE key=?", (metadata_key(SCOPE),)).fetchone()[0]
    )
    assert stored == dict(scope_version=10, history_version=9, source_history_version=9)


def test_pg_bump_sql_double_rereads_after_dml_and_rollback():
    history = '{"scope_version":7,"history_version":3,"source_history_version":4}'
    with history_database(7, history) as local:
        remote = RelationalPostgres(local)
        check_bump_freshness_and_rollback(remote)
        local.rollback()
        check_bump_freshness_and_rollback(remote)
        local.rollback()
        assert local.execute("SELECT version FROM scopes").fetchone()[0] == 7
        assert local.execute("SELECT value FROM metadata").fetchone()[0] == history


def test_native_pg_bump_rereads_after_dml_and_rollback():
    import psycopg
    from test_storage_backends import pg_options
    from tianshu_storage import PostgresConnection

    with psycopg.connect(pg_options()["postgresql_url"], autocommit=True) as raw:
        with closing(PostgresConnection(raw)) as remote:
            remote.native("CREATE TEMP TABLE scopes(key TEXT PRIMARY KEY,version BIGINT NOT NULL)")
            remote.native("CREATE TEMP TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            remote.native("INSERT INTO scopes VALUES (?,7)", (canonical(SCOPE),))
            history = '{"scope_version":7,"history_version":3,"source_history_version":4}'
            remote.native("INSERT INTO metadata VALUES (?,?)", (metadata_key(SCOPE), history))
            # The wrapper opens an implicit write transaction even though the
            # raw driver is autocommit. Keep the seed outside both rollbacks.
            remote.commit()
            for _ in range(2):
                remote.native("BEGIN")
                check_bump_freshness_and_rollback(remote)
                remote.rollback()
            assert remote.native("SELECT version FROM scopes").fetchone()[0] == 7
            assert remote.native("SELECT value FROM metadata").fetchone()[0] == history


@pytest.mark.parametrize("state", ["not-installed", "no-fact", "fact-and-quotes"])
def test_index_uses_complete_current_fact_text_once_and_reloads_on_next_call(state):
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE automatic_facts(group_id TEXT PRIMARY KEY,payload TEXT);"
            "CREATE TABLE automatic_evidence(group_id TEXT,quote TEXT);"
            "CREATE TABLE search_index(record_id TEXT,record_version INTEGER,scope TEXT,text TEXT);"
        )
        if state != "not-installed":
            db.execute("INSERT INTO metadata VALUES ('automatic_fact_schema','1')")
        if state == "fact-and-quotes":
            fact = dict(key="drink", value="coffee", entity=None, keywords=["morning"])
            db.execute("INSERT INTO automatic_facts VALUES ('g',?)", (canonical(fact),))
            db.execute("INSERT INTO automatic_evidence VALUES ('g','fresh verified quote')")
        unit = dict(
            record_id="r",
            record_version=1,
            semantic_group_id="g",
            statement="A synthetic morning preference",
            conditions=["only after breakfast"],
            negations=["never at night"],
            valid_time="current",
        )
        text = " ".join(
            [
                unit["statement"],
                *unit["conditions"],
                *unit["negations"],
                unit["valid_time"],
                "drink",
                "",
            ]
        )

        def expected():
            # The original two reads are the oracle; neither one is removed from
            # the next independent _index call after intervening DML.
            return " ".join(
                terms(text + " " + index_text(db, unit))
                + topic_tokens(text + " " + index_text(db, unit))
            )

        writer = _WorkflowBase(None)
        for changed in (False, True):
            if changed:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('automatic_fact_schema','1')")
                fact = dict(key="drink", value="tea", entity=None, keywords=["afternoon"])
                db.execute(
                    "INSERT OR REPLACE INTO automatic_facts VALUES ('g',?)", (canonical(fact),)
                )
                db.execute("INSERT INTO automatic_evidence VALUES ('g','changed current quote')")
            oracle = expected()
            trace = []
            db.set_trace_callback(trace.append)
            writer._index(db, unit, SCOPE, "drink", None)
            db.set_trace_callback(None)
            actual = db.execute(
                "SELECT text FROM search_index ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
            assert actual == oracle
            reads = [sql for sql in trace if sql.startswith("SELECT")]
            assert sum("automatic_fact_schema" in sql for sql in reads) == 1
            assert len(reads) == (
                3 if changed or state == "fact-and-quotes" else 1 if state == "not-installed" else 2
            )
