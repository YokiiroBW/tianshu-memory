"""Literal-label admission and real sourced commit/merge on both existing backends."""

import copy
import json

import pytest
from test_automatic_facts import add, recall
from test_candidate_extraction import PATH
from test_fact_entities import entity, saved_entity
from test_fact_names import draft
from test_group_context import owner, query, recalled
from test_group_context import room as room
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples

from tianshu_memory.domain import Fault
from tianshu_memory.fact_updates import may_replace

RED_SOURCE = "我的红皮本子换了张封面，以后就叫晚晴，旧名字是红叶。"


@pytest.mark.parametrize(
    "text,value,key,mention,kind,state",
    [
        (RED_SOURCE, "晚晴", "notebook.name", "红皮本子", "notebook", "current"),
        (RED_SOURCE, "红叶", "notebook.name", "红皮本子", "notebook", "historical"),
        ("蓝封面的叫暮色。", "暮色", "notebook.name", "蓝封面的", "notebook", "current"),
        ("养的猫咪名字是糖球", "糖球", "pet.cat.name", None, None, "current"),
        ("我家狗豆包又叼去阳台了。", "豆包", "pet.dog.name", None, None, "current"),
        ("我家狗豆包又叼去阳台了。", "豆包", "pet.dog.name", "豆包", "dog", "current"),
        ("昨天傍晚狗打翻了花盆，它叫栗团。", "栗团", "pet.dog.name", "狗", "dog", "current"),
        ("昨天傍晚狗打翻了花盆，它叫栗团。", "栗团", "pet.dog.name", "栗团", "dog", "current"),
        ("我这个杯子后来改叫轻舟。", "轻舟", "cup.name", "我这个杯子", "cup", "current"),
        ("第一本还是《晨光》", "晨光", "notebook.title", "第一本", "notebook", "current"),
        ("第一本改叫《不改变》", "不改变", "notebook.title", "第一本", "notebook", "current"),
        (
            "这本书改名为《不，别叫我（修订版）》",
            "不，别叫我（修订版）",
            "book.title",
            "这本书",
            "book",
            "current",
        ),
        (
            "这本书叫《晚晴（旧名：红叶）》",
            "晚晴（旧名：红叶）",
            "book.title",
            "这本书",
            "book",
            "current",
        ),
        ("这本书叫晚晴（秋）", "晚晴（秋）", "book.title", "这本书", "book", "current"),
        ("猫叫“不叫奶糖”。", "不叫奶糖", "pet.cat.name", "猫", "cat", "current"),
        ("猫不再叫米糕，现在叫奶糖。", "奶糖", "pet.cat.name", "猫", "cat", "current"),
        ("红杯子叫《如果》，蓝杯子叫晚星。", "晚星", "cup.name", "蓝杯子", "cup", "current"),
        (
            "蓝杯子叫《别把蓝杯子叫晚星》",
            "别把蓝杯子叫晚星",
            "cup.name",
            "蓝杯子",
            "cup",
            "current",
        ),
        (
            "蓝本写了《旧名是黄昏》，后来叫暮色。",
            "暮色",
            "notebook.name",
            "蓝本",
            "notebook",
            "current",
        ),
    ],
)
def test_source_label_is_a_complete_literal_for_its_object_and_state(
    text, value, key, mention, kind, state
):
    changes = dict(state=state)
    if mention:
        changes["entity"] = entity(mention, kind=kind)
    prepared = draft(text, value, key, fact=changes)
    assert prepared["fact"]["value"] == value
    assert prepared["fact"]["state"] == state
    assert prepared["quotes"][0]["quote"] == text
    assert "source_text" not in prepared["quotes"][0]


@pytest.mark.parametrize(
    "text,value,mention,state,quote",
    [
        (RED_SOURCE, "晚晴（旧名：红叶）", "红皮本子", "current", None),
        (RED_SOURCE, "红叶（原名）", "红皮本子", "historical", None),
        (RED_SOURCE, "红叶", "红皮本子", "current", None),
        (RED_SOURCE, "晚晴", "红皮本子", "historical", None),
        (RED_SOURCE, "晚晴", "红皮本子", "current", "晚晴"),
        ("红本叫晚星，蓝本不改。", "晚星", "蓝本", "current", None),
        ("红本叫晚星，蓝本不改。", "暮色", "蓝本", "current", None),
        ("红本叫晚星，蓝本不改。", "暮色（不改名，不叫晚星）", "蓝本", "current", None),
        ("蓝本不叫晚星", "晚星", "蓝本", "current", "叫晚星"),
        ("蓝本不叫晚星", "不叫晚星", "蓝本", "current", None),
        ("如果蓝本叫晚星就好了", "晚星", "蓝本", "current", "蓝本叫晚星"),
        ("假设蓝本叫晚星", "晚星", "蓝本", "current", None),
        ("“蓝本叫晚星”是别人说的。", "晚星", "蓝本", "current", "蓝本叫晚星"),
        ("蓝本叫晚星吗？", "晚星", "蓝本", "current", "蓝本叫晚星"),
        ("蓝本叫晚星雨", "晚星", "蓝本", "current", None),
    ],
)
def test_explanations_known_hints_and_cropped_claims_cannot_become_labels(
    text, value, mention, state, quote
):
    with pytest.raises(Fault) as error:
        draft(
            text,
            value,
            "notebook.name",
            fact=dict(entity=entity(mention, kind="notebook"), state=state),
            quote=quote,
        )
    assert error.value.code == "invalid_input"


