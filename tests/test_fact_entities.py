"""Object discrimination through native sourced candidate commits and retrieval."""

import copy
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


def _cup_memory(text, value, mention, identity, *, kind="cup", quote=None):
    return dict(
        statement=text,
        conditions=[],
        negations=[],
        valid_time="未说明有效时间",
        evidence=[dict(source_index=0, quote=text if quote is None else quote)],
        fact=dict(
            key="object.cup.name",
            value=value,
            state="current",
            valid_from=None,
            valid_to=None,
            keywords=["水杯", value],
            entity=entity(mention, identity, kind),
        ),
    )


def _cup_pair(h, red_mention="红色水杯"):
    blue, _ = add(
        h, "我的蓝色水杯叫雨点", "object.cup.name", "雨点", entity=entity("蓝色水杯", kind="cup")
    )
    red, _ = add(
        h,
        "我的" + red_mention + "叫晚霞",
        "object.cup.name",
        "晚霞",
        second=2,
        entity=entity(red_mention, kind="cup"),
    )
    return blue, red


@pytest.mark.parametrize("null_id", [False, True])
def test_grounded_color_ellipsis_commits_correction_and_reaffirmation_atomically(sync, null_id):
    initialize(sync)
    blue, red = _cup_pair(sync)
    blue_id, red_id = saved_entity(sync, blue)["id"], saved_entity(sync, red)["id"]
    text = "蓝杯子现在改叫细雨，红色的还是晚霞。"
    memories = [
        _cup_memory(text, "细雨", "蓝杯子", blue_id),
        _cup_memory(text, "晚霞", "红色的", None if null_id else red_id),
    ]
    memories[0].update(statement="作者的蓝色水杯现在改叫细雨。")
    memories[1].update(statement="作者的红色水杯仍然叫晚霞。")
    memories[0]["fact"]["keywords"] = ["水杯", "细雨", "蓝色", "名字", "改名"]
    memories[1]["fact"]["keywords"] = ["水杯", "晚霞", "红色", "名字"]
    original = copy.deepcopy(memories)
    result, job = add(sync, text, "object.cup.name", second=3, memories=memories)
    assert result["state"] == "committed"
    assert memories == original
    with sync.store.transaction() as db:
        rows = db.execute("SELECT group_id,payload FROM automatic_facts").fetchall()
        renamed = next(row for row in rows if json.loads(row["payload"])["value"] == "细雨")
        assert json.loads(renamed["payload"])["entity"]["id"] == blue_id
        assert set(result["group_ids"]) == {renamed["group_id"], red["group_ids"][0]}
        red_fact = db.execute(
            "SELECT payload,observed_at FROM automatic_facts WHERE group_id=?",
            (red["group_ids"][0],),
        ).fetchone()
        assert json.loads(red_fact["payload"])["entity"] == entity("红色水杯", red_id, "cup")
        assert red_fact["observed_at"] == "2026-10-11T00:00:03Z"
        for table in ("lineage", "automatic_evidence"):
            assert (
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id=?", (red["group_ids"][0],)
                ).fetchone()[0]
                == 2
            )
        assert db.execute(
            "SELECT 1 FROM automatic_evidence WHERE group_id=? AND quote=?",
            (red["group_ids"][0], text),
        ).fetchone()
        renamed_ids = {
            row[0]
            for row in db.execute("SELECT id FROM records WHERE group_id=?", (renamed["group_id"],))
        }
        assert (
            db.execute("SELECT COUNT(*) FROM write_ledger WHERE job_id=?", (job,)).fetchone()[0]
            == 1
        )
    assert {unit["record_id"] for unit in recall(sync, "蓝杯子现在叫什么")} == renamed_ids
    assert {unit["record_id"] for unit in recall(sync, "红色杯子现在叫什么")} == set(
        red["record_ids"]
    )
    assert set(blue["record_ids"]) <= {
        unit["record_id"] for unit in recall(sync, "蓝杯子以前叫什么")
    }
    replay = sync.post("memory/candidates/process", dict(job_id=job, output=None)).json()
    assert replay["state"] == "completed" and replay["result"] == result
    assert (
        sync.post(
            "memory/candidates/process", dict(job_id=job, output=dict(memories=original))
        ).json()
        == replay
    )


