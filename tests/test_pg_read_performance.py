"""Correctness and bounded-I/O regressions for the two read optimizations.

PostgreSQL cases use only TIANSHU_TEST_POSTGRES_URL and random fixture schemas.
They measure statement shape/count, not a wall-clock performance assertion.
"""

import copy
import sqlite3
from contextlib import closing, contextmanager
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from test_knowledge import SECRET, imported
from test_knowledge import knowledge as knowledge
from test_recall_scaling import (
    test_batched_group_projection_needs_every_current_projection as projection_case,
)
from test_recall_scaling import (
    test_batched_validation_drops_the_whole_inconsistent_group as incomplete_case,
)
from test_storage_backends import postgres_store

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_http import ACTION_PATH, create_app
from tianshu_memory.postgres_recall import rank
from tianshu_memory.source_authority import SourceAuthority


@pytest.fixture(params=["sqlite", "postgresql"])
def knowledge_backend(knowledge, request):
    if request.param == "sqlite":
        yield knowledge
        return
    run, _, old, path, config = knowledge
    imported(run)
    old.close()
    with postgres_store(old.path, service="knowledge") as (_, settings):
        config["storage_backends"] = settings["storage_backends"]
        path.write_text(canonical(config), encoding="utf-8")
        yield knowledge


@pytest.mark.parametrize("count", [0, 1, 32])
def test_sqlite_revoked_groups_do_not_prefetch_payloads(h, count):
    if count:
        h.seed([h.draft(field_key=f"coffee-{i}", units=[h.unit()]) for i in range(count)])
        with h.store.transaction() as db:
            db.execute("UPDATE sources SET state='withdrawn'")
    statements = []
    transaction = h.store.transaction

    @contextmanager
    def observe():
        with transaction() as db:
            db.set_trace_callback(statements.append)
            yield db

    h.store.transaction = observe
    assert not h.select()["selected_units"]
    assert not any("FROM records" in sql or "FROM projections" in sql for sql in statements)
    assert not any("allowed_search" in sql for sql in statements)


def test_batch_admission_is_equivalent_and_does_not_remember_revocations():
    authority = object.__new__(SourceAuthority)
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE source_admissions(key TEXT,verified INTEGER)")
        db.execute("CREATE TABLE suppression(source_key TEXT)")
        groups = {}
        for i in range(40):
            key = str(i)
            db.execute("INSERT INTO source_admissions VALUES (?,?)", (key, i % 3 != 0))
            if i % 7 == 0:
                db.execute("INSERT INTO suppression VALUES (?)", (key,))
            groups[key] = [
                dict(
                    key=key,
                    state="withdrawn" if i % 11 == 0 else "active",
                    revision=2,
                    required_revision=1 if i % 5 == 0 else 2,
                    epoch=2,
                    required_epoch=1 if i % 13 == 0 else 2,
                )
            ]
        groups["missing-lineage"] = []
        groups["two-sources"] = [*groups["1"], *groups["3"]]
        expected = {key for key, rows in groups.items() if authority.current(db, rows)}
        statements = []
        db.set_trace_callback(statements.append)
        assert authority.current_many(db, groups) == expected
        assert len(statements) == 1
        db.execute("INSERT INTO suppression VALUES ('1')")
        db.execute("UPDATE source_admissions SET verified=0 WHERE key='2'")
        assert authority.current_many(db, groups) == {
            key for key, rows in groups.items() if authority.current(db, rows)
        }
        assert "1" not in authority.current_many(db, groups)


@pytest.mark.parametrize("prefetched", [False, True])
def test_quoted_history_title_retains_all_versions_of_only_the_same_object(prefetched):
    from tianshu_memory.fact_entities import historical_title_groups

    facts = {
        "old": {
            "payload": canonical(dict(entity={"id": "notebook-one"}, key="title", value="暮色"))
        },
        "new": {
            "payload": canonical(dict(entity={"id": "notebook-one"}, key="title", value="夜航"))
        },
        "other": {
            "payload": canonical(dict(entity={"id": "notebook-two"}, key="title", value="晨光"))
        },
    }
    candidates = [({"id": key}, [], []) for key in facts]
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE automatic_facts(group_id TEXT,payload TEXT)")
        db.executemany(
            "INSERT INTO automatic_facts VALUES (?,?)",
            [(key, row["payload"]) for key, row in facts.items()],
        )
        assert historical_title_groups(
            db, candidates, "以前《夜航》的标题", facts=facts if prefetched else None
        ) == {"old", "new"}