def test_object_label_rule_does_not_become_an_author_or_favorite_name_rule():
    assert draft("我喜欢阿川这个名字", "阿川", "favorite.name")["fact"]["key"] == "favorite.name"
    assert draft("我叫阿川", "阿川", "author.name")["fact"]["key"] == "identity.name"


@pytest.mark.parametrize(
    "text,value,mention,kind,key",
    [
        ("我家狗豆包又把垫子拖走了", "豆包又", "狗", "dog", "pet.dog.name"),
        ("猫叫团子", "叫团子", "猫", "cat", "pet.cat.name"),
        ("猫名字是糖球", "名字是糖球", "猫", "cat", "pet.cat.name"),
        ("猫不叫晚星", "不叫晚星", "猫", "cat", "pet.cat.name"),
        ("不是狗豆包又把垫子拖走了", "豆包", "狗", "dog", "pet.dog.name"),
        ("我家猫叫《我妹妹的狗》", "我妹妹的狗", "我妹妹", "cat", "pet.cat.name"),
        ("我家猫阿川又把垫子拖走了", "阿川", "川", "cat", "pet.cat.name"),
        ("别把蓝杯子叫晚星", "晚星", "蓝杯子", "cup", "cup.name"),
        ("不要把猫叫团子", "团子", "猫", "cat", "pet.cat.name"),
        ("贴着“红杯子”标签的蓝杯子叫晚星", "晚星", "红杯子", "cup", "cup.name"),
        ("红杯子不像蓝杯子，叫晚星", "晚星", "蓝杯子", "cup", "cup.name"),
        ("红杯子旁边的蓝杯子叫晚星", "晚星", "红杯子", "cup", "cup.name"),
    ],
)
def test_source_subject_and_appositive_boundaries_do_not_come_from_the_model(
    text, value, mention, kind, key
):
    with pytest.raises(Fault) as error:
        draft(text, value, key, fact=dict(entity=entity(mention, kind=kind)))
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize(
    "value,statement,conditions,expected",
    [
        ("暮色", "蓝本还是暮色，不改名，不叫晚星。", [], False),
        ("晚星", "蓝本不叫晚星", [], False),
        ("晚星", "别把蓝本叫晚星", [], False),
        ("不叫晚星", "蓝本叫《不叫晚星》", [], True),
        ("不，别叫我（修订版）", "蓝本改叫《不，别叫我（修订版）》", [], True),
        ("晚星", "假设换封面，蓝本改叫晚星", ["假设换封面"], False),
        ("晚星", "蓝本改叫晚星", [], True),
    ],
)
def test_label_replacement_is_not_an_unconditional_name_shortcut(
    value, statement, conditions, expected
):
    prior = dict(key="notebook.name", value="暮色")
    old_unit = dict(statement="蓝本叫暮色", conditions=[], negations=[])
    current = dict(prior, value=value, state="current")
    current_unit = dict(statement=statement, conditions=conditions, negations=["不叫晚星"])
    assert (
        may_replace(prior, old_unit, current, current_unit, current_quotes=[statement]) is expected
    )
    assert may_replace(prior, old_unit, current, current_unit) is False


def memory(text, value, target, *, state="current", negations=()):
    return dict(
        statement=text,
        conditions=[],
        negations=list(negations),
        valid_time="未说明有效时间",
        evidence=[dict(source_index=0, quote=text)],
        fact=dict(
            key="notebook.name",
            value=value,
            state=state,
            valid_from=None,
            valid_to=None,
            keywords=["笔记本", value],
            entity=target,
        ),
    )