@pytest.mark.parametrize(
    "case",
    [
        "short_quote",
        "split_quotes",
        "separate_sentence",
        "unrelated_cup",
        "wrong_kind",
        "book_context",
        "third_party",
        "third_party_owner",
        "question",
        "hypothetical",
        "wrong_id",
        "forged_mention",
    ],
)
def test_unproven_color_ellipse_rejects_the_whole_proposal(sync, case):
    initialize(sync)
    blue, red = _cup_pair(sync)
    text = {
        "separate_sentence": "蓝杯子现在改叫细雨。红色的还是晚霞。",
        "unrelated_cup": "我今天洗了蓝杯子，红色的还是晚霞。",
        "book_context": "蓝书现在改叫细雨，红色的还是晚霞。",
        "third_party": "朋友说：蓝杯子现在改叫细雨，红色的还是晚霞。",
        "third_party_owner": "姐姐的蓝杯子现在改叫细雨，红色的还是晚霞。",
        "question": "蓝杯子现在改叫细雨，红色的还是晚霞吗？",
        "hypothetical": "如果蓝杯子现在改叫细雨，红色的还是晚霞。",
    }.get(case, "蓝杯子现在改叫细雨，红色的还是晚霞。")
    blue_mention = "蓝书" if case == "book_context" else "蓝杯子"
    memories = [
        _cup_memory(text, "细雨", blue_mention, saved_entity(sync, blue)["id"]),
        _cup_memory(text, "晚霞", "红色的", saved_entity(sync, red)["id"]),
    ]
    red_proposal = memories[1]
    if case == "book_context":
        memories = [red_proposal]
    if case == "short_quote":
        red_proposal["evidence"][0]["quote"] = "红色的还是晚霞"
    elif case == "split_quotes":
        red_proposal["evidence"] = [
            dict(source_index=0, quote=part) for part in text.rstrip("。").split("，")
        ]
    elif case == "wrong_kind":
        red_proposal["fact"]["entity"].update(kind="book", id=None)
    elif case == "wrong_id":
        red_proposal["fact"]["entity"]["id"] = saved_entity(sync, blue)["id"]
    elif case == "forged_mention":
        red_proposal["fact"]["entity"]["mention"] = "红色水杯"
    _, job = add(
        sync, text, "object.cup.name", second=3, memories=memories, expected_state="rejected"
    )
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 2
        assert (
            db.execute("SELECT COUNT(*) FROM write_ledger WHERE job_id=?", (job,)).fetchone()[0]
            == 0
        )
    assert {unit["record_id"] for unit in recall(sync, "蓝色杯子现在叫什么")} == set(
        blue["record_ids"]
    )


@pytest.mark.parametrize(
    "mentions",
    [
        ("红色玻璃水杯", "红色陶瓷水杯"),
        ("第一只红色玻璃水杯", "第二只红色玻璃水杯"),
        ("红色玻璃水杯", "红色磨砂水杯"),
        ("红玻璃水杯", "红磨砂水杯"),
        ("第一只红玻璃水杯", "第二只红磨砂水杯"),
        ("红色水杯", "红茶杯子"),
    ],
)
@pytest.mark.parametrize("null_id", [False, True])
def test_color_ellipse_does_not_choose_a_material_or_ordinal(sync, mentions, null_id):
    initialize(sync)
    blue, red = _cup_pair(sync, mentions[0])
    add(
        sync,
        mentions[1] + "叫朝霞",
        "object.cup.name",
        "朝霞",
        second=3,
        entity=entity(mentions[1], kind="cup"),
    )
    text = "蓝杯子现在改叫细雨，红色的还是晚霞。"
    memories = [
        _cup_memory(text, "细雨", "蓝杯子", saved_entity(sync, blue)["id"]),
        _cup_memory(text, "晚霞", "红色的", None if null_id else saved_entity(sync, red)["id"]),
    ]
    add(sync, text, "object.cup.name", second=4, memories=memories, expected_state="rejected")
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 3


