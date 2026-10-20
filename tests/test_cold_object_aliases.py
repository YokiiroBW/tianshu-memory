"""Cold lexical recall through the guarded store, including old persisted indexes."""

import pytest
from test_automatic_facts import add, initialize, recall
from test_fact_entities import entity, saved_entity
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


@pytest.mark.parametrize("mention", ["蓝杯子", "蓝色杯子", "蓝色水杯", "蓝的杯子"])
def test_cold_cup_alias_preserves_color_without_recent_context(sync, mention):
    initialize(sync)
    blue, _ = add(sync, "我的蓝色水杯叫雨点", "cup.name", "雨点")
    add(sync, "我的红色水杯叫晚霞", "cup.red.name", "晚霞", second=2)
    units = recall(sync, f"我的{mention}叫啥？")
    assert {u["record_id"] for u in units} == set(blue["record_ids"])
    assert all("晚霞" not in u["statement"] for u in units)
    both = recall(sync, f"我的{mention}叫啥，平时我把钥匙放哪里？")
    assert {u["record_id"] for u in both} == set(blue["record_ids"])


def test_alias_correction_reuses_the_sourced_object_not_its_sibling(sync):
    initialize(sync)
    blue, _ = add(
        sync, "我的蓝色水杯叫雨点", "cup.name", "雨点", entity=entity("蓝色水杯", kind="cup")
    )
    red, _ = add(
        sync,
        "我的红色水杯叫晚霞",
        "cup.name",
        "晚霞",
        second=2,
        entity=entity("红色水杯", kind="cup"),
    )
    renamed, _ = add(
        sync,
        "蓝杯子现在改叫小雨",
        "cup.name",
        "小雨",
        second=3,
        entity=entity("蓝杯子", kind="cup"),
    )
    assert saved_entity(sync, blue)["id"] == saved_entity(sync, renamed)["id"]
    assert saved_entity(sync, red)["id"] != saved_entity(sync, renamed)["id"]
    assert {u["record_id"] for u in recall(sync, "我的蓝杯子叫啥")} == set(renamed["record_ids"])


def test_cup_alias_keeps_material_ordinal_and_owner_constraints(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "第一只蓝色玻璃水杯叫清露",
        "cup.name",
        "清露",
        entity=entity("第一只蓝色玻璃水杯", kind="cup"),
    )
    add(
        sync,
        "第二只蓝色玻璃水杯叫晨雾",
        "cup.name",
        "晨雾",
        second=2,
        entity=entity("第二只蓝色玻璃水杯", kind="cup"),
    )
    add(
        sync,
        "第一只蓝色陶瓷水杯叫青瓷",
        "cup.name",
        "青瓷",
        second=3,
        entity=entity("第一只蓝色陶瓷水杯", kind="cup"),
    )
    add(sync, "我姐姐的第一只蓝色玻璃水杯叫星辰", "sister.cup.name", "星辰", second=4)
    units = recall(sync, "我的第一只蓝玻璃杯子叫啥")
    assert {u["record_id"] for u in units} == set(first["record_ids"])
    assert recall(sync, "我的第三只蓝玻璃杯子叫啥") == []
    assert recall(sync, "我的第一只蓝不锈钢杯子叫啥") == []


def test_cup_alias_does_not_expand_quoted_titles_or_distinct_vessels(sync):
    initialize(sync)
    add(sync, "我的蓝色水杯叫雨点", "cup.name", "雨点")
    assert recall(sync, "我的蓝色保温杯叫啥") == []
    assert recall(sync, "我的蓝色马克杯叫啥") == []
    assert recall(sync, "书名《蓝杯子》是什么") == []


def test_new_cup_purchase_keeps_preferences_without_recalling_an_old_cup_location(sync):
    initialize(sync)
    add(sync, "我的水杯放在玄关", "cup.location", "玄关")
    preference, _ = add(sync, "我偏好玻璃水杯", "cup.preference", "玻璃水杯", second=2)
    units = recall(sync, "我想买个喝水用的杯子")
    assert {u["record_id"] for u in units} == set(preference["record_ids"])
