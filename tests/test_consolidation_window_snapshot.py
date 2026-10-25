"""The same complete window decisions with transaction-local batched SQL reads."""

import copy

import pytest
from test_consolidation import (
    assistant_member,
    pending,
    prepare,
    query,
    submit,
    window,
    withdraw_source,
)
from test_memory_v2 import candidate
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import consolidation_snapshot, consolidation_sources
from tianshu_memory.domain import canonical


class ObservedReads:
    def __init__(self, db):
        self.db, self.statements, self.batches = db, 0, 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, parameters=()):
        self.statements += 1
        return self.db.execute(sql, parameters)

    def read_batch(self, requests):
        requests = list(requests)
        self.batches += 1
        self.statements += len(requests)
        if hasattr(self.db, "read_batch"):
            return self.db.read_batch(requests)
        return [self.db.execute(sql, parameters) for sql, parameters in requests]


def decision(failure):
    return (
        None
        if failure is None
        else (failure.window_id, failure.member_ids, failure.new_inputs_valid)
    )


def compare_uncached_checks(monkeypatch):
    """Compare to fresh original direct reads before any invalidation can run."""
    original, checked = consolidation_sources.window_failure, []

    def observed(service, db, manifest, turns, owner_check=None, *, allowed=None, snapshot=None):
        actual = original(
            service, db, manifest, turns, owner_check, allowed=allowed, snapshot=snapshot
        )
        uncached = ObservedReads(db)
        expected = original(service, uncached, manifest, turns, owner_check, allowed=allowed)
        assert decision(actual) == decision(expected)
        checked.append((decision(actual), uncached.statements))
        return actual

    monkeypatch.setattr(consolidation_sources, "window_failure", observed)
    return checked


@pytest.mark.parametrize("batch_size", [2, 256])
def test_overlapping_window_inputs_are_read_once_and_keep_every_window(
    sync, monkeypatch, batch_size
):
    monkeypatch.setattr(consolidation_snapshot, "_READ_BATCH", batch_size)
    prepare(sync)
    jobs = []
    for index in range(1, 5):
        job = candidate(sync, f"第{index}次讨论河边散步。", index, capture=False)[0]
        supplied = window(sync, [job], context=jobs)
        submit(sync, pending(sync, [job], supplied), f"第{index}次散步讨论。")
        jobs.append(job)

    checked = compare_uncached_checks(monkeypatch)
    original_snapshot, snapshots = consolidation_snapshot.WindowSnapshot, []

    def observed_snapshot(service, db, manifests, *, consolidated):
        manifests = list(manifests)
        reads = ObservedReads(db)
        result = original_snapshot(service, reads, manifests, consolidated=consolidated)
        snapshots.append((manifests, reads, result))
        return result

    monkeypatch.setattr(consolidation_snapshot, "WindowSnapshot", observed_snapshot)
    result = query(sync, "之前讨论过什么河边散步经历？", kinds=["facts"])
    assert result["evidence"]
    assert len(snapshots) == 1
    manifests, reads, snapshot = snapshots[0]
    assert len(manifests) == len(checked) == 4
    assert len(snapshot.sources) == 4 and len(snapshot.bindings) == 1
    assert all(outcome is None for outcome, _ in checked)
    assert reads.batches == 1
    assert reads.statements < sum(count for _, count in checked)


