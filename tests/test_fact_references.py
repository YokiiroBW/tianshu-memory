"""A correction can omit the old locator without making the old value current."""

import pytest
from manual_memory_fixtures import add, initialize, recall
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync
from test_user_actions import EXPIRES, execute, revision, setup_user


def select(h, endpoint, text):
    if endpoint == "legacy":
        return recall(h, text)
    from tianshu_memory.memory_context import MemoryContext

    request = dict(
        h.selection(budget=100000),
        query_text=text,
        selection=["evidence"],
        include_associated=False,
        time_range=None,
        limit=32,
        known_association_version=None,
        known_scope_checks=None,
    )
    return MemoryContext(h.service).query(request, h.contexts["synthetic-viewer:0"])[
        "selected_units"
    ]


def plans(h):
    initialize(h)
    old, _ = add(
        h,
        "我周日约人在银杏咖啡馆，先在东门等。",
        "event.appointment",
        keywords=["周日", "银杏咖啡馆", "东门"],
    )
    current, _ = add(
        h,
        "周日东门太晒了，换到后门等。",
        "event.appointment",
        second=2,
        keywords=["周日", "后门", "等候"],
    )
    return old, current


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_old_activity_locator_finds_only_its_current_correction(sync, endpoint):
    old, current = plans(sync)
    units = select(sync, endpoint, "我周日咖啡馆那场，最后说的是哪边等？")
    assert {u["record_id"] for u in units} == set(current["record_ids"])
    assert not set(old["record_ids"]).intersection(u["record_id"] for u in units)
    assert "换到后门" in units[0]["statement"]
    assert select(sync, endpoint, "我的护照号码是什么？") == []


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_old_locator_follows_multiple_existing_replacements(sync, endpoint):
    plans(sync)
    newest, _ = add(
        sync,
        "最后还是在二楼等。",
        "event.appointment",
        second=3,
        keywords=["二楼", "等候"],
    )
    assert {u["record_id"] for u in select(sync, endpoint, "银杏咖啡馆最后在哪儿等？")} == set(
        newest["record_ids"]
    )


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_corrected_plan_is_recalled_without_chat_history_for_a_question_suffix(sync, endpoint):
    initialize(sync)
    old, _ = add(
        sync,
        "计划周六去云杉园碰面，先从南门进入。",
        "event.meetup",
        value="周六去云杉园碰面，从南门进入",
        conditions=["周六"],
        keywords=["云杉园", "碰面", "周六", "南门"],
    )
    current, _ = add(
        sync,
        "计划周六去云杉园碰面，改从东门进入，因为南门离车站太远。",
        "event.meetup",
        value="周六去云杉园碰面，改从东门进入；南门离车站太远",
        second=2,
        conditions=["周六"],
        negations=["南门"],
        keywords=["云杉园", "碰面", "周六", "东门", "南门", "公交路线"],
    )
    units = select(sync, endpoint, "周六云杉园碰面的入口现在定哪边？")
    assert {u["record_id"] for u in units} == set(current["record_ids"])
    assert not set(old["record_ids"]).intersection(u["record_id"] for u in units)
    assert select(sync, endpoint, "周六雪松馆碰面的入口现在定哪边？") == []
    assert select(sync, endpoint, "我的银行卡号码是什么？") == []


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_question_word_inside_an_explicit_title_remains_a_literal_name(sync, endpoint):
    initialize(sync)
    written, _ = add(sync, "我的笔记本叫《哪边》。", "notebook.name", value="哪边")
    units = select(sync, endpoint, "《哪边》")
    assert {u["record_id"] for u in units} == set(written["record_ids"])


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_location_question_still_uses_its_owned_object_focus(sync, endpoint):
    initialize(sync)
    written, _ = add(sync, "我的手机通常收在门边柜子。", "phone.storage", value="门边柜子")
    units = select(sync, endpoint, "我的手机存放在哪儿？")
    assert {u["record_id"] for u in units} == set(written["record_ids"])


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_natural_question_retrieves_a_persisted_meeting_phrase(sync, endpoint):
    initialize(sync)
    written, _ = add(
        sync,
        "提议周日去松林书店，到门口碰头时说月光纸船。",
        "event.meetup",
        keywords=["松林书店", "周日", "碰头", "月光纸船"],
    )
    for text in ("书店碰头的那句话是什么来着？", "刚才书店碰头的那句话是什么？"):
        assert {u["record_id"] for u in select(sync, endpoint, text)} == set(written["record_ids"])
    assert select(sync, endpoint, "公交卡上的那句话是什么来着？") == []


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_deictic_words_in_quoted_names_are_not_discarded(sync, endpoint):
    initialize(sync)
    written, _ = add(sync, "我的笔记本叫《那句话》。", "notebook.name", value="那句话")
    for text in ("《那句话》", "‘那句话’", "'那句话'"):
        assert {u["record_id"] for u in select(sync, endpoint, text)} == set(written["record_ids"])


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
@pytest.mark.parametrize("invalidated", ["old_source", "old_record", "current_record"])
def test_invalidated_reference_never_restores_a_hidden_locator(sync, endpoint, invalidated):
    old, current = plans(sync)
    if invalidated == "old_source":
        grant = next(g for g in sync.grants if g["entry_id"] == "quality-entry:1")
        grant["state"] = "denied"
        sync.platform_head["sequence"] += 1
    else:
        record_id = (old if invalidated == "old_record" else current)["record_ids"][0]
        request = revision(sync, record_id, kind="forget")
        if invalidated == "old_record":
            request["expected_version"] = 2
        execute(
            setup_user(sync),
            dict(operation="confirm_revision", request=request, expires_at=EXPIRES),
        )
        response = sync.post("memory/revise", request)
        assert response.status_code == 200, response.text
    assert select(sync, endpoint, "银杏咖啡馆最后在哪儿等？") == []
