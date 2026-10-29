from test_segments import front, output, pending, ready, seal, submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.target_rotation import covered, order


def test_twenty_authorized_targets_rotate_in_three_eight_target_batches(sync):
    ready(sync)
    sync.store.migrate_rotation(sync.directory / "pre-rotation.sqlite")
    targets = [dict(scope=dict(sync.scope(), person_id=f"p{i}")) for i in range(20)]
    seen = set()
    with sync.store.transaction() as db:
        before = db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        for _ in range(3):
            selected = order(db, targets)[:8]
            for target in selected:
                seen.add(target["scope"]["person_id"])
                covered(db, target["scope"], "2026-10-30T00:00:00Z")
        assert len(seen) == 20
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
            == before
        )


def test_only_a_committed_receipt_advances_coverage_and_replay_is_idempotent(sync):
    ready(sync)
    sync.store.migrate_rotation(sync.directory / "pre-rotation.sqlite")
    material, _ = front(sync, "今天散步。", "rotation")
    prepared = pending(sync, seal(sync, [material])[0])
    value = output(prepared)
    response, _ = submit(sync, prepared, value)
    assert response.status_code == 200
    assert submit(sync, prepared, value)[0].json() == response.json()
    with sync.store.transaction() as db:
        assert db.execute("SELECT covered_count FROM memory_target_rotation").fetchone()[0] == 1
