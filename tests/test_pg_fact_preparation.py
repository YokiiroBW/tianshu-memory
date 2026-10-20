"""Real PG sourced facts through both recall entries, with fresh-call validation."""

import pytest
from test_automatic_facts import add, initialize
from test_fact_entities import entity, saved_entity
from test_memory_context import query_request
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_storage_backends import pg_sync as pg_sync
from tianshu_storage.postgres import PostgresConnection


@pytest.mark.parametrize("pg_sync", ["self_private"], indirect=True)
def test_postgres_fact_snapshot_is_per_call_for_both_recall_entries(pg_sync, monkeypatch):
    h = pg_sync
    initialize(h)
    h.service.contracts.load_context()
    h.config["callers"]["companion"]["operations"].append("context_query")
    h.save()
    first, _ = add(
        h,
        "第一本笔记本标题是《暮色》",
        "notebook.title",
        "暮色",
        entity=entity("第一本笔记本", kind="notebook"),
    )
    one = saved_entity(h, first)
    second, _ = add(
        h,
        "第一本笔记本改叫《夜航》",
        "notebook.title",
        "夜航",
        second=2,
        entity=one,
    )
    add(
        h,
        "第二本笔记本标题是《晨光》",
        "notebook.title",
        "晨光",
        second=3,
        entity=entity("第二本笔记本", kind="notebook"),
    )
    statements = []
    native = PostgresConnection.native

    def observed(connection, sql, parameters=(), **kwargs):
        if sql.startswith("SELECT * FROM automatic_facts WHERE group_id IN ("):
            statements.append(sql)
        return native(connection, sql, parameters, **kwargs)

    monkeypatch.setattr(PostgresConnection, "native", observed)

    def recalled(path, text):
        request = (
            query_request(h, query_text=text, selection=["evidence"])
            if path.endswith("context/query")
            else dict(h.selection(budget=100000), query_text=text, selection=["evidence"])
        )
        before = len(statements)
        response = h.post(path, request)
        assert response.status_code == 200, response.text
        assert len(statements) == before + 1
        return {unit["record_id"] for unit in response.json()["selected_units"]}

    paths = ("memory/select", "memory/context/query")
    for path in paths:
        assert recalled(path, "第一本笔记本的标题是什么") == set(second["record_ids"])
        assert recalled(path, "以前《夜航》的标题") == set(
            first["record_ids"] + second["record_ids"]
        )

    newest, _ = add(
        h,
        "第一本笔记本改叫《星河》",
        "notebook.title",
        "星河",
        second=4,
        entity=one,
    )
    for path in paths:
        assert recalled(path, "第一本笔记本的标题是什么") == set(newest["record_ids"])
