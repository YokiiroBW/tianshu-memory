"""Complete source appositions keep object identity without promoting name fragments."""

import copy
import json

import pytest
from test_automatic_facts import add, recall
from test_candidate_extraction import PATH
from test_fact_entities import entity, saved_entity
from test_fact_labels import memory
from test_fact_names import draft
from test_group_context import query, recalled
from test_group_context import room as room
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples

from tianshu_memory.domain import Fault

SOURCE = "刚把拖鞋找回来，我家狗蛋挞又叼去阳台了。"
ACTUAL_QUOTES = ["我家狗蛋挞又叼去阳台了", SOURCE, "我家狗蛋挞又叼去阳台了"]
ACTUAL_SUMMARIES = ["作者家的狗叫蛋挞。", "作者养了一只名叫蛋挞的狗。", "作者养了一只叫蛋挞的狗。"]


def actual_proposal(index=0):
    proposal = memory(SOURCE, "蛋挞", entity("狗蛋挞", kind="dog"))
    proposal.update(
        statement=ACTUAL_SUMMARIES[index],
        evidence=[dict(source_index=0, quote=ACTUAL_QUOTES[index])],
    )
    proposal["fact"].update(key="pet.dog.name", keywords=["狗", "宠物", "名字", "蛋挞"])
    return proposal


@pytest.mark.parametrize("index", range(3))
def test_actual_q90_business_proposals_have_a_complete_source_apposition(index):
    proposal = actual_proposal(index)
    prepared = draft(
        SOURCE,
        "蛋挞",
        "pet.dog.name",
        quote=proposal["evidence"][0]["quote"],
        statement=proposal["statement"],
        fact=proposal["fact"],
    )
    assert prepared["fact"] == proposal["fact"]
    assert prepared["units"][0]["statement"] == proposal["statement"]
    assert "source_text" not in prepared["quotes"][0]


@pytest.mark.parametrize("mention", [None, "狗", "我家狗", "蛋挞", "狗蛋挞", "我家狗蛋挞"])
def test_original_head_whole_name_and_complete_composite_shapes_are_compatible(mention):
    target = entity(mention, kind="dog") if mention else None
    assert (
        draft(SOURCE, "蛋挞", "pet.dog.name", fact=dict(entity=target))["fact"]["entity"] == target
    )


@pytest.mark.parametrize(
    "source,value,mention,kind,key",
    [
        ("我家猫咪糖球又来扒门了", "糖球", "猫咪糖球", "cat", "pet.cat.name"),
        ("我家狗狗豆包又拖走垫子了", "豆包", "狗狗豆包", "dog", "pet.dog.name"),
        ("我家猫《我妹妹的狗》又来扒门了", "我妹妹的狗", "猫《我妹妹的狗》", "cat", "pet.cat.name"),
        (
            "我家猫《不叫奶糖（旧名：蓝豆）》又来扒门了",
            "不叫奶糖（旧名：蓝豆）",
            "猫《不叫奶糖（旧名：蓝豆）》",
            "cat",
            "pet.cat.name",
        ),
        (
            "我的笔记本《如果（旧名：昨日）》又被压在下面了",
            "如果（旧名：昨日）",
            "笔记本《如果（旧名：昨日）》",
            "notebook",
            "notebook.title",
        ),
    ],
)
def test_source_parsed_composite_keeps_complete_quoted_literals(source, value, mention, kind, key):
    target = entity(mention, kind=kind)
    prepared = draft(source, value, key, fact=dict(entity=target))
    assert prepared["fact"]["value"] == value
    assert prepared["fact"]["entity"] == target


