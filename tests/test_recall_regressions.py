import sqlite3
from contextlib import contextmanager

import pytest

from tianshu_memory import workflow


@pytest.mark.parametrize(
    "query",
    [
        "今天天气怎么样",
        "晚上天气怎么样",
        "今天穿什么好",
        "白天穿什么",
        "晚上好呀",
        "今天过得怎么样",
        "晚上吃什么",
        "现在可以聊聊吗",
        "晚上有什么电影推荐",
    ],
)
def test_common_characters_and_time_do_not_retrieve_unrelated_coffee(h, query):
    h.seed()
    assert h.select(query=query)["selected_units"] == []


def controlled_group_ids(monkeypatch, ids):
    iterator = iter(ids)
    original = workflow.new_id
    monkeypatch.setattr(
        workflow, "new_id", lambda prefix: next(iterator) if prefix == "group" else original(prefix)
    )


def test_topic_gate_excludes_movie_before_single_group_budget_selection(h, monkeypatch):
    controlled_group_ids(monkeypatch, ["group:000-movie", "group:zzz-coffee"])
    h.seed(
        [
            h.draft(
                units=[h.unit(statement="晚上看电影", conditions=[], negations=[])],
                field_key="movie",
            ),
            h.draft(),
        ]
    )
    coffee = h.select(query="field:coffee")
    selected = h.select(query="晚上咖啡", budget=coffee["budget_used"]["bytes"])
    assert [u["statement"] for u in selected["selected_units"]] == ["晚上不喝咖啡，白天偶尔可以"]
    assert selected["selected_units"][0]["conditions"] == ["白天偶尔可以"]
    assert selected["selected_units"][0]["negations"] == ["晚上不喝咖啡"]


def test_topic_coverage_then_fts_rank_precedes_id_and_preserves_dependencies(h, monkeypatch):
    controlled_group_ids(monkeypatch, ["group:000-cup", "group:zzz-flavour"])
    h.seed(
        [
            h.draft(
                units=[h.unit(statement="咖啡杯颜色是蓝色", conditions=[], negations=[])],
                field_key="cup",
            ),
            h.draft(
                units=[
                    h.unit(statement="咖啡口味偏清淡", conditions=["口味适用于白天"], negations=[]),
                    h.unit(statement="晚上不喝咖啡", conditions=[], negations=["晚上不喝咖啡"]),
                ],
                field_key="flavour",
            ),
        ]
    )
    expected = h.select(query="field:flavour")
    actual = h.select(query="咖啡口味", budget=expected["budget_used"]["bytes"])
    assert actual["selected_units"] == expected["selected_units"]
    assert actual["dependency_groups"] == expected["dependency_groups"]
    assert len(actual["selected_units"]) == 2
    # The lower coverage match is allowed only when the caller has enough remaining budget.
    roomy = h.select(query="咖啡口味")
    assert roomy["dependency_groups"][0]["semantic_group_id"] == "group:zzz-flavour"


def test_fts_score_breaks_equal_coverage_before_id(h, monkeypatch):
    controlled_group_ids(monkeypatch, ["group:000-diluted", "group:zzz-focused"])
    h.seed(
        [
            h.draft(
                units=[
                    h.unit(
                        statement="喜欢咖啡，也喜欢音乐电影摄影阅读旅行绘画游泳跑步烹饪园艺",
                        conditions=[],
                        negations=[],
                    )
                ],
                field_key="many-interests",
            ),
            h.draft(
                units=[h.unit(statement="喜欢咖啡", conditions=[], negations=[])],
                field_key="coffee",
            ),
        ]
    )
    budget = h.select(query="field:coffee")["budget_used"]["bytes"]
    result = h.select(query="咖啡", budget=budget)
    assert [u["statement"] for u in result["selected_units"]] == ["喜欢咖啡"]


def guarded_sql(h, monkeypatch, *, zero_content):
    statements, reads = [], []
    original = h.store.transaction

    def authorizer(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ:
            reads.append((table, column))
            if table.startswith("search_index") or table.startswith("allowed_search"):
                return sqlite3.SQLITE_DENY
            if zero_content and table in {
                "groups",
                "records",
                "history",
                "sources",
                "lineage",
                "projections",
            }:
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_VTABLE:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def transaction():
        with original() as db:
            db.set_authorizer(authorizer)
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(h.store, "transaction", transaction)
    return statements, reads


@pytest.mark.parametrize("zero_dimension", ["tokens", "bytes"])
def test_zero_budget_reads_only_authority_metadata_without_fts(h, monkeypatch, zero_dimension):
    h.seed([h.draft(), h.draft(scope=h.group)])
    statements, reads = guarded_sql(h, monkeypatch, zero_content=True)
    request = h.selection()
    request["budget"][zero_dimension] = 0
    response = h.post("memory/select", request)
    assert response.status_code == 200, response.text
    assert response.json()["selected_units"] == []
    assert response.json()["budget_used"] == {"tokens": 0, "bytes": 0}
    assert ("accounts", "person_id") in reads and ("scopes", "version") in reads
    assert not any(
        "allowed_search" in statement or "search_index" in statement for statement in statements
    )
    stale = dict(request, known_scope_version=2)
    assert h.post("memory/select", stale).json()["code"] == "scope_changed"
    denied = h.selection(budget=0)
    denied["requested_scope"]["conversation_id"] = "other"
    assert h.post("memory/select", denied).status_code == 403
    h.add_origin("origin-private", dict(h.private, conversation_id=None))
    h.save_config()
    assert h.post("memory/select", request).status_code == 503


@pytest.mark.parametrize("kind", ["field", "item"])
def test_exact_queries_filter_groups_before_body_reads_and_never_use_fts(h, monkeypatch, kind):
    controlled_group_ids(monkeypatch, ["group:000-irrelevant", "group:zzz-required"])
    h.seed(
        [
            h.draft(
                units=[h.unit(statement="晚上看电影", conditions=[], negations=[])],
                field_key="movie",
                item_key="movie",
            ),
            h.draft(field_key="coffee", item_key="coffee"),
        ]
    )
    statements, reads = guarded_sql(h, monkeypatch, zero_content=False)
    actual = h.select(query=f"{kind}:coffee")
    assert [u["semantic_group_id"] for u in actual["selected_units"]] == ["group:zzz-required"]
    body_queries = [sql for sql in statements if "FROM records WHERE" in sql]
    assert len(body_queries) == 1 and "group:zzz-required" in body_queries[0]
    assert all("group:000-irrelevant" not in sql for sql in body_queries)
    assert any(f"{kind}_key='coffee'" in sql for sql in statements)
    assert not any("search_index" in table or "allowed_search" in table for table, _ in reads)
