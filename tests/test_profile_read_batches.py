"""Profile read equivalence and bounded I/O; doubles do not measure PostgreSQL speed."""

import json

import pytest
from test_recall_preparation import RelationalPostgres
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync
from test_storage_backends import pg_sync as pg_sync

from tianshu_memory import profiles
from tianshu_memory.domain import canonical


class ScalarReads:
    """Use the retained scalar profile path over this same transaction/driver."""

    def __init__(self, db):
        self.db, self.statements = db, []

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        return self.db.execute(sql, parameters)


def signature(entries):
    return canonical(
        [(dict(group), [dict(row) for row in rows], units) for group, rows, units in entries]
    )


def profile_groups(h, count):
    reader = h.profile_fixture()
    with h.store.transaction() as db:
        original = db.execute(
            "SELECT g.* FROM groups g JOIN profile_shares p ON p.group_id=g.id"
        ).fetchone()
        record = db.execute("SELECT * FROM records WHERE group_id=?", (original["id"],)).fetchone()
        unit = json.loads(record["payload"])
        for index in range(count):
            group_id, record_id, projection = (
                f"profile-batch-group:{index:04}",
                f"profile-batch-record:{index:04}",
                f"profile-batch-projection:{index:04}",
            )
            value = dict(
                unit,
                record_id=record_id,
                semantic_group_id=group_id,
                sources=[dict(unit["sources"][0], projection_ref=projection)],
            )
            db.execute(
                "INSERT INTO groups VALUES (?,?,'active',?,'interest','interest.coffee',NULL)",
                (group_id, original["scope"], canonical([record_id])),
            )
            db.execute(
                "INSERT INTO records VALUES (?,?,1,'active',?)",
                (record_id, group_id, canonical(value)),
            )
            db.execute(
                "INSERT INTO lineage SELECT ?,source_key,revision,epoch FROM lineage WHERE group_id=?",
                (group_id, original["id"]),
            )
            db.execute("INSERT INTO projections VALUES (?,?,1,'active')", (projection, record_id))
            db.execute(
                "INSERT INTO profile_shares VALUES (?,'actor:a','person',?,'public_preference',NULL,?)",
                (group_id, h.person, f"profile-batch-approval:{index:04}"),
            )
    return reader


def selected_groups(db):
    return db.execute(
        "SELECT g.*,p.sharing FROM profile_shares p JOIN groups g ON g.id=p.group_id "
        "WHERE g.id LIKE 'profile-batch-group:%' ORDER BY g.id"
    ).fetchall()


def damage_group(db, groups, damage):
    group = groups[0]
    record = db.execute("SELECT * FROM records WHERE group_id=?", (group["id"],)).fetchone()
    value = json.loads(record["payload"])
    if damage == "lineage":
        db.execute("UPDATE lineage SET epoch=epoch+1 WHERE group_id=?", (group["id"],))
    elif damage == "members":
        db.execute("UPDATE groups SET members='[]' WHERE id=?", (group["id"],))
    elif damage == "scope":
        scope = json.loads(group["scope"])
        scope["actor_id"] = "another-actor"
        db.execute("UPDATE groups SET scope=? WHERE id=?", (canonical(scope), group["id"]))
    elif damage.startswith("projection-"):
        ref = value["sources"][0]["projection_ref"]
        if damage == "projection-missing":
            db.execute("DELETE FROM projections WHERE ref=?", (ref,))
        elif damage == "projection-owner":
            db.execute(
                "UPDATE projections SET record_id=? WHERE ref=?",
                ("profile-batch-record:0001", ref),
            )
        else:
            db.execute("UPDATE projections SET version=version+1 WHERE ref=?", (ref,))
    else:
        key, bad_value = {
            "subject": ("subject", {"kind": "person", "person_id": "other-person"}),
            "sharing": ("sharing", "group_only"),
            "category": ("category", "style"),
            "field": ("field_key", "different.field"),
            "version": ("record_version", 2),
            "source": ("sources", [{"kind": "raw_message"}]),
        }[damage]
        value[key] = bad_value
        db.execute("UPDATE records SET payload=? WHERE id=?", (canonical(value), record["id"]))


