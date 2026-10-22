"""Unresolved explicit people do not borrow another authorized owner's animal."""

import pytest
from test_automatic_facts import add
from test_group_context import cat, named_cats, owner, query, recalled
from test_group_context import room as room
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples

from tianshu_memory.recall_language import explicit_named_reference, named_reference


@pytest.mark.parametrize(
    "text",
    [
        "阿宁家那只猫叫什么来着？",
        "阿宁家的猫叫什么？",
        "群里阿宁那只猫叫什么？",
        "阿这只猫叫什么？",
        "Ann Marie's cat叫什么？",
        "Ann Marie上次提过的猫叫什么？",
        "阿宁上次提过的猫叫什么？",
        "阿宁之前提到过的猫叫什么？",
        "阿宁并没有提过的猫叫什么？",
        "如果阿宁提过的猫叫什么？",
        "阿宁和小雨提过的猫叫什么？",
        "阿宁和小雨家的猫叫什么？",
        "小猫家那只猫叫什么？",
        "小猫提过的猫叫什么？",
        "咖啡家的猫叫什么？",
        "咖啡提过的猫叫什么？",
        "说说家那只猫叫什么？",
        "说说提过的猫叫什么？",
    ],
)
def test_explicit_person_wording_does_not_require_a_private_name_lookup(text):
    assert explicit_named_reference(text)


@pytest.mark.parametrize(
    "text",
    [
        "群里那只猫叫什么？",
        "我家的猫叫什么？",
        "咱家那只猫叫什么？",
        "这家猫叫什么？",
        "那家猫叫什么？",
        "大家的猫叫什么？",
        "厂家的猫叫什么？",
        "作家的猫叫什么？",
        "这家提过的猫叫什么？",
        "桌上那只猫叫什么？",
        "红杯子那只猫叫什么？",
        "红色的杯子叫什么？",
        "桌上的猫叫什么？",
        "我妹妹那只猫叫什么？",
        "那只家猫叫什么？",
        "普通家猫叫什么？",
        "《阿宁家的猫》叫什么？",
        "‘阿宁提过的猫’叫什么？",
        "有人说阿宁提过的猫叫什么？",
    ],
)
def test_object_location_deictic_and_quoted_text_are_not_invented_names(text):
    assert not explicit_named_reference(text)


def test_unknown_name_and_private_or_other_room_name_have_the_same_empty_result(room):
    h = room
    public, private, elsewhere = (
        owner(h),
        owner(h, audience="self_private"),
        owner(h, conversation="other-room"),
    )
    add(h, "我叫团子", "identity.name", "团子", actor=public, second=1)
    public_cat, _ = cat(h, public, "星渡", second=2)
    for actor, name, pet, second in (
        (private, "阿宁", "苔灯", 3),
        (elsewhere, "小雨", "砂岸", 5),
    ):
        add(h, "我叫" + name, "identity.name", name, actor=actor, second=second)
        cat(h, actor, pet, second=second + 1)
    for text in (
        "阿宁家那只猫叫什么来着？",
        "小雨家那只猫叫什么？",
        "未知那只猫叫什么？",
        "阿宁上次提过的猫叫什么？",
        "小雨之前提到过的猫叫什么？",
        "未知提过的猫叫什么？",
        "团子和阿宁家的猫叫什么？",
    ):
        result = recalled(h, query(h, text))
        assert result["selected_units"] == [], text
        assert result["dependency_groups"] == [], text
        assert len(result["scope_checks"]) == 1, text
        assert "no_match" in result["omissions"], text
    generic = recalled(h, query(h, "群里那只猫叫什么？"))
    assert {unit["record_id"] for unit in generic["selected_units"]} == set(
        public_cat["record_ids"]
    )


def test_current_exact_single_character_and_spaced_names_keep_their_own_witness(room):
    h = room
    a, b, c = owner(h), owner(h), owner(h)
    records = {}
    for actor, name, pet, second in (
        (a, "阿", "栗团", 1),
        (b, "Ann Marie", "栗糖", 3),
        (c, "Anna Marie", "栗饼", 5),
    ):
        witness, _ = add(h, "我叫" + name, "identity.name", name, actor=actor, second=second)
        target, _ = cat(h, actor, pet, second=second + 1)
        records[actor] = set(witness["record_ids"] + target["record_ids"])
    for actor, text in (
        (a, "阿家那只猫叫什么？"),
        (a, "阿上次提过的猫叫什么？"),
        (b, "Ann Marie's cat叫什么？"),
        (b, "Ann Marie上次提过的猫叫什么？"),
        (c, "Anna Marie的猫叫什么？"),
    ):
        selected = recalled(h, query(h, text))
        assert {unit["record_id"] for unit in selected["selected_units"]} == records[actor]
        assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
            h.scope(actor)["person_id"]
        }
    for text in ("阿川那只猫叫什么？", "Ann的猫叫什么？", "Ann上次提过的猫叫什么？"):
        # Plain unknown X的 is intentionally ambiguous; the other two frames
        # are explicit. Exact known-name parsing must never shorten a name.
        assert named_reference(text, "阿") is None
        assert named_reference(text, "Ann Marie") is None
        if "的猫" not in text or "提过" in text:
            assert recalled(h, query(h, text))["selected_units"] == []


