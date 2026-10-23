"""Explicit property updates differ from parallel assertion modalities."""

import json

import pytest
from test_fact_provenance import forget_request, ref
from test_memory_v2 import candidate, commit, fact, post, query, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical


def write(sync, text, serial, value):
    job, _ = candidate(sync, text, serial)
    result = commit(sync, job, text, value)
    assert result["state"] == "completed", result
    return result["result"]["group_ids"][0]


@pytest.mark.parametrize(
    "before,after",
    [
        ("uncertain", "planned"),
        ("uncertain", "asserted"),
        ("uncertain", "observed"),
        ("asserted", "observed"),
        ("planned", "observed"),
    ],
)
def test_exact_replace_can_change_modality_without_retiring_other_slots(sync, before, after):
    setup(sync)
    first = write(
        sync,
        "我记录的行程是去河岸。",
        1,
        fact("去河岸", predicate="event.departure", modality=before),
    )
    other_mode = next(
        mode for mode in ("conditional", "planned", "uncertain") if mode not in {before, after}
    )
    other = write(
        sync,
        "另有条件下去花园。",
        2,
        fact("去花园", predicate="event.departure", modality=other_mode),
    )
    newest = write(
        sync,
        "现在已明确这项行程。",
        3,
        fact(
            "去河岸",
            predicate="event.departure",
            modality=after,
            operation="replace",
            targets=[ref(sync, first)],
        ),
    )
    with sync.store.transaction() as db:
        rows = {row["group_id"]: row for row in db.execute("SELECT * FROM automatic_facts")}
        assert rows[first]["superseded_by"] == newest
        assert rows[other]["superseded_by"] is None
        assert rows[newest]["superseded_by"] is None
        assert len({row["fact_key"] for row in rows.values()}) == 3
    values = [item["fact"] for item in query(sync, "河岸 花园")["evidence"] if item.get("fact")]
    assert {(value["value"], value["modality"]) for value in values} == {
        ("去河岸", after),
        ("去花园", other_mode),
    }


def test_without_targets_planned_and_observed_remain_parallel(sync):
    setup(sync)
    first = write(
        sync, "我计划去河岸。", 1, fact("去河岸", predicate="event.departure", modality="planned")
    )
    second = write(
        sync,
        "我已经去了河岸。",
        2,
        fact("去河岸", predicate="event.departure", modality="observed"),
    )
    with sync.store.transaction() as db:
        assert all(
            row[0] is None
            for row in db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id IN (?,?)", (first, second)
            )
        )
    values = [item["fact"] for item in query(sync, "河岸")["evidence"] if item.get("fact")]
    assert {value["modality"] for value in values} == {"planned", "observed"}


@pytest.mark.parametrize(
    "change", ["predicate", "subject", "reality", "cardinality", "confirm", "retract"]
)
def test_explicit_target_does_not_cross_property_or_derived_modality_boundaries(sync, change):
    setup(sync)
    first = write(
        sync, "我计划去河岸。", 1, fact("去河岸", predicate="event.departure", modality="planned")
    )
    text = "我已报告去河岸，我的猫也在河岸。"
    proposal = fact(
        "去河岸",
        predicate="event.departure",
        modality="observed",
        operation="replace",
        targets=[ref(sync, first)],
    )
    if change == "predicate":
        proposal["predicate"] = "event.attendance"
    elif change == "subject":
        proposal["subject"] = dict(kind="entity", id=None, type="cat", mention="我的猫")
    elif change == "cardinality":
        proposal["cardinality"] = "set"
    elif change in {"confirm", "retract"}:
        proposal["operation"] = change
    job, _ = candidate(sync, text, 2, reality="fictional" if change == "reality" else "real")
    assert commit(sync, job, text, proposal)["state"] == "rejected"
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?", (first,)
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("operation", ["confirm", "retract"])
def test_derived_dates_come_from_target_but_retirement_uses_control_observation(sync, operation):
    setup(sync)
    start = "2026-10-01T00:00:00Z"
    end = "2026-11-01T00:00:00Z"
    original = dict(fact("合成甲", predicate="name"), valid_from=start, valid_to=end)
    first = write(sync, "我从2026年10月1日起叫合成甲。这个名字用到2026年11月1日。", 1, original)
    episode = next(item for item in query(sync, "合成甲")["evidence"] if item["kind"] == "episode")
    text = "确认之前那项记录。" if operation == "confirm" else "撤回之前那项记录。"
    proposal = dict(
        original, predicate="author.name", operation=operation, target_refs=[ref(sync, first)]
    )
    job, _ = candidate(sync, text, 2)
    wrong = dict(proposal, valid_from="2026-09-01T00:00:00Z")
    assert commit(sync, job, text, wrong)["state"] == "rejected"
    result = commit(sync, job, text, proposal)
    assert result["state"] == "completed", result
    second = result["result"]["group_ids"][0]
    with sync.store.transaction() as db:
        first_unit = json.loads(
            db.execute("SELECT payload FROM records WHERE group_id=?", (first,)).fetchone()[0]
        )
        current = json.loads(
            db.execute(
                "SELECT payload FROM automatic_facts WHERE group_id=?", (second,)
            ).fetchone()[0]
        )
        assert "2026-10-11T00:00:02Z 起被后续陈述替代" in first_unit["valid_time"]
        assert current["valid_from"] == start and current["valid_to"] == end
    forgotten = post(sync, "forget", forget_request(sync, episode, 3))
    assert forgotten.status_code == 200 and forgotten.json()["state"] == "tombstoned", (
        forgotten.text
    )
    assert "合成甲" not in canonical(query(sync, "我叫什么")["evidence"])


@pytest.mark.parametrize("operation", ["assert", "replace"])
def test_independent_dates_still_require_this_sources_temporal_evidence(sync, operation):
    setup(sync)
    first = write(sync, "我喜欢乌龙茶。", 1, fact("乌龙茶"))
    text = "我喜欢红茶。"
    job, _ = candidate(sync, text, 2)
    proposal = dict(
        fact(
            "红茶",
            operation=operation,
            targets=[ref(sync, first)] if operation == "replace" else [],
        ),
        valid_from="2026-09-01T00:00:00Z",
    )
    assert commit(sync, job, text, proposal)["state"] == "rejected"
