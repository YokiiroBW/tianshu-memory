"""A correction can omit the old locator without making the old value current."""

import pytest
from test_automatic_facts import add, initialize, recall
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