def test_unknown_source_reference_is_not_another_owner_or_a_quoted_title(room):
    h = room
    a, b = named_cats(h)
    for text in (
        "未知提过的小禾家的猫叫什么？",
        "未知提过的猫原名是什么？",
        "未知并没有提过的猫叫什么？",
        "如果未知提过的猫叫什么？",
        "未知和阿川提过的猫叫什么？",
        "未知提过的猫！阿川的呢？",
    ):
        assert recalled(h, query(h, text))["selected_units"] == [], text
    source = "我的蓝杯子叫《阿宁家的猫》"
    title, _ = add(
        h,
        source,
        "cup.name",
        "阿宁家的猫",
        entity=dict(id=None, kind="cup", mention="蓝杯子"),
        actor=a,
        second=5,
    )
    quoted = recalled(h, query(h, "《阿宁家的猫》叫什么？"))
    assert {unit["record_id"] for unit in quoted["selected_units"]} == set(title["record_ids"])
    assert a != b


def test_topic_bearing_nicknames_need_a_current_witness_in_strong_frames(room):
    h = room
    public = owner(h)
    star, _ = cat(h, public, "星渡", second=1)
    for index, name in enumerate(("小猫", "咖啡", "说说")):
        texts = (name + "家那只猫叫什么？", name + "提过的猫叫什么？")
        for text in texts:
            assert recalled(h, query(h, text))["selected_units"] == [], text
        actor = owner(h)
        witness, _ = add(h, "我叫" + name, "identity.name", name, actor=actor, second=2 + index * 2)
        target, _ = cat(h, actor, f"栗团{index}", second=3 + index * 2)
        for text in texts:
            selected = recalled(h, query(h, text))
            assert {unit["record_id"] for unit in selected["selected_units"]} == set(
                witness["record_ids"] + target["record_ids"]
            )
            assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
                h.scope(actor)["person_id"]
            }
    assert set(star["record_ids"]) <= {unit["record_id"] for unit in recalled(h)["selected_units"]}


def test_deictic_relative_keeps_the_existing_object_owner_constraint(room):
    h = room
    public = owner(h)
    cat(h, public, "星渡", second=1)
    relative, _ = add(h, "我妹妹的猫叫白团", "pet.cat.name", "白团", second=2)
    selected = recalled(h, query(h, "我妹妹那只猫叫什么？"))
    assert {unit["record_id"] for unit in selected["selected_units"]} == set(relative["record_ids"])


def test_zero_width_between_real_time_words_does_not_lose_the_complete_source_quote(room):
    h = room
    a = owner(h)
    source = "周日下午想去杉月公园，从南门进会方便些吧。"
    statement = "作者提出周日\u200b下午想去杉月公园，并认为从南门进入会更方便。"
    value = "周日\u200b下午去杉月公园，从南门进"
    memories = [
        dict(
            statement=statement,
            conditions=["周日\u200b下午"],
            negations=[],
            valid_time="周日\u200b下午",
            evidence=[dict(source_index=0, quote=source)],
            fact=dict(
                key="event.outing",
                value=value,
                state="current",
                valid_from=None,
                valid_to=None,
                keywords=["杉月公园", "南门"],
            ),
        )
    ]
    added, _ = add(h, source, "event.outing", actor=a, second=1, memories=memories)
    with h.store.transaction() as db:
        indexed = db.execute(
            "SELECT text FROM search_index WHERE record_id=?", (added["record_ids"][0],)
        ).fetchone()[0]
    assert "日下" in indexed.split()  # Only the complete quote spans the zero-width boundary.
    # Match the actual source through normal natural queries, not a field query
    # or a rewritten stored value. The proposal's formatting remains untouched.
    for text in ("周日下午去哪里，从哪进？", "杉月公园从哪个门进？"):
        selected = recalled(h, query(h, text))
        assert {unit["record_id"] for unit in selected["selected_units"]} == set(
            added["record_ids"]
        )
        assert selected["selected_units"][0]["statement"] == statement
        assert selected["selected_units"][0]["conditions"] == ["周日\u200b下午"]
        assert selected["selected_units"][0]["valid_time"] == "周日\u200b下午"
