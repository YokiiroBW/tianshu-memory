import pytest
from test_automatic_facts import add, initialize, recall
from test_fact_entities import entity, saved_entity
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


def test_cover_alias_updates_same_object_and_new_title_retrieves_old_name(sync):
    initialize(sync)
    red, _ = add(
        sync,
        "红封面那本笔记叫《山谷》",
        "notebook.title",
        "《山谷》",
        entity=entity("红封面那本笔记", kind="notebook"),
    )
    blue, _ = add(
        sync,
        "蓝封面那本笔记叫《远山》",
        "notebook.title",
        "《远山》",
        second=2,
        entity=entity("蓝封面那本笔记", kind="notebook"),
    )
    changed, _ = add(
        sync,
        "蓝皮那本改叫《晨雾》了",
        "notebook.title",
        "《晨雾》",
        second=3,
        entity=entity("蓝皮那本", kind="notebook"),
    )
    assert saved_entity(sync, changed)["id"] == saved_entity(sync, blue)["id"]
    assert saved_entity(sync, changed)["id"] != saved_entity(sync, red)["id"]
    assert {u["record_id"] for u in recall(sync, "蓝皮本现在叫什么？")} == set(
        changed["record_ids"]
    )
    assert {u["record_id"] for u in recall(sync, "《晨雾》的原名是什么？")} == set(
        blue["record_ids"] + changed["record_ids"]
    )
    assert {u["record_id"] for u in recall(sync, "红皮和蓝皮两本现在分别叫啥？")} == set(
        red["record_ids"] + changed["record_ids"]
    )


@pytest.mark.parametrize("endpoint", ["legacy", "context"])
def test_short_keywords_preserve_cover_alias_and_title_history(sync, endpoint):
    initialize(sync)
    old, _ = add(
        sync,
        "绿封面那本笔记本叫《山窗》。",
        "notebook.name",
        "《山窗》",
        keywords=["笔记本", "命名", "绿封面", "山窗"],
        entity=entity("绿封面那本笔记本", kind="notebook"),
    )
    gray, _ = add(
        sync,
        "灰封面的那本笔记本叫《河畔》。",
        "notebook.name",
        "《河畔》",
        second=2,
        keywords=["笔记本", "命名", "灰封面", "河畔"],
        entity=entity("灰封面的那本", kind="notebook"),
    )
    changed, _ = add(
        sync,
        "绿皮那本笔记本现在改叫《竹雨》。",
        "notebook.name",
        "《竹雨》",
        second=3,
        keywords=["笔记本", "改名", "绿皮", "竹雨"],
        entity=entity("绿皮那本", kind="notebook"),
    )

    def select(text):
        if endpoint == "legacy":
            return recall(sync, text)
        from tianshu_memory.memory_context import MemoryContext

        request = dict(
            sync.selection(budget=100000),
            query_text=text,
            selection=["evidence"],
            include_associated=False,
            time_range=None,
            limit=32,
            known_association_version=None,
            known_scope_checks=None,
        )
        return MemoryContext(sync.service).query(request, sync.contexts["synthetic-viewer:0"])[
            "selected_units"
        ]

    assert saved_entity(sync, old)["id"] == saved_entity(sync, changed)["id"]
    assert {u["record_id"] for u in select("绿皮和灰皮的两本现在分别叫什么？")} == set(
        gray["record_ids"] + changed["record_ids"]
    )
    assert {u["record_id"] for u in select("《竹雨》以前叫什么？")} == set(
        old["record_ids"] + changed["record_ids"]
    )


def test_ambiguous_same_cover_objects_are_not_merged(sync):
    initialize(sync)
    add(
        sync,
        "第一本蓝封面笔记叫《甲》",
        "notebook.title",
        "《甲》",
        entity=entity("第一本蓝封面笔记", kind="notebook"),
    )
    add(
        sync,
        "第二本蓝封面笔记叫《乙》",
        "notebook.title",
        "《乙》",
        second=2,
        entity=entity("第二本蓝封面笔记", kind="notebook"),
    )
    add(
        sync,
        "蓝皮那本改叫《丙》",
        "notebook.title",
        "《丙》",
        second=3,
        entity=entity("蓝皮那本", kind="notebook"),
        expected_state="rejected",
    )


def test_journal_alias_reuses_explicit_known_identity(sync):
    initialize(sync)
    first, _ = add(
        sync,
        "白色封面的手账叫《云站》",
        "journal.title",
        "《云站》",
        entity=entity("白色封面的手账", kind="journal"),
    )
    known = saved_entity(sync, first)
    changed, _ = add(
        sync,
        "白皮那本现在叫《潮声》了",
        "journal.title",
        "《潮声》",
        second=2,
        entity={**known, "mention": "白皮那本"},
    )
    assert saved_entity(sync, changed)["id"] == known["id"]
    assert {u["record_id"] for u in recall(sync, "白皮那本现在叫什么？")} == set(
        changed["record_ids"]
    )


@pytest.mark.parametrize("kind", ["wallet", "pen"])
def test_cover_normalization_does_not_apply_to_other_object_kinds(kind):
    from tianshu_memory.fact_entities import anchor, compatible

    assert not compatible(
        anchor(entity("蓝皮那本", kind=kind)), anchor(entity("蓝封面那本", kind=kind))
    )
