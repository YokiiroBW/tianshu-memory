import math

from test_segments import front, output, pending, ready, seal, submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical
from tianshu_memory.persistent_retrieval import write


def saved(h, serial, text, vector):
    material, _ = front(h, text, serial)
    prepared = pending(h, seal(h, [material], "merge:" + serial)[0])
    value = output(prepared)
    value["results"][0]["memories"][0]["text"] = text
    done, _ = submit(h, prepared, value)
    assert done.status_code == 200
    ref = done.json()["results"][0]["memory_refs"][0]
    with h.store.transaction() as db:
        document = dict(
            db.execute(
                "SELECT d.* FROM memory_documents d JOIN records r ON r.group_id=d.group_id WHERE r.id=?",
                (ref["record_id"],),
            ).fetchone()
        )
        before = db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        assert write(db, document, "same-revision", vector)
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
            == before
        )
    return document, ref


def test_similar_rephrasing_marks_only_and_next_preparation_supplies_current_refs(sync):
    ready(sync)
    sync.store.migrate_merge_candidates(sync.directory / "pre-merge.sqlite")
    one, first = saved(sync, "first", "门卡放书房。", [1, 0])
    two, second = saved(sync, "second", "书房里放着门卡。", [0.99, 0.01])
    with sync.store.transaction() as db:
        row = db.execute("SELECT * FROM memory_merge_candidates").fetchone()
        assert row["new_group_id"] == two["group_id"] and row["prior_group_id"] == one["group_id"]
        assert row["score"] > 0.92
        assert db.execute("SELECT count(*) FROM groups WHERE state='active'").fetchone()[0] == 2
        assert write(db, two, "same-revision", [0.99, 0.01])
        assert db.execute("SELECT count(*) FROM memory_merge_candidates").fetchone()[0] == 1
    material, _ = front(sync, "再确认门卡位置。", "third")
    prepared = pending(sync, seal(sync, [material], "merge:third")[0])
    response = sync.post(
        "memory/candidates/hints",
        dict(job_id=prepared["job_id"], batch_ref=prepared["batch_ref"]),
    )
    assert response.status_code == 200, response.text
    hints = response.json()["merge_candidates"]
    assert any(hint["new"] == second and hint["prior"] == first for hint in hints)
    assert "text" not in canonical(hints)


def test_threshold_and_owner_isolation_do_not_automatically_merge(sync):
    ready(sync)
    sync.store.migrate_merge_candidates(sync.directory / "pre-merge.sqlite")
    saved(sync, "first", "门卡在书房。", [1, 0])
    saved(sync, "second", "公交卡在抽屉。", [0.919, math.sqrt(1 - 0.919**2)])
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM memory_merge_candidates").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM groups WHERE state='active'").fetchone()[0] == 2
