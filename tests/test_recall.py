import json

from tianshu_memory.domain import canonical


def test_preserves_negation_condition_time_and_complete_group(h):
    h.seed(
        [
            h.draft(
                units=[
                    h.unit(),
                    h.unit(statement="例外仍需考虑睡眠状况", conditions=["睡眠正常"], negations=[]),
                ]
            )
        ]
    )
    result = h.select()
    assert len(result["selected_units"]) == 2
    assert result["selected_units"][0]["negations"] or result["selected_units"][1]["negations"]
    assert {u["record_id"] for u in result["selected_units"]} == set(
        result["dependency_groups"][0]["record_ids"]
    )
    assert result["dependency_groups"][0]["complete"] is True
    assert all(
        u["sources"][0]["source"]["archive_state"] == "pending" for u in result["selected_units"]
    )


def test_exact_field_item_and_real_fts_no_whole_profile(h):
    h.seed(
        [
            h.draft(),
            h.draft(
                units=[h.unit(statement="使用 Python 开发", conditions=[], negations=[])],
                field_key="technology",
            ),
            h.draft(
                units=[
                    h.unit(
                        statement="下周有一个条件计划",
                        uncertainty="inferred",
                        valid_time="next week",
                    )
                ],
                category="current_items",
                item_key="plan-1",
            ),
        ]
    )
    assert len(h.select(query="field:technology")["selected_units"]) == 1
    assert len(h.select(query="Python")["selected_units"]) == 1
    assert h.select(query="衣橱")["selected_units"] == []
    assert len(h.select(query="item:plan-1", selection=["current_items"])["selected_units"]) == 1
    assert h.select(query="item:plan-1")["selected_units"] == []
    assert h.select(query='" OR * -()')["selected_units"] == []
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == 3


def test_private_source_lineage_never_leaks_into_group_projection(h):
    private_statement = "私密经历不能泄漏"
    h.seed(
        [
            h.draft(units=[h.unit(statement=private_statement)]),
            h.draft(units=[h.unit(statement="普通兴趣：白天偶尔喝咖啡")], scope=h.group),
        ]
    )
    group = h.select(scope=h.group)
    assert len(group["selected_units"]) == 1
    wire = canonical(group)
    for secret in [
        private_statement,
        "message_key",
        "receipt_id",
        "archive_state",
        "locator",
        "synthetic-private-channel",
        "message-1",
        "conversation-private",
    ]:
        assert secret not in wire
    projection = group["selected_units"][0]["sources"][0]
    assert projection["kind"] == "shareable_projection" and projection["owner"] == "memory"
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 1
        source = json.loads(db.execute("SELECT payload FROM sources").fetchone()[0])
        assert source["archive_state"] == "pending" and source["locator"] is None


def test_unauthorized_scope_rejected_and_hidden_presence_not_reported(h):
    h.seed()
    assert h.select(scope=h.group)["omissions"] == ["no_match"]
    for key, value in [
        ("person_id", "person-other"),
        ("actor_id", "actor-other"),
        ("conversation_id", "another"),
        ("audience", "group"),
    ]:
        request = h.selection()
        request["requested_scope"][key] = value
        assert h.post("memory/select", request).status_code == 403


def test_budget_zero_whole_group_boundary_and_remaining_budget(h):
    h.seed([h.draft(units=[h.unit(), h.unit(statement="保持例外完整")])])
    full = h.select()
    cost = full["budget_used"]["bytes"]
    assert cost == len(
        canonical(
            {
                "selected_units": full["selected_units"],
                "dependency_groups": full["dependency_groups"],
            }
        ).encode("utf-8")
    )
    assert full["budget_used"]["tokens"] == cost
    for budget in [0, cost - 1]:
        result = h.select(budget=budget)
        assert result["selected_units"] == [] and result["dependency_groups"] == []
        assert result["budget_used"] == {"tokens": 0, "bytes": 0} and result["omissions"] == [
            "budget"
        ]
    assert len(h.select(budget=cost)["selected_units"]) == 2
    # Contract has no turn id: caller subtracts previous assembly, then submits the remainder.
    assert h.select(budget=cost - full["budget_used"]["tokens"])["selected_units"] == []
    request = h.selection(budget=cost)
    request["budget"]["bytes"] = cost - 1
    assert h.post("memory/select", request).json()["selected_units"] == []


def test_missing_member_and_outdated_projection_omit_entire_group(h):
    seeded, _, _ = h.seed([h.draft(units=[h.unit(), h.unit(statement="完整依赖")], scope=h.group)])
    assert len(h.select(scope=h.group)["selected_units"]) == 2
    with h.store.transaction() as db:
        db.execute(
            "UPDATE projections SET version=version+1 WHERE record_id=?", (seeded["record_ids"][0],)
        )
    assert h.select(scope=h.group)["selected_units"] == []
    with h.store.transaction() as db:
        db.execute(
            "UPDATE projections SET version=version-1 WHERE record_id=?", (seeded["record_ids"][0],)
        )
        db.execute(
            "UPDATE groups SET members=? WHERE id=?",
            (canonical(seeded["record_ids"] + ["missing-record"]), seeded["group_ids"][0]),
        )
    assert h.select(scope=h.group)["selected_units"] == []


def test_item_limit_omits_group_and_uncertainty_not_promoted(h):
    h.seed(
        [
            h.draft(
                units=[
                    h.unit(uncertainty="uncertain"),
                    h.unit(statement="据说可能可行", uncertainty="inferred"),
                ]
            )
        ]
    )
    assert {u["uncertainty"] for u in h.select()["selected_units"]} == {"uncertain", "inferred"}
    h.service.max_units = 1
    assert h.select()["selected_units"] == []