def test_postgres_bm25_matches_original_formula_and_preserves_the_whole_corpus(tmp_path):
    documents = [
        ("first", "receipt retry retry coffee"),
        ("second", "receipt wait"),
        ("third", "咖啡 书房 receipt unrelated unrelated"),
        ("empty", ""),
        ("unmatched", "other long words no match"),
        ("operator-word", "or and"),
    ]
    with postgres_store(tmp_path / "bm25.sqlite") as (store, _):
        with store.transaction() as db:
            db.execute(
                "CREATE VIRTUAL TABLE temp.allowed_search USING fts5(group_id UNINDEXED,text)"
            )
            db.executemany("INSERT INTO temp.allowed_search VALUES (?,?)", documents)
            for expression in ('"receipt" OR "retry"', '"咖啡"', '"missing"', '"OR"'):
                before = {
                    row["group_id"]: row["relevance"]
                    for row in db.execute(
                        "SELECT group_id,memory_bm25(text,?) AS relevance "
                        "FROM temp.allowed_search WHERE allowed_search MATCH ?",
                        (expression, expression),
                    )
                }
                after = {
                    row["group_id"]: row["relevance"] for row in rank(db, documents, expression)
                }
                assert after == pytest.approx(before, abs=1e-12)
        # No connection-local corpus or statistics from the preceding call survive.
        with store.transaction() as db:
            assert [row["group_id"] for row in rank(db, [("new", "receipt")], '"receipt"')] == [
                "new"
            ]


def test_postgres_group_reads_are_batched_and_ranking_is_one_statement(h):
    h.seed([h.draft(field_key=f"coffee-{i}", units=[h.unit()]) for i in range(32)])
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        statements = []
        transaction = store.transaction

        @contextmanager
        def observe():
            with transaction() as db:
                native = db.native

                def record(sql, parameters=(), **options):
                    statements.append(sql)
                    return native(sql, parameters, **options)

                db.native = record
                yield db

        store.transaction = observe
        result = h.select()
        assert len(result["selected_units"]) == 32
        assert len([sql for sql in statements if "FROM records WHERE group_id IN" in sql]) == 1
        assert len([sql for sql in statements if "FROM lineage l JOIN sources" in sql]) == 1
        assert len([sql for sql in statements if "jsonb_to_recordset" in sql]) == 1
        assert not any("INSERT INTO pg_temp.allowed_search" in sql for sql in statements)
        ids = [group["semantic_group_id"] for group in result["dependency_groups"]]
        assert ids == sorted(ids)


@pytest.mark.parametrize("damage", ["version", "record", "group", "person", "members", "source"])
def test_postgres_inconsistent_group_is_omitted_in_full(h, damage):
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        incomplete_case(h, damage)


@pytest.mark.parametrize("damage", ["missing", "withdrawn", "version", "owner"])
def test_postgres_projection_requires_every_current_member(h, damage):
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        projection_case(h, damage)


def test_hot_knowledge_reuses_storage_but_rechecks_permissions_and_files(
    knowledge_backend, monkeypatch
):
    from tianshu_memory import knowledge_storage

    run, root, _, path, config = knowledge_backend
    imported(run)
    opened = []
    factory = knowledge_storage.Store.from_config

    def create(*args, **kwargs):
        result = factory(*args, **kwargs)
        opened.append(result)
        return result

    monkeypatch.setattr(knowledge_storage.Store, "from_config", create)
    with closing(KnowledgeApplication(path)) as app:

        def execute():
            return app.execute(
                dict(
                    operation="query",
                    project_id="demo",
                    arguments=dict(text="receipt", budget_bytes=8192),
                ),
                client="reader",
                credential=SECRET,
            )

        first = execute()
        assert first["blocks"]
        assert execute() == first
        assert len(opened) == 1
        denied = copy.deepcopy(config)
        denied["knowledge"]["clients"]["reader"]["permissions"] = []
        path.write_text(canonical(denied), encoding="utf-8")
        with pytest.raises(Fault, match="forbidden"):
            execute()
        path.write_text(canonical(config), encoding="utf-8")
        (root / "design.md").write_text("Changed original file.\n", encoding="utf-8")
        assert execute()["omissions"] == ["stale_source"]
        assert len(opened) == 1


