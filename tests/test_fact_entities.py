"""Object discrimination through native sourced candidate commits and retrieval."""

import json

import pytest
from test_automatic_facts import add, initialize, recall
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


def entity(mention, identity=None, kind="pen"):
    return dict(id=identity, kind=kind, mention=mention)


def saved_entity(h, result):
    with h.store.transaction() as db:
        row = db.execute(
            "SELECT payload FROM automatic_facts WHERE group_id=?", (result["group_ids"][0],)
        ).fetchone()
    return json.loads(row[0])["entity"]


@pytest.mark.parametrize("mention", ["我这个杯子", "我的那个水杯", "我这只白色杯子"])
def test_self_reference_corrects_the_unique_owned_object_in_one_commit(sync, mention):
    initialize(sync)
    original, _ = add(
        sync, "我的白色水杯叫浮云", "cup.name", "浮云", entity=entity("白色水杯", kind="cup")
    )
    prior = saved_entity(sync, original)
    changed, job = add(
        sync,
        mention + "后来改叫轻舟，另一个人的不变。",
        "cup.name",
        "轻舟",
        second=2,
        entity=entity(mention, prior["id"], "cup"),
    )
    assert saved_entity(sync, changed)["id"] == prior["id"]
    assert {u["record_id"] for u in recall(sync, "杯子现在叫什么")} == set(changed["record_ids"])
    assert {u["record_id"] for u in recall(sync, "我这个杯子现在叫什么")} == set(
        changed["record_ids"]
    )
    history = recall(sync, "杯子以前叫什么")
    assert set(original["record_ids"]) <= {u["record_id"] for u in history}
    receipt = sync.post("memory/candidates/process", dict(job_id=job, output=None))
    assert receipt.json()["state"] == "completed"


def test_generic_self_reference_cannot_choose_between_two_objects(sync):
    initialize(sync)
    white, _ = add(
        sync, "我的白色水杯叫浮云", "cup.name", "浮云", entity=entity("白色水杯", kind="cup")
    )
    blue, _ = add(
        sync,
        "我的蓝色水杯叫雪团",
        "cup.name",
        "雪团",
        second=2,
        entity=entity("蓝色水杯", kind="cup"),
    )
    add(
        sync,
        "我这个杯子后来改叫轻舟",
        "cup.name",
        "轻舟",
        second=3,
        entity=entity("我这个杯子", saved_entity(sync, white)["id"], "cup"),
        expected_state="rejected",
    )
    assert {u["record_id"] for u in recall(sync, "两只水杯叫什么")} == set(
        white["record_ids"] + blue["record_ids"]
    )


def test_qualified_self_reference_distinguishes_two_same_kind_objects(sync):
    initialize(sync)
    white, _ = add(
        sync, "我的白色水杯叫浮云", "cup.name", "浮云", entity=entity("白色水杯", kind="cup")
    )
    blue, _ = add(
        sync,
        "我的蓝色水杯叫雪团",
        "cup.name",
        "雪团",
        second=2,
        entity=entity("蓝色水杯", kind="cup"),
    )
    changed, _ = add(
        sync,
        "我这个白色杯子后来改叫轻舟",
        "cup.name",
        "轻舟",
        second=3,
        entity=entity("我这个白色杯子", saved_entity(sync, white)["id"], "cup"),
    )
    assert saved_entity(sync, changed)["id"] == saved_entity(sync, white)["id"]
    assert {u["record_id"] for u in recall(sync, "蓝色杯子叫什么")} == set(blue["record_ids"])


@pytest.mark.parametrize("case", ["foreign_id", "wrong_kind", "qualified_other", "third_party"])
def test_deictic_matching_does_not_override_identity_or_source_boundary(sync, case):
    initialize(sync)
    original, _ = add(
        sync, "我的白色水杯叫浮云", "cup.name", "浮云", entity=entity("白色水杯", kind="cup")
    )
    proposed = entity("我这个杯子", saved_entity(sync, original)["id"], "cup")
    text = "我这个杯子后来改叫轻舟"
    if case == "foreign_id":
        proposed["id"] = "entity:" + "f" * 64
    elif case == "wrong_kind":
        proposed["kind"] = "notebook"
    elif case == "qualified_other":
        proposed["mention"] = "我这个蓝色杯子"
        text = proposed["mention"] + "后来改叫轻舟"
    else:
        text = "朋友说：我这个杯子后来改叫轻舟"
    add(sync, text, "cup.name", "轻舟", second=2, entity=proposed, expected_state="rejected")
    assert {u["record_id"] for u in recall(sync, "白色杯子叫什么")} == set(original["record_ids"])


def test_deictic_reference_does_not_revive_a_forgotten_entity(sync):
    initialize(sync)
    original, _ = add(
        sync, "我的白色水杯叫浮云", "cup.name", "浮云", entity=entity("白色水杯", kind="cup")
    )
    one = saved_entity(sync, original)
    assert sync.post("memory/revise", sync.revision(original["record_ids"][0])).status_code == 200
    add(
        sync,
        "我这个杯子后来改叫轻舟",
        "cup.name",
        "轻舟",
        second=2,
        entity=entity("我这个杯子", one["id"], "cup"),
        expected_state="rejected",
    )
    assert recall(sync, "白色杯子叫什么") == []