def test_color_ellipse_cannot_create_a_new_nounless_object(sync):
    initialize(sync)
    add(
        sync, "我的蓝色水杯叫雨点", "object.cup.name", "雨点", entity=entity("蓝色水杯", kind="cup")
    )
    text = "蓝杯子现在改叫细雨，红色的还是晚霞。"
    add(
        sync,
        text,
        "object.cup.name",
        "晚霞",
        second=2,
        entity=entity("红色的", kind="cup"),
        expected_state="rejected",
    )


def test_color_ellipse_does_not_revive_a_forgotten_target(sync):
    initialize(sync)
    blue, red = _cup_pair(sync)
    red_id = saved_entity(sync, red)["id"]
    assert sync.post("memory/revise", sync.revision(red["record_ids"][0])).status_code == 200
    text = "蓝杯子现在改叫细雨，红色的还是晚霞。"
    memories = [
        _cup_memory(text, "细雨", "蓝杯子", saved_entity(sync, blue)["id"]),
        _cup_memory(text, "晚霞", "红色的", red_id),
    ]
    add(sync, text, "object.cup.name", second=3, memories=memories, expected_state="rejected")
    assert recall(sync, "红色杯子叫什么") == []


@pytest.mark.parametrize("null_id", [False, True])
def test_color_ellipse_cannot_bind_an_existing_cup_in_another_scope(sync, null_id):
    initialize(sync)
    add(
        sync, "我的蓝色水杯叫雨点", "object.cup.name", "雨点", entity=entity("蓝色水杯", kind="cup")
    )
    foreign, _ = add(
        sync,
        "我的红色水杯叫晚霞",
        "object.cup.name",
        "晚霞",
        second=2,
        actor=1,
        entity=entity("红色水杯", kind="cup"),
    )
    text = "蓝杯子现在改叫细雨，红色的还是晚霞。"
    identity = None if null_id else saved_entity(sync, foreign)["id"]
    add(
        sync,
        text,
        "object.cup.name",
        "晚霞",
        second=3,
        entity=entity("红色的", identity, "cup"),
        expected_state="rejected",
    )


@pytest.mark.parametrize("mention", ["红色水杯", "第一只红色玻璃水杯", "红色磨砂水杯"])
def test_unique_qualified_cup_with_multiple_attributes_is_one_color_target(sync, mention):
    initialize(sync)
    _, red = _cup_pair(sync, mention)
    red_id = saved_entity(sync, red)["id"]
    add(
        sync,
        mention + "放在抽屉",
        "object.cup.location",
        "抽屉",
        second=3,
        entity=entity(mention, red_id, "cup"),
    )
    text = "蓝杯子还是雨点，红色的还是晚霞。"
    repeated, _ = add(
        sync, text, "object.cup.name", "晚霞", second=4, entity=entity("红色的", red_id, "cup")
    )
    assert repeated["record_ids"] == red["record_ids"]
    assert saved_entity(sync, repeated) == entity(mention, red_id, "cup")


@pytest.mark.parametrize("mention", ["红茶杯子", "红磨砂水杯"])
@pytest.mark.parametrize("null_id", [False, True])
def test_unknown_bare_cup_descriptor_cannot_ground_color_by_itself(sync, mention, null_id):
    initialize(sync)
    _, red = _cup_pair(sync, mention)
    identity = None if null_id else saved_entity(sync, red)["id"]
    text = "蓝杯子还是雨点，红色的还是晚霞。"
    add(
        sync,
        text,
        "object.cup.name",
        "晚霞",
        second=3,
        entity=entity("红色的", identity, "cup"),
        expected_state="rejected",
    )