def test_current_and_historical_literals_keep_distinct_values_and_groups(room):
    h = room
    old, _ = add(
        h, "红皮本子叫红叶", "notebook.name", "红叶", entity=entity("红皮本子", kind="notebook")
    )
    target = saved_entity(h, old)
    memories = [
        memory(RED_SOURCE, "晚晴", target),
        memory(RED_SOURCE, "红叶", target, state="historical"),
    ]
    memories[0]["conditions"] = ["以后"]
    result, job = add(h, RED_SOURCE, "notebook.name", second=2, memories=memories)
    assert result["state"] == "committed"
    with h.store.transaction() as db:
        facts = [
            (row, json.loads(row["payload"])) for row in db.execute("SELECT * FROM automatic_facts")
        ]
        current = [
            (row, fact)
            for row, fact in facts
            if row["superseded_by"] is None and fact["state"] == "current"
        ]
        assert [fact["value"] for _, fact in current] == ["晚晴"]
        assert (
            next(row for row, fact in facts if row["group_id"] == old["group_ids"][0])[
                "superseded_by"
            ]
            == current[0][0]["group_id"]
        )
        assert any(fact["value"] == "红叶" and fact["state"] == "historical" for _, fact in facts)
        current_ids = {
            row[0]
            for row in db.execute(
                "SELECT id FROM records WHERE group_id=?", (current[0][0]["group_id"],)
            )
        }
    assert {u["record_id"] for u in recall(h, "红皮本子现在叫什么")} == current_ids
    assert [u["conditions"] for u in recall(h, "红皮本子现在叫什么")] == [["以后"]]
    assert h.post(PATH, dict(job_id=job, output=None)).json()["result"] == result


def test_known_or_polluted_name_rejects_atomically_and_same_job_can_retry(room):
    h = room
    red, _ = add(
        h,
        "红封面笔记本叫红叶",
        "notebook.name",
        "红叶",
        entity=entity("红封面笔记本", kind="notebook"),
    )
    blue, _ = add(
        h,
        "蓝封面的叫暮色",
        "notebook.name",
        "暮色",
        second=2,
        entity=entity("蓝封面的", kind="notebook"),
    )
    source = "我这本红封面笔记本改叫晚星，蓝封面的不改。"
    valid = memory(source, "晚星", saved_entity(h, red))
    valid["statement"] = "晚星"  # The source quote, not this model summary, proves the rename.
    for second, value in enumerate(("暮色（不改名，不叫晚星）", "暮色", "晚星"), 3):
        before = copy.deepcopy(valid)
        invalid = memory(source, value, saved_entity(h, blue))
        rejected, job = add(
            h,
            source,
            "notebook.name",
            second=second,
            memories=[valid, invalid],
            expected_state="rejected",
        )
        assert rejected == {"error_code": "invalid_input"} and valid == before
        with h.store.transaction() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM write_ledger WHERE job_id=?", (job,)).fetchone()[0]
                == 0
            )
            assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == (
                2 if second == 3 else 3
            )
            assert (
                db.execute(
                    "SELECT superseded_by FROM automatic_facts WHERE group_id=?",
                    (blue["group_ids"][0],),
                ).fetchone()[0]
                is None
            )
        retry = h.post(PATH, dict(job_id=job, output=dict(memories=[valid])))
        assert retry.status_code == 200 and retry.json()["state"] == "completed", retry.text
        assert h.post(PATH, dict(job_id=job, output=None)).json() == retry.json()
    assert [u["statement"] for u in recall(h, "蓝封面的现在叫什么")] == ["蓝封面的叫暮色"]


def test_same_literal_reaffirmation_keeps_interval_lineage_and_new_denial_evidence(room):
    h = room
    old, _ = add(
        h,
        "蓝封面笔记本在2026-10-10至2027-10-10期间叫暮色",
        "notebook.name",
        "暮色",
        valid_from="2026-10-10T00:00:00Z",
        valid_to="2027-10-10T00:00:00Z",
        entity=entity("蓝封面笔记本", kind="notebook"),
    )
    target = saved_entity(h, old)
    repeated, _ = add(h, "蓝封面笔记本还是暮色", "notebook.name", "暮色", second=2, entity=target)
    assert repeated["group_ids"] == old["group_ids"]
    text = "蓝封面笔记本还是暮色，不改名，不叫晚星。"
    extra, _ = add(
        h,
        text,
        "notebook.name",
        "暮色",
        second=3,
        memories=[memory(text, "暮色", target, negations=["不叫晚星"])],
    )
    assert extra["group_ids"] != old["group_ids"]
    with h.store.transaction() as db:
        row = db.execute(
            "SELECT * FROM automatic_facts WHERE group_id=?", (old["group_ids"][0],)
        ).fetchone()
        fact = json.loads(row["payload"])
        assert row["superseded_by"] is None
        assert (fact["valid_from"], fact["valid_to"]) == (
            "2026-10-10T00:00:00Z",
            "2027-10-10T00:00:00Z",
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM lineage WHERE group_id=?", (old["group_ids"][0],)
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automatic_evidence WHERE group_id=?", (extra["group_ids"][0],)
            ).fetchone()[0]
            == 1
        )
    denial = "蓝封面笔记本不叫晚星。"
    denied_memory = memory(denial, "晚星", target, negations=["不叫晚星"])
    rejected, job = add(
        h, denial, "notebook.name", second=4, memories=[denied_memory], expected_state="rejected"
    )
    assert rejected == {"error_code": "invalid_input"}
    denied_memory.pop("fact")
    retried = h.post(PATH, dict(job_id=job, output=dict(memories=[denied_memory])))
    assert retried.status_code == 200 and retried.json()["state"] == "completed", retried.text
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 2