def profile_integrity_case(h, damage, *, branch_double=False):
    reader = profile_groups(h, 4)
    with h.store.transaction() as db:
        damage_group(db, selected_groups(db), damage)
        groups = selected_groups(db)
        connection = RelationalPostgres(db) if branch_double else db
        result = profiles._eligible(
            h.service,
            connection,
            groups,
            reader["allowed_scope"],
            {"kind": "person", "person_id": h.person},
        )
        assert [entry[0]["id"] for entry in result] == [group["id"] for group in groups[1:]]
        assert all(entry[2][0]["subject"]["person_id"] == h.person for entry in result)
        if branch_double and damage in {"lineage", "scope"}:
            assert connection.statements
            record_read = next(sql for sql in connection.statements if "FROM records WHERE" in sql)
            assert " IN (" in record_read


DAMAGE = [
    "lineage",
    "members",
    "scope",
    "subject",
    "sharing",
    "category",
    "field",
    "version",
    "source",
    "projection-missing",
    "projection-owner",
    "projection-version",
]


@pytest.mark.parametrize("damage", DAMAGE)
@pytest.mark.parametrize("branch_double", [False, True], ids=["sqlite", "pg-branch-double"])
def test_profile_batch_keeps_complete_subject_source_and_projection_checks(
    sync, damage, branch_double
):
    profile_integrity_case(sync, damage, branch_double=branch_double)


@pytest.mark.parametrize("damage", DAMAGE)
def test_native_postgres_profile_batch_integrity(pg_sync, damage):
    profile_integrity_case(pg_sync, damage)


def test_profile_batch_approves_sources_before_body_reads_and_keeps_order(sync):
    h = sync
    reader = profile_groups(h, 32)
    with h.store.transaction() as db:
        groups = selected_groups(db)
        scalar = profiles._eligible(
            h.service,
            db,
            groups,
            reader["allowed_scope"],
            {"kind": "person", "person_id": h.person},
        )
        remote = RelationalPostgres(db)
        batched = profiles._eligible(
            h.service,
            remote,
            groups,
            reader["allowed_scope"],
            {"kind": "person", "person_id": h.person},
        )
        assert signature(batched) == signature(scalar)
        assert len(batched) == 32
        assert len([sql for sql in remote.statements if "FROM lineage l JOIN sources" in sql]) == 1
        assert len([sql for sql in remote.statements if "FROM records WHERE" in sql]) == 1
        assert len([sql for sql in remote.statements if "FROM projections WHERE" in sql]) == 1
        assert len([sql for sql in remote.statements if "FROM source_admissions a" in sql]) == 1
        db.execute("UPDATE sources SET state='withdrawn'")
        remote.statements.clear()
        assert (
            profiles._eligible(
                h.service,
                remote,
                groups,
                reader["allowed_scope"],
                {"kind": "person", "person_id": h.person},
            )
            == []
        )
        assert not any("FROM records" in sql for sql in remote.statements)


def test_earlier_invalid_profile_schema_precedes_later_malformed_body(sync):
    h = sync
    reader = profile_groups(h, 2)
    with h.store.transaction() as db:
        groups = selected_groups(db)
        record = db.execute(
            "SELECT payload FROM records WHERE group_id=?", (groups[0]["id"],)
        ).fetchone()
        value = dict(json.loads(record[0]), statement=123)
        db.execute(
            "UPDATE records SET payload=? WHERE group_id=?", (canonical(value), groups[0]["id"])
        )
        db.execute("UPDATE records SET payload='{' WHERE group_id=?", (groups[1]["id"],))
        for connection in (db, RelationalPostgres(db)):
            with pytest.raises(Exception) as error:
                profiles._eligible(
                    h.service,
                    connection,
                    groups,
                    reader["allowed_scope"],
                    {"kind": "person", "person_id": h.person},
                )
            assert type(error.value).__name__ == "ValidationError"


@pytest.mark.parametrize("count", [20, 100])
def test_native_profile_batch_read_counts_and_scalar_equivalence(pg_sync, count):
    h = pg_sync
    reader = profile_groups(h, count)
    target = {"kind": "person", "person_id": h.person}
    with h.store.transaction() as db:
        groups = selected_groups(db)
        scalar, batched = ScalarReads(db), RelationalPostgres(db)
        before = profiles._eligible(h.service, scalar, groups, reader["allowed_scope"], target)
        after = profiles._eligible(h.service, batched, groups, reader["allowed_scope"], target)
        assert signature(before) == signature(after)
        assert len(after) == count
        assert len([sql for sql in scalar.statements if "FROM records WHERE" in sql]) == count
        assert len([sql for sql in batched.statements if "FROM records WHERE" in sql]) == 1
        assert len([sql for sql in batched.statements if "FROM projections WHERE" in sql]) == 1
        assert len([sql for sql in batched.statements if "FROM source_admissions a" in sql]) == 1
