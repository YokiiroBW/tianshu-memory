"""Subjectless conversation references retain the existing guarded group query."""

import json

import pytest
from test_automatic_facts import add
from test_group_context import (
    cat,
    owner,
    query,
    recalled,
)
from test_group_context import (
    certificates as certificates,
)
from test_group_context import (
    room as room,
)
from test_group_context import (
    source_contracts as source_contracts,
)
from test_group_context import (
    source_examples as source_examples,
)

from tianshu_memory.recall_language import (
    explicit_named_reference,
    history_query,
    named_reference,
    query_focus,
)


@pytest.mark.parametrize(
    "text",
    [
        "刚才群里提到的那只猫叫什么？",
        "之前提到过的那只猫叫什么来着？",
        "群里说起过的那只猫叫什么？",
        "前面说过的那只猫叫什么？",
    ],
)
def test_subjectless_reporting_words_are_not_an_unknown_name(text):
    assert not explicit_named_reference(text)


@pytest.mark.parametrize(
    "text",
    [
        "阿宁家的那只猫叫什么？",
        "阿宁那只猫叫什么？",
        "阿宁提过的那只猫叫什么？",
        "咖啡提到过的那只猫叫什么？",
        "不是阿宁提过的那只猫叫什么？",
        "如果阿宁提过的那只猫叫什么？",
        "阿川和小禾说过的那只猫叫什么？",
        "阿川提过的小禾家的那只猫叫什么？",
    ],
)
def test_unresolved_or_unsupported_people_still_constrain_the_query(text):
    assert explicit_named_reference(text)


@pytest.mark.parametrize(
    ("text", "focus", "historical"),
    [
        ("之前提到过的那只猫现在叫什么？", "猫", False),
        ("之前提到过的那只猫以前叫什么？", "猫", True),
        ("前面说过的那个杯子叫什么？", "杯子", False),
        ("群里提到过的杯子叫什么？", "杯子", False),
    ],
)
def test_discourse_focus_drops_reporting_words_but_keeps_object_history(text, focus, historical):
    assert query_focus(text) == focus
    assert history_query(text) is historical


def test_group_discourse_reference_uses_committed_other_owner(room):
    h = room
    author = owner(h)
    cat(h, author, "栗团")
    selected = recalled(h, query(h, "刚才群里提到的那只猫叫什么？"))
    assert [unit["subject_person_id"] for unit in selected["selected_units"]] == [
        h.scope(author)["person_id"]
    ]
    assert "栗团" in selected["selected_units"][0]["statement"]
    assert [check["scope"] for check in selected["scope_checks"]] == [h.scope(), h.scope(author)]


def test_unnamed_reference_uses_the_existing_cup_topic(room):
    h = room
    author = owner(h)
    add(h, "我的杯子叫星渡", "cup.name", "星渡", actor=author)
    for text in ("前面说过的那个杯子叫什么？", "群里提到过的杯子叫什么？"):
        selected = recalled(h, query(h, text))
        assert len(selected["selected_units"]) == 1
        assert selected["selected_units"][0]["subject_person_id"] == h.scope(author)["person_id"]
        assert "星渡" in selected["selected_units"][0]["statement"]


def test_named_reference_keeps_a_real_canonical_witness_ahead_of_discourse(room):
    h = room
    a, b = owner(h), owner(h)
    add(h, "我叫提到的", "identity.name", "提到的", actor=a, second=1)
    cat(h, a, "栗团", second=2)
    cat(h, b, "栗糖", second=3)
    text = "提到的那只猫叫什么？"
    assert not explicit_named_reference(text)
    assert named_reference(text, "提到的") == "那只猫叫什么？"
    selected = recalled(h, query(h, text))
    assert len(selected["selected_units"]) == 2  # original name group and target group
    assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
        h.scope(a)["person_id"]
    }
    assert "栗团" in json.dumps(selected, ensure_ascii=False)
    assert "栗糖" not in json.dumps(selected, ensure_ascii=False)


def test_generic_multiple_sources_do_not_assert_a_unique_person(room):
    h = room
    a, b = owner(h), owner(h)
    cat(h, a, "栗团", second=1)
    cat(h, b, "栗糖", second=2)
    selected = recalled(h, query(h, "刚才群里提到的那只猫叫什么？"))
    assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
        h.scope(a)["person_id"],
        h.scope(b)["person_id"],
    }
    assert len(selected["selected_units"]) == 2


def test_discourse_does_not_override_unknown_names_or_invisible_scopes(room):
    h = room
    visible = owner(h)
    private = owner(h, audience="self_private")
    elsewhere = owner(h, conversation="other-room")
    cat(h, visible, "栗团", second=1)
    cat(h, private, "隐雪", second=2)
    cat(h, elsewhere, "远雾", second=3)
    for text in ("阿宁家的那只猫叫什么？", "阿宁那只猫叫什么？", "阿宁说过的那只猫叫什么？"):
        assert recalled(h, query(h, text))["selected_units"] == []
    selected = recalled(h, query(h, "刚才群里提到的那只猫叫什么？"))
    assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
        h.scope(visible)["person_id"]
    }
    assert "隐雪" not in json.dumps(selected, ensure_ascii=False)
    assert "远雾" not in json.dumps(selected, ensure_ascii=False)


def test_discourse_current_correction_and_source_withdrawal_stay_guarded(room):
    h = room
    author = owner(h)
    cat(h, author, "栗团", second=1)
    add(h, "我的猫改叫栗糖", "pet.cat.name", "栗糖", actor=author, second=2)
    text = "刚才群里提到的那只猫现在叫什么？"
    selected = recalled(h, query(h, text))
    assert len(selected["selected_units"]) == 1
    assert "栗糖" in selected["selected_units"][0]["statement"]
    assert "栗团" not in selected["selected_units"][0]["statement"]
    h.physicals[-1]["state"] = "withdrawn"
    h.core_head["sequence"] += 1
    request = query(
        h,
        text,
        budget={"tokens": 0, "bytes": 0},
        known_scope_version=selected["scope_version"],
        known_association_version=selected["association_version"],
        known_scope_checks=selected["scope_checks"],
    )
    assert h.post("memory/context/query", request).status_code in {403, 409, 503}


def test_subjectless_reporting_time_does_not_replace_object_history(room):
    h = room
    author = owner(h)
    cat(h, author, "栗团", second=1)
    add(h, "我的猫改叫栗糖", "pet.cat.name", "栗糖", actor=author, second=2)
    current = recalled(h, query(h, "之前提到过的猫现在叫什么？"))
    assert {unit["statement"] for unit in current["selected_units"]} == {"我的猫改叫栗糖"}
    for text in (
        "之前提到过的猫以前叫什么？",
        "之前提到过的以前那只猫现在叫什么？",
        "之前提到过的猫原名是什么？",
    ):
        selected = recalled(h, query(h, text))
        assert "我的猫叫栗团" in {unit["statement"] for unit in selected["selected_units"]}
        assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
            h.scope(author)["person_id"]
        }
