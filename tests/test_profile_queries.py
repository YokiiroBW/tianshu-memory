import copy

import pytest
from test_profiles import profile_draft, publish

from tianshu_memory import profiles
from tianshu_memory.domain import Fault, canonical


@pytest.fixture
def p(h):
    h.store.migrate_profiles(h.directory / "before-profiles.sqlite")
    h.seed()
    reader(h)
    return h


def reader(h):
    account = {"namespace": "qq", "immutable_account_id": "synthetic-reader-A"}
    h.add_origin("reader-first", dict(h.private, person_id=None), account)
    h.save_config()
    response = h.post(
        "identity/register",
        {"command": h.command("reader-first"), "account": account, "display_name": "same nickname"},
    )
    assert response.status_code == 200, response.text
    person = response.json()["person_id"]
    assert person != h.person
    for name, scope in (
        ("reader-group", dict(h.group, person_id=person)),
        ("reader-private", dict(h.private, person_id=person, conversation_id="reader-private")),
        ("reader-other", dict(h.group, person_id=person, conversation_id="group-other")),
    ):
        h.add_origin(name, scope, account)
    h.save_config()
    return person


def profile_request(
    h, origin="reader-group", target=None, query="咖啡", selection=None, budget=100000, known=None
):
    return {
        "query": h.query(origin),
        "requester_scope": copy.deepcopy(h.config["origins"][origin]["allowed_scope"]),
        "target": target or {"kind": "person", "person_id": h.person},
        "query_text": query,
        "selection": selection or ["interest"],
        "known_scope_version": known,
        "budget": {"tokens": budget, "bytes": budget},
    }


def select_profile(h, **kwargs):
    request = profile_request(h, **kwargs)
    return profiles.select(
        h.service, request, h.config["origins"][request["query"]["origin"]["assertion_ref"]]
    )


def test_a_queries_b_only_explicit_shared_projection_and_no_existence_leak(p):
    h = p
    hidden = select_profile(h)
    absent = select_profile(h, target={"kind": "person", "person_id": "absent"})
    for key in ("selected_units", "dependency_groups", "scope_version", "budget_used", "omissions"):
        assert hidden[key] == absent[key]
    result = publish(h, profile_draft(h))
    selected = select_profile(h)
    assert [u["record_id"] for u in selected["selected_units"]] == result["record_ids"]
    assert selected["selected_units"][0]["conditions"] == ["白天偶尔可以"]
    for raw in (
        "synthetic-private-channel",
        "message_key",
        "receipt_id",
        "subject_person_id",
        "relationship_delta",
    ):
        assert raw not in canonical(selected)
    for origin in ("reader-private", "reader-other"):
        assert select_profile(h, origin=origin)["selected_units"] == selected["selected_units"]


def test_same_person_two_group_styles_and_real_group_subject(p):
    h = p
    h.add_origin("owner-other", dict(h.group, conversation_id="group-other"))
    for origin, statement in (
        ("origin-group", "讨论技术时保持严谨"),
        ("owner-other", "游戏讨论可以玩梗"),
    ):
        scope = h.config["origins"][origin]["allowed_scope"]
        publish(
            h,
            profile_draft(
                h,
                sharing="group_only",
                conversation=scope["conversation_id"],
                category="style",
                field="style.expression",
                units=[h.unit(statement=statement, conditions=[], negations=[])],
            ),
            origin,
        )
    first = select_profile(h, query="field:style.expression", selection=["style"])
    second = select_profile(
        h, origin="reader-other", query="field:style.expression", selection=["style"]
    )
    assert [u["statement"] for u in first["selected_units"]] == ["讨论技术时保持严谨"]
    assert [u["statement"] for u in second["selected_units"]] == ["游戏讨论可以玩梗"]
    assert (
        select_profile(
            h, origin="reader-private", query="field:style.expression", selection=["style"]
        )["selected_units"]
        == []
    )
    source = h.source("group-topic")
    h.workflow.observe_source(source, h.group)
    target = {"kind": "group", "conversation_id": h.group["conversation_id"]}
    publish(
        h,
        profile_draft(
            h,
            subject=target,
            sharing="group_only",
            source_scope=h.group,
            conversation=h.group["conversation_id"],
            category="topic",
            field="topic.technology",
            units=[
                h.unit(
                    source,
                    statement="本群讨论技术",
                    conditions=[],
                    negations=[],
                    uncertainty="inferred",
                )
            ],
        ),
        "origin-group",
    )
    result = select_profile(h, target=target, query="field:topic.technology", selection=["topic"])
    assert result["selected_units"][0]["subject"] == target
    with pytest.raises(Fault, match="forbidden"):
        select_profile(h, origin="reader-other", target=target, selection=["topic"], budget=0)


