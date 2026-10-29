from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical
from tianshu_memory.memory_context.partition import candidate_limit, interleave, partition
from tianshu_memory.partition_index import rank_terms


def test_index_migration_and_reads_keep_the_authority_watermark(sync):
    from test_memory_v2 import setup

    from tianshu_memory.partition_index import refresh

    setup(sync)
    result = sync.store.migrate_partitions(sync.directory / "pre-partitions.sqlite")
    assert result["memory_partition_schema"] == 1
    with sync.store.transaction() as db:
        before = db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        refresh(db, "unrepresented-derived-group")
        assert (
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
            == before
        )
    assert sync.store.migrate_partitions(sync.directory / "unused")["already_installed"]


def test_twenty_owner_candidate_recall_does_not_drop_the_last_owners():
    scopes = [
        dict(person_id=f"p{i}", actor_id="actor", audience="group", conversation_id="room")
        for i in range(20)
    ]
    eligible = [
        (dict(id=f"{i}:{j}", scope=canonical(scope)), [], [])
        for i, scope in enumerate(scopes)
        for j in range(3)
    ]
    partitions = partition(eligible, scopes)
    candidates = interleave(
        {key: rows[: candidate_limit(32, len(partitions))] for key, rows in partitions.items()}
    )
    assert len({entry[0]["scope"] for entry in candidates[:20]}) == 20
    assert len(candidates) == 60
    assert len(partition(eligible, scopes[:1])) == 1


def test_bm25_is_local_to_each_owner_and_ignores_unselected_index_rows():
    rows = [(dict(id=f"p{i}:{j}", scope=f"p{i}"), [], []) for i in range(20) for j in range(3)]
    partitions = partition(rows)
    indexed = {entry[0]["id"]: "meeting plan" for entry in rows}
    indexed["hidden"] = "meeting " * 500
    ranked = rank_terms(partitions, indexed, ["meeting"])
    assert len(ranked) == 20
    assert len(interleave(ranked)) == 60
    assert all(entry[0]["id"] != "hidden" for entry in interleave(ranked))
