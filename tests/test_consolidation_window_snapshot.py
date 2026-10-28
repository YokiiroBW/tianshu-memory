"""The same complete window decisions with transaction-local batched SQL reads."""

import pytest
from summary_fixtures import (
    pending,
    prepare,
    query,
    submit,
    window,
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
