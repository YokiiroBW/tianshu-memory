"""Regression traces from native candidate proposals; no model or seeded memory."""

import json

import pytest
from test_automatic_facts import add, initialize, recall
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


@pytest.mark.parametrize("species,name", [("cat", "米糕"), ("dog", "核桃")])
def test_source_observation_timestamp_does_not_split_unchanged_fact(sync, species, name):
    initialize(sync)
    animal = "猫" if species == "cat" else "狗"
    key = "pet." + species + ".name"
    first, _ = add(sync, f"我养的{animal}叫{name}", key, name, valid_from="2026-10-11T00:00:01Z")
    repeat, _ = add(
        sync,
        f"我养的{animal}还是{name}，这一点没变",
        key,
        name,
        second=2,
        valid_from="2026-10-11T00:00:02Z",
    )
    assert first["record_ids"] == repeat["record_ids"]
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM automatic_evidence").fetchone()[0] == 2
        row = db.execute("SELECT * FROM automatic_facts").fetchone()
        assert row["superseded_by"] is None
        assert row["observed_at"] == "2026-10-11T00:00:02Z"
        assert json.loads(row["payload"])["valid_from"] is None
        assert db.execute("SELECT COUNT(*) FROM lineage").fetchone()[0] == 2
    assert len(recall(sync, "我的宠物名字是什么")) == 1


def test_explicit_dates_are_not_discarded_or_merged(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "2026年9月1日起我住在春城，9月30日搬离",
        "residence",
        "春城",
        state="historical",
        valid_from="2026-09-01T00:00:00Z",
        valid_to="2026-09-30T00:00:00Z",
    )
    second, _ = add(
        sync,
        "2026年10月20日起我又将住在春城",
        "residence",
        "春城",
        second=2,
        valid_from="2026-10-20T00:00:00Z",
    )
    assert first["record_ids"] != second["record_ids"]
    with sync.store.transaction() as db:
        values = [json.loads(r[0]) for r in db.execute("SELECT payload FROM automatic_facts")]
        assert {x["valid_from"] for x in values} == {"2026-09-01T00:00:00Z", "2026-10-20T00:00:00Z"}
    assert not recall(sync, "我现在的住所")


@pytest.mark.parametrize(
    "tool,studio,tea",
    [("Penpot", "桥北陶艺工作室", "普洱茶"), ("Figma", "月白陶艺工作室", "茉莉花茶")],
)
def test_work_and_software_query_excludes_visited_studio_and_beverage(sync, tool, studio, tea):
    initialize(sync)
    job, _ = add(sync, "我的职业是前端设计师", "occupation", "前端设计师")
    software, _ = add(sync, f"我平常使用 {tool}", "tool.design", tool, second=2)
    add(sync, f"我平时喜欢喝{tea}", "beverage.tea", tea, second=3)
    add(sync, f"我常去{studio}做陶艺", "location.pottery_studio", studio, second=4)
    units = recall(sync, "我平常做的工作和常用软件是什么？")
    assert {u["record_id"] for u in units} == set(job["record_ids"] + software["record_ids"])
    assert any(studio in u["statement"] for u in recall(sync, "我去哪儿做陶艺"))
    assert any(tea in u["statement"] for u in recall(sync, "我喝什么茶"))


def test_actual_studio_employer_and_english_software_queries_are_retained(sync):
    initialize(sync)
    employer, _ = add(sync, "我在松林工作室上班", "employer", "松林工作室")
    software, _ = add(sync, "I normally use Krita", "tool.art", "Krita", second=2)
    assert {u["record_id"] for u in recall(sync, "我的工作单位是什么")} == set(
        employer["record_ids"]
    )
    assert {u["record_id"] for u in recall(sync, "What software do I use?")} == set(
        software["record_ids"]
    )
    assert recall(sync, "我的护照号码是什么") == []


def test_unproven_future_date_is_rejected_instead_of_becoming_current():
    from tianshu_memory.domain import Fault
    from tianshu_memory.temporal_facts import sourced_times

    fact = dict(valid_from="2026-10-20T00:00:00Z", valid_to=None)
    evidence = [dict(source_index=0, quote="我住在春城")]
    with pytest.raises(Fault) as rejected:
        sourced_times(fact, evidence, [dict(text="下周我住在春城", sent_at="2026-10-11T00:00:00Z")])
    assert rejected.value.status == 400


def test_relative_date_requires_matching_source_day_and_quote():
    from tianshu_memory.domain import Fault
    from tianshu_memory.temporal_facts import sourced_times

    fact = dict(valid_from="2026-10-12T00:00:00Z", valid_to=None)
    evidence = [dict(source_index=0, quote="明天我搬到春城")]
    content = [dict(text="明天我搬到春城", sent_at="2026-10-11T09:30:00Z")]
    assert sourced_times(fact, evidence, content) == fact
    with pytest.raises(Fault):
        sourced_times(dict(fact, valid_from="2026-10-13T00:00:00Z"), evidence, content)


def test_undated_confirmation_preserves_explicit_start_and_merges_sources(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "2026年9月1日起我养的猫叫米糕",
        "pet.cat.name",
        "米糕",
        valid_from="2026-09-01T00:00:00Z",
    )
    repeated, _ = add(sync, "我养的猫还是米糕", "pet.cat.name", "米糕", second=2)
    assert first["record_ids"] == repeated["record_ids"]
    with sync.store.transaction() as db:
        row = db.execute("SELECT payload FROM automatic_facts").fetchone()
        assert json.loads(row[0])["valid_from"] == "2026-09-01T00:00:00Z"
        assert db.execute("SELECT COUNT(*) FROM lineage").fetchone()[0] == 2


def test_same_value_with_different_negation_is_not_a_duplicate(sync):
    initialize(sync)
    first, _ = add(sync, "我晚上不喝红茶", "beverage.tea", "红茶", negations=["晚上不喝"])
    repeated, _ = add(
        sync, "我早上不喝红茶", "beverage.tea", "红茶", second=2, negations=["早上不喝"]
    )
    assert first["record_ids"] != repeated["record_ids"]