def test_closing_hot_owner_does_not_close_a_query_between_capture_and_serve(
    knowledge_backend, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from tianshu_memory import knowledge_sources

    run, _, _, path, _ = knowledge_backend
    imported(run)
    application = KnowledgeApplication(path)
    request = dict(
        operation="query", project_id="demo", arguments=dict(text="receipt", budget_bytes=8192)
    )

    def call():
        return application.execute(request, client="reader", credential=SECRET)

    first = call()
    entered, release = Event(), Event()
    read_file = knowledge_sources.read_file

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return read_file(*args, **kwargs)

    monkeypatch.setattr(knowledge_sources, "read_file", pause)
    with ThreadPoolExecutor(1) as pool:
        worker = pool.submit(call)
        try:
            assert entered.wait(10)
            application.close()
        finally:
            release.set()
        assert worker.result(10) == first
    # A worker admitted before shutdown can also reach authorization afterwards.
    # It uses a fresh short-lived owner, preserving the old transport behavior.
    assert call() == first


def test_hot_knowledge_switches_database_instead_of_serving_a_previous_registration(knowledge):
    run, _, store, path, config = knowledge
    imported(run)
    request = dict(
        operation="query", project_id="demo", arguments=dict(text="receipt", budget_bytes=8192)
    )
    with closing(KnowledgeApplication(path)) as app:
        assert app.execute(request, client="reader", credential=SECRET)["blocks"]
        changed = dict(config, database_path=str(path.with_name("empty.sqlite")))
        path.write_text(canonical(changed), encoding="utf-8")
        with pytest.raises(Fault, match="dependency_unavailable"):
            app.execute(request, client="reader", credential=SECRET)
        path.write_text(canonical(dict(config, database_path=store.path)), encoding="utf-8")
        assert app.execute(request, client="reader", credential=SECRET)["blocks"]


def test_http_knowledge_reuses_store_across_fresh_request_applications(knowledge, monkeypatch):
    from tianshu_memory import knowledge_storage

    run, _, _, path, _ = knowledge
    imported(run)
    app = create_app(path, "writer", 55001)
    opened = []
    factory = knowledge_storage.Store.from_config

    def create(*args, **kwargs):
        owner = factory(*args, **kwargs)
        opened.append(owner)
        return owner

    monkeypatch.setattr(knowledge_storage.Store, "from_config", create)
    with TestClient(app, base_url="http://127.0.0.1:55001") as client:
        body = dict(
            operation="query", project_id="demo", arguments=dict(text="receipt", budget_bytes=8192)
        )
        responses = [
            client.post(ACTION_PATH, json=body, headers={"Authorization": "Bearer " + SECRET})
            for _ in range(3)
        ]
        assert all(response.status_code == 200 for response in responses)
        assert all(response.json() == responses[0].json() for response in responses)
        assert len(opened) == 1


def test_shared_application_keeps_concurrent_domain_and_project_fields_private(
    knowledge, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    _, _, _, path, _ = knowledge
    meeting = Barrier(2)

    def execute(invocation, request, **auth):
        invocation.project_id = request["project_id"]
        invocation.note_domain = request["operation"] == "note_query"
        meeting.wait(10)
        return invocation.project_id, invocation.note_domain

    monkeypatch.setattr(KnowledgeApplication, "_execute", execute)
    with closing(KnowledgeApplication(path)) as app, ThreadPoolExecutor(2) as pool:
        first = pool.submit(
            app.execute, dict(project_id="one", operation="query"), client="a", credential=SECRET
        )
        second = pool.submit(
            app.execute,
            dict(project_id="two", operation="note_query"),
            client="b",
            credential=SECRET,
        )
        assert first.result(10) == ("one", False)
        assert second.result(10) == ("two", True)
        assert app.project_id is None and app.note_domain is False


def test_storage_configuration_switch_defers_close_until_active_lease_ends(monkeypatch):
    from tianshu_memory import knowledge_storage

    class Owner:
        def __init__(self, path):
            self.path, self.closed = path, False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        knowledge_storage.Store, "from_config", lambda config, **_: Owner(config["database_path"])
    )
    storage = knowledge_storage.KnowledgeStorage()
    first, second = {"database_path": "first"}, {"database_path": "second"}
    with storage.lease(first, knowledge_storage.registration(first)) as old:
        with storage.lease(second, knowledge_storage.registration(second)) as new:
            assert old.path != new.path
            assert not old.closed
            storage.close()
            assert not old.closed and not new.closed
        assert new.closed and not old.closed
    assert old.closed
    with storage.lease(first, knowledge_storage.registration(first)) as late:
        assert late is not old and not late.closed
    assert late.closed