def test_same_predicate_keeps_two_objects_and_corrects_only_its_target(sync):
    initialize(sync)
    first, _ = add(sync, "我的第一支钢笔用蓝色墨水", "pen.ink", "蓝色", entity=entity("第一支钢笔"))
    second, _ = add(
        sync, "我的第二支钢笔用红色墨水", "pen.ink", "红色", second=2, entity=entity("第二支钢笔")
    )
    one, two = saved_entity(sync, first), saved_entity(sync, second)
    assert one["id"] != two["id"]
    assert {u["record_id"] for u in recall(sync, "第一支钢笔用什么墨水")} == set(
        first["record_ids"]
    )
    assert {u["record_id"] for u in recall(sync, "第二支钢笔用什么墨水")} == set(
        second["record_ids"]
    )
    assert recall(sync, "第三支钢笔用什么墨水") == []
    repeated, _ = add(sync, "第一支钢笔仍然使用蓝色墨水", "pen.ink", "蓝色", second=3, entity=one)
    assert repeated["record_ids"] == first["record_ids"]
    changed, _ = add(sync, "第一支钢笔改用黑色墨水", "pen.ink", "黑色", second=4, entity=one)
    assert saved_entity(sync, changed)["id"] == one["id"]
    assert {u["record_id"] for u in recall(sync, "两支钢笔的墨水")} == set(
        second["record_ids"] + changed["record_ids"]
    )
    with sync.store.transaction() as db:
        prior = db.execute(
            "SELECT superseded_by FROM automatic_facts WHERE group_id=?", (first["group_ids"][0],)
        ).fetchone()[0]
        assert prior == changed["group_ids"][0]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automatic_facts WHERE superseded_by IS NULL"
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automatic_evidence WHERE group_id=?", (first["group_ids"][0],)
            ).fetchone()[0]
            == 2
        )


def test_same_value_and_predicate_do_not_merge_different_named_objects(sync):
    initialize(sync)
    red, _ = add(
        sync,
        "红色卡包放在抽屉",
        "wallet.location",
        "抽屉",
        entity=entity("红色卡包", kind="wallet"),
    )
    blue, _ = add(
        sync,
        "蓝色卡包放在抽屉",
        "wallet.location",
        "抽屉",
        second=2,
        entity=entity("蓝色卡包", kind="wallet"),
    )
    assert red["record_ids"] != blue["record_ids"]
    assert {u["record_id"] for u in recall(sync, "红色卡包在哪里")} == set(red["record_ids"])
    repeated, _ = add(
        sync,
        "我的红色卡包还在抽屉",
        "wallet.location",
        "抽屉",
        second=3,
        entity=entity("我的红色卡包", kind="wallet"),
    )
    assert repeated["record_ids"] == red["record_ids"]


def test_ordinal_paraphrase_reuses_object_across_attributes(sync):
    initialize(sync)
    first, _ = add(sync, "第一支钢笔用黑墨水", "pen.ink", "黑色", entity=entity("第一支钢笔"))
    color, _ = add(
        sync, "第1支笔的笔杆是白色", "pen.color", "白色", second=2, entity=entity("第1支笔")
    )
    assert saved_entity(sync, first)["id"] == saved_entity(sync, color)["id"]
    assert first["record_ids"] != color["record_ids"]


@pytest.mark.parametrize("wrong", ["foreign", "other_object", "ungrounded"])
def test_entity_hints_cannot_override_source_identity(sync, wrong):
    initialize(sync)
    first, _ = add(sync, "第一支钢笔用蓝墨水", "pen.ink", "蓝色", entity=entity("第一支钢笔"))
    one = saved_entity(sync, first)
    proposed = entity("第二支钢笔", one["id"])
    if wrong == "foreign":
        proposed["id"] = "entity:" + "f" * 64
    elif wrong == "ungrounded":
        proposed = one
    add(
        sync,
        "第二支钢笔用红墨水",
        "pen.ink",
        "红色",
        second=2,
        entity=proposed,
        expected_state="rejected",
    )
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 1
    assert {u["record_id"] for u in recall(sync, "第一支钢笔的墨水")} == set(first["record_ids"])


def test_legacy_fact_is_not_implicitly_assigned_to_a_new_object(sync):
    initialize(sync)
    legacy, _ = add(sync, "我的猫叫团团", "pet.cat.name", "团团")
    newer, _ = add(
        sync,
        "我的第二只猫叫圆圆",
        "pet.cat.name",
        "圆圆",
        second=2,
        entity=entity("第二只猫", kind="cat"),
    )
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?",
                (legacy["group_ids"][0],),
            ).fetchone()[0]
            is None
        )
    assert legacy["record_ids"] != newer["record_ids"]
    assert {u["record_id"] for u in recall(sync, "第二只猫叫什么")} == set(newer["record_ids"])


