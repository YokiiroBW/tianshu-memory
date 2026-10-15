"""Natural language incidents from the social benchmark, through sourced native writes."""

import copy

import pytest
from test_automatic_facts import add, initialize, recall
from test_candidate_extraction import PATH
from test_fact_entities import entity, saved_entity
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


@pytest.mark.parametrize(
    "denial",
    [
        "没有跟着换回去喝桂花乌龙",
        "不喝桂花乌龙了",
        "现在很少碰桂花乌龙",
    ],
)
def test_denial_of_old_drink_keeps_new_positive_preference(sync, denial):
    initialize(sync)
    old, _ = add(sync, "我通常选桂花乌龙", "beverage.preference", "通常选桂花乌龙")
    current, _ = add(
        sync,
        "我平时最常点热豆浆，现在很少碰桂花乌龙",
        "beverage.preference",
        "平时最常点热豆浆，现在很少碰桂花乌龙",
        second=2,
    )
    add(sync, "我" + denial, "beverage.preference", denial, second=3)
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?",
                (current["group_ids"][0],),
            ).fetchone()[0]
            is None
        )
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?", (old["group_ids"][0],)
            ).fetchone()[0]
            == current["group_ids"][0]
        )


def test_direct_denial_of_current_drink_invalidates_positive_without_inventing_replacement(sync):
    initialize(sync)
    old, _ = add(sync, "我最常喝热豆浆", "beverage.preference", "热豆浆")
    denied, _ = add(sync, "我不喝热豆浆了", "beverage.preference", "不喝热豆浆了", second=2)
    units = recall(sync, "我喝什么")
    assert not set(old["record_ids"]).intersection(u["record_id"] for u in units)
    assert set(denied["record_ids"]) == {u["record_id"] for u in units}


def test_mixed_denial_and_new_assertion_can_update_preference(sync):
    initialize(sync)
    add(sync, "我最常点热豆浆", "beverage.preference", "热豆浆")
    current, _ = add(
        sync,
        "我不喝豆浆了，现在常点燕麦奶",
        "beverage.preference",
        "不喝豆浆了，现在常点燕麦奶",
        second=2,
    )
    assert {u["record_id"] for u in recall(sync, "我喝什么")} == set(current["record_ids"])


@pytest.mark.parametrize(
    "query",
    [
        "我卡包一般搁哪儿",
        "我的卡包平时放哪来着",
        "出门找不到卡包了，通常收在哪个地方？",
        "卡包又找不着了，我平时到底搁哪儿来着？",
    ],
)
def test_colloquial_location_questions_keep_the_object_anchor(sync, query):
    initialize(sync)
    wanted, _ = add(sync, "我的卡包通常放在玄关右边第二格", "wallet.location", "玄关右边第二格")
    add(sync, "我的钥匙放在书房桌上", "key.location", "书房桌上", second=2)
    add(sync, "我平时常喝热豆浆", "beverage.preference", "热豆浆", second=3)
    assert {u["record_id"] for u in recall(sync, query)} == set(wanted["record_ids"])


@pytest.mark.parametrize(
    "query", ["给我带一杯喝的，按我平时口味选就行", "我平时爱喝啥", "帮我点杯饮料，选我常喝的"]
)
def test_implicit_drink_request_recalls_only_drink_facts(sync, query):
    initialize(sync)
    wanted, _ = add(
        sync, "我最常点热豆浆", "beverage.preference", "热豆浆", keywords=["饮料", "口味"]
    )
    add(sync, "我吃面不放香菜", "diet.cilantro", "不放香菜", second=2)
    add(sync, "我的水杯放在玄关", "cup.location", "玄关", second=3)
    assert {u["record_id"] for u in recall(sync, query)} == set(wanted["record_ids"])
    assert recall(sync, "我想买个喝水用的杯子") == []


