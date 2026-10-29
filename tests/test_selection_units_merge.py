from tianshu_memory.memory_context.merge import by_owner, mmr_diversify, quota_merge, unit_items
from tianshu_memory.memory_context.read_v2 import _selection_units


def test_quota_covers_twenty_owners_before_spare_score_slots():
    owners = {
        f"p{i:02}": [dict(id=f"p{i}:{j}", score=1 - j / 10) for j in range(3)] for i in range(20)
    }
    result = quota_merge(owners, 20)
    assert len(result) == 20
    assert {item["id"].split(":")[0] for item in result} == {f"p{i}" for i in range(20)}


def test_mmr_reduces_near_duplicate_pairs_within_a_shortlist():
    items = [dict(id="a", score=1.0), dict(id="b", score=0.99), dict(id="c", score=0.8)]
    vectors = {"a": (1.0, 0.0), "b": (1.0, 0.0), "c": (0.0, 1.0)}
    assert [item["id"] for item in mmr_diversify(items, vectors, limit=2)] == ["a", "c"]


def test_same_window_selection_unit_is_admitted_whole_or_omitted():
    scope = '{"person_id":"p1"}'
    documents = [
        dict(id=f"fact:{identity}", group_id=identity, kind="summary", scope=scope)
        for identity in ("a", "b")
    ]
    frame = dict(
        summaries={identity: dict(kind="summary", window_id="same") for identity in ("a", "b")},
        group_map={
            identity: (dict(scope=scope), [], [dict(subject_person_id="p1")])
            for identity in ("a", "b")
        },
    )
    plan = dict(subquery=dict(intent="current"), documents=documents, ranked=documents[:1])
    units = _selection_units(frame, plan, context_mode=True)
    items = unit_items(units, {"fact:a": 0})
    assert len(units) == 1 and len(units[0]) == 2
    assert quota_merge(by_owner(items), 1) == []
    assert quota_merge(by_owner(items), 2)[0]["members"] == documents