@pytest.mark.parametrize("damage", ["withdrawal", "binding", "owner-turn", "owner-check"])
def test_batch_keeps_exact_member_failures_and_does_not_survive_the_next_request(
    sync, monkeypatch, damage
):
    from test_candidate_extraction import PATH

    prepare(sync)
    old = candidate(sync, "上次讨论过海边旅行。", 1, capture=False)[0]
    submit(sync, pending(sync, [old], window(sync, [old])), "以前讨论过海边旅行。")
    new = candidate(sync, "之后改为去河边。", 2, capture=False)[0]
    supplied = window(sync, [new], context=[old])
    checked = compare_uncached_checks(monkeypatch)
    if damage == "withdrawal":
        withdraw_source(sync, old)
    elif damage == "binding":
        supplied = copy.deepcopy(supplied)
        supplied["manifest"]["members"][0]["binding_version"] += 1
        supplied["manifest"]["window_id"] = consolidation_sources.fingerprint(
            {key: value for key, value in supplied["manifest"].items() if key != "window_id"}
        )
    elif damage == "owner-turn":
        old_turn = supplied["manifest"]["members"][0]["turn_id"]
        turn = next(item for item in sync.turns if item["turn_id"] == old_turn)
        turn["input_revision"] += 1
        turn["committed_event"]["input_revision"] = turn["input_revision"]
        sync.core_head["sequence"] += 1
    else:
        check = sync.window_checker

        def denied(body):
            result = check(body)
            result.update(
                state="invalid",
                invalid_member_ids=[supplied["manifest"]["members"][0]["member_id"]],
            )
            return result

        sync.window_checker = denied
    response = sync.post(
        PATH,
        dict(
            job_id=new,
            job_ids=[new],
            output=None,
            supported_extraction_schemas=[5],
            window=supplied,
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "context_changed"
    assert response.json()["invalid_context_member_ids"] == [
        supplied["manifest"]["members"][0]["member_id"]
    ]
    assert any(value is not None for value, _ in checked)
    # A clean new window must use a new final transaction's reads, rather than
    # retaining the negative snapshot or reusing the previous valid one.
    if damage == "owner-check":
        sync.window_checker = check
    clean = pending(sync, [new], window(sync, [new]))
    assert clean["state"] == "pending"


def test_batched_window_checks_keep_bot_record_and_episode_dependencies(sync, monkeypatch):
    from test_candidate_extraction import PATH

    prepare(sync)
    first = candidate(sync, "上次去过海边。", 1, capture=False)[0]
    done, _ = submit(sync, pending(sync, [first], window(sync, [first])), "曾去过海边。")
    recalled = query(sync, "海边", kinds=["facts", "episodes"])["bundle"]
    assert recalled["group_dependencies"] and recalled["episode_dependencies"]
    context_job = candidate(sync, "谢谢提醒。", 2, capture=False)[0]
    context = window(sync, [context_job])
    assistant = assistant_member(
        sync,
        context["manifest"]["members"][0],
        "你之前去过海边。",
        memory_dependencies={
            key: recalled[key]
            for key in ("group_dependencies", "episode_dependencies", "source_dependencies")
        },
    )
    new = candidate(sync, "继续聊旅行。", 3, capture=False)[0]
    supplied = window(sync, [new], context=[context_job], assistant=[assistant])
    checked = compare_uncached_checks(monkeypatch)
    prepared = pending(sync, [new], supplied)
    assert prepared["state"] == "pending"
    assert checked and all(value is None for value, _ in checked)
    revision = sync.revision(done["result"]["record_ids"][0])
    assert sync.post("memory/revise", revision).status_code == 200
    response = sync.post(
        PATH,
        dict(
            job_id=new,
            job_ids=[new],
            batch_ref=prepared["batch_ref"],
            extraction_schema=5,
            output=dict(subject_person_id=sync.person, summary="继续聊旅行。", portrait=None),
        ),
    )
    assert response.status_code == 200 and response.json()["state"] == "invalidated", response.text


def test_same_room_different_people_keep_their_actual_bindings_in_the_snapshot(sync, monkeypatch):
    from test_group_context import owner

    if sync.scope()["audience"] != "group":
        pytest.skip("This window needs different authors in the same authorized room")
    prepare(sync)
    other = owner(sync)
    old = candidate(sync, "我打算去海边。", 1, other, capture=False)[0]
    new = candidate(sync, "我打算去河边。", 2, capture=False)[0]
    supplied = window(sync, [new], context=[old])
    checked = compare_uncached_checks(monkeypatch)
    result = pending(sync, [new], supplied)
    assert result["state"] == "pending" and checked
    assert all(value is None for value, _ in checked)
    old_member = supplied["manifest"]["members"][0]
    new_member = supplied["manifest"]["members"][1]
    assert canonical(old_member["source_scope"]) != canonical(new_member["source_scope"])
    assert old_member["speaker"]["person_id"] == sync.scope(other)["person_id"]