def test_numbered_subtypes_do_not_share_an_identity(sync):
    initialize(sync)
    fountain, _ = add(sync, "第一支钢笔用蓝墨水", "pen.ink", "蓝色", entity=entity("第一支钢笔"))
    ballpoint, _ = add(
        sync, "第一支圆珠笔用黑墨水", "pen.ink", "黑色", second=2, entity=entity("第一支圆珠笔")
    )
    assert saved_entity(sync, fountain)["id"] != saved_entity(sync, ballpoint)["id"]
    add(
        sync,
        "第1支笔用红墨水",
        "pen.ink",
        "红色",
        second=3,
        entity=entity("第1支笔", saved_entity(sync, fountain)["id"]),
        expected_state="rejected",
    )


def test_forgotten_object_reference_does_not_restore_its_fact(sync):
    initialize(sync)
    saved, _ = add(sync, "第一支钢笔用蓝墨水", "pen.ink", "蓝色", entity=entity("第一支钢笔"))
    one = saved_entity(sync, saved)
    response = sync.post("memory/revise", sync.revision(saved["record_ids"][0]))
    assert response.status_code == 200, response.text
    assert recall(sync, "第一支钢笔的墨水") == []
    add(
        sync,
        "第一支钢笔用红墨水",
        "pen.ink",
        "红色",
        second=2,
        entity=one,
        expected_state="rejected",
    )
    assert recall(sync, "第一支钢笔的墨水") == []


def test_quoted_replacement_retrieves_old_target_before_background_update(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "第一本笔记本标题是《晨光》",
        "notebook.title",
        "晨光",
        entity=entity("第一本笔记本", kind="notebook"),
    )
    second, _ = add(
        sync,
        "第二本笔记本标题是《暮色》",
        "notebook.title",
        "暮色",
        second=2,
        entity=entity("第二本笔记本", kind="notebook"),
    )
    assert {u["record_id"] for u in recall(sync, "第二本笔记本的标题更正为《夜航》")} == set(
        second["record_ids"]
    )
    assert recall(sync, "哪本笔记本叫《夜航》") == []
    assert {u["record_id"] for u in recall(sync, "第一本笔记本叫什么")} == set(first["record_ids"])


def test_numbered_recall_aliases_keep_ordinals_with_their_objects(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "第一本笔记本标题是《晨光》",
        "notebook.title",
        "晨光",
        entity=entity("第一本笔记本", kind="notebook"),
    )
    second, _ = add(
        sync,
        "第二本笔记本标题是《暮色》",
        "notebook.title",
        "暮色",
        second=2,
        entity=entity("第二本笔记本", kind="notebook"),
    )
    cat, _ = add(
        sync,
        "第一只猫叫团团",
        "cat.name",
        "团团",
        second=3,
        entity=entity("第一只猫", kind="cat"),
    )
    assert {u["record_id"] for u in recall(sync, "只告诉我第二本笔记的标题。")} == set(
        second["record_ids"]
    )
    assert recall(sync, "第三本笔记的标题是什么") == []
    assert {u["record_id"] for u in recall(sync, "第二本笔记的标题、第一只猫的名字")} == set(
        second["record_ids"] + cat["record_ids"]
    )
    assert {u["record_id"] for u in recall(sync, "第一本笔记本、第二本笔记的标题")} == set(
        first["record_ids"] + second["record_ids"]
    )


def test_ordinal_only_reference_requires_unique_existing_sourced_identity(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "第一本笔记本标题是《晨光》",
        "notebook.title",
        "晨光",
        entity=entity("第一本笔记本", kind="notebook"),
    )
    one = saved_entity(sync, first)
    short = entity("第一本", one["id"], kind="notebook")
    repeated, _ = add(sync, "第一本还是《晨光》", "notebook.title", "晨光", second=2, entity=short)
    assert repeated["record_ids"] == first["record_ids"]
    changed, _ = add(sync, "第一本改叫《暮色》", "notebook.title", "暮色", second=3, entity=short)
    assert saved_entity(sync, changed)["id"] == one["id"]
    assert {u["record_id"] for u in recall(sync, "第一本笔记的标题")} == set(changed["record_ids"])
    assert recall(sync, "第三本笔记叫什么") == []
    add(
        sync,
        "第二本叫《夜航》",
        "notebook.title",
        "夜航",
        second=4,
        entity=entity("第二本", kind="notebook"),
        expected_state="rejected",
    )
    add(
        sync,
        "第二本叫《夜航》",
        "notebook.title",
        "夜航",
        second=5,
        entity=entity("第二本", one["id"], kind="notebook"),
        expected_state="rejected",
    )


def test_ordinal_only_reference_cannot_choose_between_numbered_subtypes(sync):
    initialize(sync)
    one, _ = add(sync, "第一支钢笔用蓝墨水", "pen.ink", "蓝色", entity=entity("第一支钢笔"))
    add(sync, "第一支圆珠笔用黑墨水", "pen.ink", "黑色", second=2, entity=entity("第一支圆珠笔"))
    add(
        sync,
        "第一支改用红墨水",
        "pen.ink",
        "红色",
        second=3,
        entity=entity("第一支", saved_entity(sync, one)["id"]),
        expected_state="rejected",
    )