def test_ordinal_without_model_supplied_id_resolves_unique_existing_object(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "第一本笔记叫《晨光》",
        "notebook.title",
        "晨光",
        entity=entity("第一本笔记", kind="notebook"),
    )
    second, _ = add(
        sync,
        "第二本笔记叫《暮色》",
        "notebook.title",
        "暮色",
        second=2,
        entity=entity("第二本笔记", kind="notebook"),
    )
    changed, _ = add(
        sync,
        "第二本改叫《夜航》",
        "notebook.title",
        "夜航",
        second=3,
        entity=entity("第二本", kind="notebook"),
    )
    assert saved_entity(sync, changed)["id"] == saved_entity(sync, second)["id"]
    assert {u["record_id"] for u in recall(sync, "第一本笔记叫什么")} == set(first["record_ids"])
    assert {u["record_id"] for u in recall(sync, "第二本笔记叫什么")} == set(changed["record_ids"])
    assert {u["record_id"] for u in recall(sync, "第一本和第二本笔记现在分别叫什么")} == set(
        first["record_ids"] + changed["record_ids"]
    )
    unchanged = recall(sync, "第一本还是原来的，第二本笔记叫什么？")
    assert not set(second["record_ids"]).intersection(u["record_id"] for u in unchanged)
    assert set(changed["record_ids"]).issubset(u["record_id"] for u in unchanged)
    historical = recall(sync, "第二本笔记以前叫什么？")
    assert set(second["record_ids"]).issubset(u["record_id"] for u in historical)


@pytest.mark.parametrize(
    "text",
    [
        "我有两本笔记，第一本叫《晨光》，第二本叫《暮色》。",
        "桌上两本都快写满了，第一本笔记本叫《晨光》，第二本叫《暮色》。",
    ],
)
def test_compound_source_can_establish_two_sourced_numbered_notebooks(sync, text):
    initialize(sync)
    mention = "第一本笔记本" if "第一本笔记本" in text else "第一本"
    first, _ = add(sync, text, "notebook.title", "晨光", entity=entity(mention, kind="notebook"))
    second, _ = add(
        sync, text, "notebook.title", "暮色", second=2, entity=entity("第二本", kind="notebook")
    )
    assert saved_entity(sync, first)["id"] != saved_entity(sync, second)["id"]
    assert {u["record_id"] for u in recall(sync, "第二本笔记的标题")} == set(second["record_ids"])


def test_negative_looking_literal_title_is_not_a_denial(sync):
    initialize(sync)
    add(
        sync,
        "我的第一本笔记叫《晨光》",
        "notebook.title",
        "晨光",
        entity=entity("第一本笔记", kind="notebook"),
    )
    changed, _ = add(
        sync,
        "第一本笔记改叫《不喝奶茶》",
        "notebook.title",
        "不喝奶茶",
        second=2,
        entity=entity("第一本笔记", kind="notebook"),
    )
    assert {u["record_id"] for u in recall(sync, "第一本笔记叫什么")} == set(changed["record_ids"])


def test_bad_object_rejects_whole_batch_then_same_job_accepts_fresh_proposal_once(sync):
    initialize(sync)
    original = sync.post
    proposals = []

    def insert_bad_object(path, body):
        if path == PATH and body["output"] is not None:
            proposals.append(copy.deepcopy(body["output"]))
            broken = copy.deepcopy(body)
            invalid = copy.deepcopy(broken["output"]["memories"][0])
            invalid["fact"].update(
                key="pen.color", entity=entity("第一支钢笔", "entity:" + "f" * 64)
            )
            broken["output"]["memories"].append(invalid)
            return original(path, broken)
        return original(path, body)

    sync.post = insert_bad_object
    _, job = add(
        sync,
        "第一支钢笔用蓝墨水",
        "pen.ink",
        "蓝色",
        entity=entity("第一支钢笔"),
        expected_state="rejected",
    )
    sync.post = original
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    result = sync.post(PATH, dict(job_id=job, output=proposals[0]))
    assert result.status_code == 200 and result.json()["state"] == "completed"
    assert sync.post(PATH, dict(job_id=job, output=proposals[0])).json() == result.json()
    assert len(recall(sync, "第一支钢笔的墨水")) == 1


def test_ambiguous_new_ordinal_is_rejected_instead_of_guessing_object_type(sync):
    initialize(sync)
    add(
        sync,
        "我有两本笔记，两本书，第一本叫《晨光》",
        "notebook.title",
        "晨光",
        entity=entity("第一本", kind="notebook"),
        expected_state="rejected",
    )


def test_conditional_denial_does_not_erase_general_preference(sync):
    initialize(sync)
    current, _ = add(sync, "我喜欢拿铁", "beverage.preference", "拿铁")
    add(
        sync,
        "我晚上不喝拿铁",
        "beverage.preference",
        "晚上不喝拿铁",
        second=2,
        conditions=["晚上"],
        negations=["不喝拿铁"],
    )
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?",
                (current["group_ids"][0],),
            ).fetchone()[0]
            is None
        )