@pytest.mark.parametrize("dimension", ["bytes", "tokens"])
def test_profile_zero_probe_does_not_read_body_and_still_checks_authority(
    p, monkeypatch, dimension
):
    from test_recall_regressions import guarded_sql

    h = p
    publish(h, profile_draft(h))
    request = profile_request(h)
    request["budget"][dimension] = 0
    context = h.config["origins"]["reader-group"]
    guarded_sql(h, monkeypatch, zero_content=True)
    result = profiles.select(h.service, request, context)
    assert result["selected_units"] == [] and result["budget_used"] == {"tokens": 0, "bytes": 0}
    with pytest.raises(Fault, match="scope_changed"):
        profiles.select(
            h.service, dict(request, known_scope_version=result["scope_version"] + 1), context
        )
    for key, value in (
        ("person_id", h.person),
        ("actor_id", "wrong-actor"),
        ("conversation_id", "elsewhere"),
    ):
        changed = copy.deepcopy(request)
        changed["requester_scope"][key] = value
        with pytest.raises(Fault, match="forbidden"):
            profiles.select(h.service, changed, context)
    with pytest.raises(Fault, match="dependency_unavailable"):
        profiles.select(
            h.service,
            request,
            dict(context, allowed_scope=dict(context["allowed_scope"], conversation_id=None)),
        )


def test_profile_ranking_exact_pruning_and_complete_budget(p, monkeypatch):
    from test_recall_regressions import controlled_group_ids, guarded_sql

    h = p
    controlled_group_ids(monkeypatch, ["group:000-distractor", "group:zzz-required"])
    publish(
        h,
        profile_draft(
            h,
            field="interest.cup",
            units=[
                h.unit(
                    statement="咖啡杯是蓝色，也喜欢电影音乐阅读旅行游泳",
                    conditions=[],
                    negations=[],
                )
            ],
        ),
    )
    publish(
        h,
        profile_draft(
            h,
            field="interest.flavour",
            units=[
                h.unit(statement="咖啡口味偏淡", conditions=["白天偶尔可以"], negations=[]),
                h.unit(statement="晚上不喝咖啡"),
            ],
        ),
    )
    expected = select_profile(h, query="field:interest.flavour")
    cost = expected["budget_used"]["bytes"]
    actual = select_profile(h, query="咖啡口味", budget=cost)
    assert (
        actual["selected_units"] == expected["selected_units"]
        and len(actual["selected_units"]) == 2
    )
    assert (
        select_profile(h, query="field:interest.flavour", budget=cost - 1)["selected_units"] == []
    )
    assert select_profile(h, query="晚上好呀")["selected_units"] == []
    statements, _ = guarded_sql(h, monkeypatch, zero_content=False)
    assert (
        select_profile(h, query="field:interest.flavour")["selected_units"]
        == expected["selected_units"]
    )
    body_reads = [s for s in statements if "FROM records WHERE" in s]
    assert len(body_reads) == 1 and "group:zzz-required" in body_reads[0]


def test_private_changes_and_other_group_changes_do_not_expose_private_versions(p):
    h = p
    initial = select_profile(h)
    private_id = h.select()["selected_units"][0]["record_id"]
    assert h.post("memory/revise", h.revision(private_id)).status_code == 200
    assert select_profile(h)["scope_version"] == initial["scope_version"]
    source = h.source("new-unrelated-interest")
    h.workflow.observe_source(source, h.private)
    h.add_origin("owner-other", dict(h.group, conversation_id="group-other"))
    publish(
        h,
        profile_draft(h, sharing="group_only", conversation="group-other", units=[h.unit(source)]),
        "owner-other",
    )
    assert select_profile(h)["scope_version"] == initial["scope_version"]
    assert select_profile(h, origin="reader-other")["scope_version"] > initial["scope_version"]


@pytest.mark.parametrize(
    "mutation", ["missing_member", "stale_projection", "wrong_subject", "withdraw_source"]
)
def test_profile_dependency_failure_removes_whole_group(p, mutation):
    h = p
    result = publish(h, profile_draft(h, units=[h.unit(), h.unit(statement="晚上不喝咖啡")]))
    if mutation == "withdraw_source":
        h.workflow.observe_source(h.source(), h.private, state="withdrawn")
    else:
        with h.store.transaction() as db:
            record = result["record_ids"][0]
            if mutation == "missing_member":
                db.execute(
                    "UPDATE groups SET members=? WHERE id=?",
                    (canonical(result["record_ids"] + ["absent"]), result["group_id"]),
                )
            elif mutation == "stale_projection":
                db.execute("UPDATE projections SET version=version+1 WHERE record_id=?", (record,))
            else:
                import json

                payload = json.loads(
                    db.execute("SELECT payload FROM records WHERE id=?", (record,)).fetchone()[0]
                )
                payload["subject"] = {"kind": "person", "person_id": "wrong-target"}
                db.execute("UPDATE records SET payload=? WHERE id=?", (canonical(payload), record))
    result = select_profile(h)
    assert result["selected_units"] == [] and result["dependency_groups"] == []
    assert result["omissions"] == ["no_match"]


def test_rebuild_and_v1_compatibility_after_new_profile_subjects(p):
    h = p
    before = h.select()
    publish(h, profile_draft(h))
    assert h.workflow.rebuild_index()["indexed_records"] == 2
    assert h.select()["selected_units"] == before["selected_units"]
    assert select_profile(h)["selected_units"]
    request = h.selection(scope=h.group)
    request["query"] = h.query("reader-group")
    assert h.post("memory/select", request).status_code == 403