@pytest.mark.parametrize(
    "source,value,mention,kind,quote",
    [
        (SOURCE, "蛋挞", "狗蛋", "dog", None),
        (SOURCE, "蛋挞", "蛋", "dog", None),
        (SOURCE, "蛋挞", "狗蛋挞又", "dog", None),
        (SOURCE, "蛋", "狗蛋挞", "dog", None),
        (SOURCE, "蛋挞又", "狗蛋挞", "dog", None),
        (SOURCE, "狗蛋挞", "狗蛋挞", "dog", None),
        (SOURCE, "蛋挞", "狗蛋挞", "cat", None),
        (SOURCE, "蛋挞", "猫栗团", "dog", None),
        (SOURCE, "蛋挞", "狗蛋挞", "dog", "蛋挞"),
        ("我家猫阿川又来扒门了", "阿川", "川", "cat", None),
        ("我家猫《我妹妹的狗》又来扒门了", "我妹妹的狗", "我妹妹", "cat", None),
        ("我家猫《狗阿川》又来扒门了", "阿川", "狗阿川", "dog", None),
        ("我家猫咪糖球又来扒门了", "咪糖球", "猫咪糖球", "cat", None),
        ("我家狗狗豆包又拖走垫子了", "狗豆包", "狗狗豆包", "dog", None),
        ("假设我家狗蛋挞又叼去阳台了", "蛋挞", "狗蛋挞", "dog", None),
        ("不是狗蛋挞又叼去阳台了", "蛋挞", "狗蛋挞", "dog", None),
        ("请不要把狗蛋挞叫晚星", "晚星", "狗蛋挞", "dog", None),
        ("我不再把狗蛋挞叫晚星", "晚星", "狗蛋挞", "dog", None),
        ("“我家狗蛋挞又叼去阳台了”是别人说的", "蛋挞", "狗蛋挞", "dog", None),
    ],
)
def test_complete_apposition_does_not_grant_literal_fragments_or_other_objects(
    source, value, mention, kind, quote
):
    with pytest.raises(Fault) as error:
        draft(
            source,
            value,
            f"pet.{kind}.name",
            quote=quote,
            fact=dict(entity=entity(mention, kind=kind)),
        )
    assert error.value.code == "invalid_input"


def test_apposition_atomic_retry_keeps_two_dogs_and_projects_only_the_real_cat_head(room):
    h = room
    good = actual_proposal()
    bad = copy.deepcopy(good)
    bad["fact"]["entity"]["mention"] = "狗蛋"
    rejected, job = add(h, SOURCE, "pet.dog.name", memories=[good, bad], expected_state="rejected")
    assert rejected == {"error_code": "invalid_input"}
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM write_ledger WHERE job_id=?", (job,)).fetchone()[0]
            == 0
        )
    response = h.post(PATH, dict(job_id=job, output=dict(memories=[good])))
    assert response.status_code == 200 and response.json()["state"] == "completed", response.text
    first = response.json()["result"]

    text = "我家第二只狗豆包又把垫子拖走了。"
    second = memory(text, "豆包", entity("第二只狗豆包", kind="dog"))
    second["fact"].update(key="pet.dog.name", keywords=["狗", "名字", "豆包"])
    second["statement"] = "豆包"
    other, _ = add(h, text, "pet.dog.name", second=2, memories=[second])
    assert saved_entity(h, first)["id"] != saved_entity(h, other)["id"]
    assert saved_entity(h, first)["mention"] == "狗蛋挞"
    assert saved_entity(h, other)["mention"] == "第二只狗豆包"
    assert set(first["record_ids"]).isdisjoint(other["record_ids"])

    value = "我妹妹的狗"
    text = f"我家猫《{value}》又把窗帘当秋千了。"
    cat_proposal = memory(text, value, entity(f"猫《{value}》", kind="cat"))
    cat_proposal["fact"].update(key="pet.cat.name", keywords=["猫", "名字", value])
    cat_proposal["statement"] = value
    cat, _ = add(h, text, "pet.cat.name", second=3, memories=[cat_proposal])
    expected_dogs = set(first["record_ids"] + other["record_ids"])
    for text, expected in (
        ("狗叫什么", expected_dogs),
        ("第二只狗叫什么", set(other["record_ids"])),
        ("猫叫什么", set(cat["record_ids"])),
        ("我妹妹的狗叫什么", set()),
    ):
        assert {unit["record_id"] for unit in recall(h, text)} == expected
        assert {
            unit["record_id"] for unit in recalled(h, query(h, text))["selected_units"]
        } == expected
    with h.store.transaction() as db:
        facts = [json.loads(row["payload"]) for row in db.execute("SELECT * FROM automatic_facts")]
        assert {fact["value"] for fact in facts} == {"蛋挞", "豆包", value}
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automatic_facts WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