@pytest.mark.parametrize("order", [(1, 2, 3), (3, 1, 2)])
def test_literal_rename_commit_order_uses_each_current_groups_source_quote(room, order):
    h = room
    base, _ = add(
        h,
        "蓝封面笔记本叫序章",
        "notebook.name",
        "序章",
        second=0,
        entity=entity("蓝封面笔记本", kind="notebook"),
    )
    target = saved_entity(h, base)
    values = {1: "暮色", 2: "晨光", 3: "不叫晚星"}
    receipts = {}
    for second in order:
        value = values[second]
        source = "蓝封面笔记本改叫《" + value + "》"
        proposal = memory(source, value, target)
        proposal["statement"] = value
        receipts[second], _ = add(h, source, "notebook.name", second=second, memories=[proposal])
    with h.store.transaction() as db:
        current = [
            (row, json.loads(row["payload"]))
            for row in db.execute("SELECT * FROM automatic_facts WHERE superseded_by IS NULL")
        ]
        assert [(fact["value"], fact["state"]) for _, fact in current] == [("不叫晚星", "current")]
        assert current[0][0]["group_id"] == receipts[3]["group_ids"][0]
    assert {unit["record_id"] for unit in recall(h, "蓝封面笔记本现在叫什么")} == set(
        receipts[3]["record_ids"]
    )


def test_literal_summary_uses_its_bound_object_not_the_label_or_other_source_object(room):
    h = room
    red = entity("红封面笔记本", kind="notebook")
    blue = entity("蓝封面笔记本", kind="notebook")
    value = "我妹妹的狗不叫红封面"
    source = f"红封面笔记本叫《如果》，蓝封面笔记本叫《{value}》。"
    proposals = [memory(source, "如果", red), memory(source, value, blue)]
    for proposal in proposals:
        proposal["statement"] = proposal["fact"]["value"]
    receipt, _ = add(h, source, "notebook.name", memories=proposals)
    with h.store.transaction() as db:
        records = {
            json.loads(row["payload"])["statement"]: row["id"]
            for row in db.execute(
                "SELECT * FROM records WHERE group_id IN (?,?)", receipt["group_ids"]
            )
        }
    for text, expected in (("蓝封面笔记本叫什么", value), ("红封面笔记本叫什么", "如果")):
        assert [unit["record_id"] for unit in recall(h, text)] == [records[expected]]
        assert [unit["statement"] for unit in recalled(h, query(h, text))["selected_units"]] == [
            expected
        ]
    for text in ("狗叫什么", "我妹妹的蓝封面笔记本叫什么"):
        assert recalled(h, query(h, text))["selected_units"] == []

    # A pet entity may itself be labeled with its name. Its type comes from
    # the bound kind; the name's fictional dog/relative words supply no owner.
    cat_name = "我妹妹的狗"
    source = f"我家猫叫《{cat_name}》。"
    proposal = memory(source, cat_name, entity(cat_name, kind="cat"))
    proposal["fact"]["key"] = "pet.cat.name"
    proposal["statement"] = cat_name
    cat, _ = add(h, source, "pet.cat.name", second=2, memories=[proposal])
    assert {unit["record_id"] for unit in recall(h, "猫叫什么")} == set(cat["record_ids"])
    assert recalled(h, query(h, "狗叫什么"))["selected_units"] == []

    other = owner(h, audience="self_private")
    hidden = "蓝封面笔记本叫私下的蓝页"
    proposal = memory(hidden, "私下的蓝页", blue)
    proposal["statement"] = "私下的蓝页"
    private, _ = add(h, hidden, "notebook.name", second=3, actor=other, memories=[proposal])
    result = recalled(h, query(h, "蓝封面笔记本叫什么"))
    assert [unit["record_id"] for unit in result["selected_units"]] == [records[value]]
    assert not set(private["record_ids"]).intersection(
        unit["record_id"] for unit in result["selected_units"]
    )
