"""Personal chronology and coverage through the existing guarded browser route."""

from unittest.mock import patch

from test_browser_catalog import post
from test_browser_extensions import bootstrap
from test_contextual_recall import contextual_query
from test_group_context import owner
from test_segments import forget_original, front, output, pending, seal, submit
from test_segments import (
    test_pure_core_higher_revision_is_a_new_material_with_exact_revision_coverage as revised_material_scenario,
)
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


def test_browser_explains_own_memory_without_exposing_other_people_or_stale_sources(sync):
    bootstrap(sync)
    material, event = front(sync, "我周末去河边散步了。", "read-model:own")
    materials = [material]
    if sync.scope()["audience"] == "group":
        index = owner(sync)
        materials.append(
            front(sync, "别人的独立经历，不属于当前本人。", "read-model:other", index)[0]
        )
    waiting = post(sync, "overview").json()["learning"]
    assert waiting == dict(received=1, completed=0, waiting=1, truncated=False)
    prepared = pending(sync, seal(sync, materials)[0])
    response, _ = submit(sync, prepared, output(prepared))
    assert response.status_code == 200, response.text
    response = post(sync, "records")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    details = items[0]["provenance"]
    assert details["subject_person_id"] == sync.scope()["person_id"]
    assert details["source_count"] == 1
    assert details["effective_at"] == "2026-10-24T00:00:00Z"
    assert details["retention"] == "medium" and details["memory_state"] == "described"
    if sync.scope()["audience"] == "self_private":
        assert [item["text"] for item in details["source_excerpts"]] == ["我周末去河边散步了。"]
    else:
        assert details["source_excerpts"] == []
    assert "别人的独立经历" not in response.text
    assert "message_key" not in response.text and "source_key" not in response.text
    assert post(sync, "overview").json()["learning"] == dict(
        received=1,
        completed=1,
        waiting=0,
        truncated=False,
    )
    forget_original(sync, material, 0)
    response = post(sync, "records")
    assert response.status_code == 200 and response.json()["items"] == []
    assert "我周末去河边散步了" not in response.text


def test_empty_learning_result_is_completed_coverage_and_not_a_missing_job(sync):
    bootstrap(sync)
    material, _ = front(sync, "只是打了个招呼。", "read-model:empty")
    prepared = pending(sync, seal(sync, [material])[0])
    proposed = output(prepared, empty=[sync.scope()["person_id"]])
    response, _ = submit(sync, prepared, proposed)
    assert response.status_code == 200, response.text
    assert post(sync, "records").json()["items"] == []
    progress = post(sync, "overview").json()["learning"]
    assert progress["completed"] == 1 and progress["waiting"] == 0


def test_context_recall_uses_generic_text_candidates_without_scene_alias_rules(sync):
    bootstrap(sync)
    with (
        patch(
            "tianshu_memory.recall_language.normalize_objects",
            side_effect=AssertionError("object rule"),
        ),
        patch(
            "tianshu_memory.recall_language.topic_tokens", side_effect=AssertionError("topic rule")
        ),
        patch(
            "tianshu_memory.recall_language.candidate_aliases",
            side_effect=AssertionError("alias rule"),
        ),
    ):
        material, _ = front(sync, "我想给橙色保温壶配一个软木杯垫。", "read-model:recall")
        prepared = pending(sync, seal(sync, [material])[0])
        proposed = output(prepared)
        proposed["results"][0]["memories"][0]["text"] = "想给橙色保温壶配软木杯垫，尚未购买。"
        response, _ = submit(sync, prepared, proposed)
        assert response.status_code == 200, response.text
        result = contextual_query(sync, "之前说的软木杯垫是什么安排？")
    assert any(
        item["kind"] == "summary" and "软木杯垫" in item["unit"]["statement"]
        for item in result["evidence"]
    )
    assert all(item["owner_scope"] == sync.scope() for item in result["evidence"])


def test_personal_source_excerpt_tracks_the_exact_saved_revision(sync):
    # Reuse the real source-owner edit and reorganization scenario; the old and
    # new immutable material rows deliberately share the same physical key.
    revised_material_scenario(sync)
    bootstrap(sync)
    response = post(sync, "records")
    assert response.status_code == 200, response.text
    assert "原来周六见。" not in response.text
    items = response.json()["items"]
    assert len(items) == 1
    if sync.scope()["audience"] == "self_private":
        assert [item["text"] for item in items[0]["provenance"]["source_excerpts"]] == [
            "改为周日见。"
        ]
    else:
        assert items[0]["provenance"]["source_excerpts"] == []
    progress = post(sync, "overview").json()["learning"]
    assert progress == dict(received=1, completed=1, waiting=0, truncated=False)
