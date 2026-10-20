import json
from contextlib import contextmanager

import pytest

from tianshu_memory.domain import canonical


def test_keyword_index_is_read_once_for_many_authorized_records(h):
    h.seed(
        [
            h.draft(
                field_key=f"fixture-{i}",
                units=[
                    h.unit(
                        statement="我的门卡放在书房" if i == 0 else f"照片编号{i}放在相册",
                        conditions=[],
                        negations=[],
                    )
                ],
            )
            for i in range(32)
        ]
    )
    statements = []
    transaction = h.store.transaction

    @contextmanager
    def observe():
        with transaction() as db:
            db.set_trace_callback(statements.append)
            yield db

    h.store.transaction = observe
    result = h.select(query="我的门卡放哪儿")
    assert [unit["statement"] for unit in result["selected_units"]] == ["我的门卡放在书房"]
    scans = [q for q in statements if q.startswith("SELECT ") and "FROM search_index" in q]
    assert len(scans) == 1


def test_whole_group_validation_batches_sources_and_records_without_changing_order(h):
    h.seed(
        [
            h.draft(field_key=f"coffee-{i}", units=[h.unit(statement=f"咖啡品种{i}")])
            for i in range(32)
        ]
    )
    statements = []
    transaction = h.store.transaction

    @contextmanager
    def observe():
        with transaction() as db:
            db.set_trace_callback(statements.append)
            yield db

    h.store.transaction = observe
    result = h.select(query="咖啡")
    assert len(result["selected_units"]) == 32
    records = [q for q in statements if q.startswith("SELECT * FROM records WHERE group_id")]
    sources = [q for q in statements if q.startswith("SELECT l.group_id,")]
    assert len(records) == len(sources) == 1
    assert [g["semantic_group_id"] for g in result["dependency_groups"]] == sorted(
        g["semantic_group_id"] for g in result["dependency_groups"]
    )


@pytest.mark.parametrize("damage", ["version", "record", "group", "person", "members", "source"])
def test_batched_validation_drops_the_whole_inconsistent_group(h, damage):
    h.seed([h.draft(units=[h.unit(), h.unit(statement="咖啡的另一条限定")])])
    with h.store.transaction() as db:
        record = db.execute("SELECT * FROM records ORDER BY id LIMIT 1").fetchone()
        unit = json.loads(record["payload"])
        if damage == "members":
            db.execute("UPDATE groups SET members='[]'")
        elif damage == "source":
            db.execute("UPDATE sources SET epoch=epoch+1")
        else:
            key = {
                "version": "record_version",
                "record": "record_id",
                "group": "semantic_group_id",
                "person": "subject_person_id",
            }[damage]
            unit[key] = 200 if damage == "version" else "unrelated"
            db.execute("UPDATE records SET payload=? WHERE id=?", (canonical(unit), record["id"]))
    assert h.select()["selected_units"] == []


@pytest.mark.parametrize("damage", ["missing", "withdrawn", "version", "owner"])
def test_batched_group_projection_needs_every_current_projection(h, damage):
    h.seed([h.draft(scope=h.group, units=[h.unit(), h.unit(statement="咖啡仍有条件")])])
    with h.store.transaction() as db:
        ref = db.execute("SELECT ref FROM projections ORDER BY ref LIMIT 1").fetchone()[0]
        if damage == "missing":
            db.execute("DELETE FROM projections WHERE ref=?", (ref,))
        else:
            field, value = {
                "withdrawn": ("state", "withdrawn"),
                "version": ("version", 200),
                "owner": (
                    "record_id",
                    db.execute("SELECT record_id FROM projections WHERE ref!=?", (ref,)).fetchone()[
                        0
                    ],
                ),
            }[damage]
            db.execute(f"UPDATE projections SET {field}=? WHERE ref=?", (value, ref))
    assert h.select(scope=h.group)["selected_units"] == []
