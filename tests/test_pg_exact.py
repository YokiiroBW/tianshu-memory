"""PG exact-read preparation: local SQL doubles and explicitly gated native cases."""

import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_pg_followup import seed_groups
from test_recall_preparation import RelationalPostgres
from test_storage_backends import certificates as certificates
from test_storage_backends import pg_sync as pg_sync
from test_storage_backends import postgres_store
from test_storage_backends import source_contracts as source_contracts
from test_storage_backends import source_examples as source_examples

from tianshu_memory.automatic_facts import filter_current
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.fact_migration import SCHEMA as FACT_SCHEMA
from tianshu_memory.fact_queries import snapshot_for_groups
from tianshu_memory.postgres import GROUP_READ_BATCH, is_postgres
from tianshu_memory.recall_index import eligible_groups, exact_groups
from tianshu_memory.source_authority import SourceAuthority


class ExactReads(RelationalPostgres):
    """Confirmed-result double; it proves neither PG plans nor wire semantics."""

    def __init__(self, db):
        super().__init__(db)
        self.batches, self.metadata = [], []
        self.approved = set()

    def native(self, sql, parameters=()):
        cursor = self.execute(sql, parameters)
        if sql.startswith("WITH candidates AS MATERIALIZED"):
            rows = cursor.fetchall()
            self.metadata.append([dict(row) for row in rows])
            return SimpleNamespace(fetchall=lambda: rows)
        return cursor

    def read_batch(self, requests):
        self.batches.append(deepcopy(requests))
        for sql, parameters in requests:
            if "FROM records WHERE group_id IN" in sql or "FROM automatic_facts" in sql:
                assert set(parameters) <= self.approved, "record/fact queued before source approval"
        return [self.execute(sql, parameters) for sql, parameters in requests]


def install_facts(db, group_id, *, malformed=False):
    if not is_postgres(db):
        for statement in FACT_SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
    db.execute(
        "INSERT INTO metadata VALUES ('automatic_fact_schema','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    fact = dict(
        key="drink",
        value="coffee",
        keywords=["morning"],
        entity=None,
        valid_from=None,
        valid_to=None,
        state="current",
    )
    scope = db.execute("SELECT scope FROM groups WHERE id=?", (group_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO automatic_facts VALUES (?,?,?,?,?,?,NULL)",
        (
            group_id,
            scope,
            "drink",
            "coffee",
            "2026-01-01T00:00:00Z",
            "bad-json" if malformed else canonical(fact),
        ),
    )


def use_batch_authority(h, db):
    # Exercise the actual current/current_many rules, without pretending this
    # local branch test performs the remote synchronization barrier.
    db.execute("CREATE TABLE source_admissions(key TEXT PRIMARY KEY,verified INTEGER,payload TEXT)")
    db.execute("CREATE TABLE suppression(source_key TEXT PRIMARY KEY)")
    db.execute("INSERT INTO source_admissions SELECT key,1,NULL FROM sources")
    h.service.source_authority = object.__new__(SourceAuthority)


def observe_authority(monkeypatch, service, remote):
    authority = service.source_authority
    if hasattr(authority, "current_many"):
        current_many = authority.current_many

        def current(db, groups):
            result = current_many(db, groups)
            if db is remote:
                remote.approved.update(result)
            return result

        monkeypatch.setattr(authority, "current_many", current)
    else:
        group_current = service._group_current

        def current(db, group_id, **options):
            result = group_current(db, group_id, **options)
            if db is remote and result:
                remote.approved.add(group_id)
            return result

        monkeypatch.setattr(service, "_group_current", current)


def original_exact(service, db, scope, kind, value="coffee"):
    groups = db.execute(
        "SELECT * FROM groups WHERE scope=? AND state='active' AND category IN (?) "
        f"AND {kind}_key=? ORDER BY id",
        (canonical(scope), "evidence", value),
    ).fetchall()
    eligible = eligible_groups(service, db, groups, scope)
    return eligible, snapshot_for_groups(db, eligible)


def fitted(h, db, capture, query):
    groups, facts = capture
    return h.service._fit_groups(
        filter_current(db, groups, query, h.clock, fact_snapshot=facts),
        dict(tokens=100000, bytes=100000),
    )


@pytest.mark.parametrize("kind", ["field", "item"])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("batch_authority", [False, True])
def test_exact_reads_match_previous_path_without_replacing_either_authority(
    h, monkeypatch, kind, shared, batch_authority
):
    scope = h.group if shared else h.private
    result, _, _ = h.seed(
        [
            h.draft(
                scope=scope,
                item_key="coffee",
                units=[h.unit(), h.unit(statement="second condition")],
            )
        ]
    )
    with h.store.transaction() as db:
        install_facts(db, result["group_ids"][0])
        if batch_authority:
            use_batch_authority(h, db)
        old, remote = RelationalPostgres(db), ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)
        expected = fitted(h, old, original_exact(h.service, old, scope, kind), kind + ":coffee")
        actual = exact_groups(h.service, remote, scope, ["evidence"], kind, "coffee")
        assert fitted(h, remote, actual, kind + ":coffee") == expected
        assert len(old.statements) == len(remote.statements) + 1
        assert [len(batch) for batch in remote.batches] == [2, 2 if shared else 1]
        assert len(actual[0][0][2]) == 2 and actual[1].installed


@pytest.mark.parametrize(
    "damage", ["withdrawn", "revision", "epoch", "no-lineage", "unverified", "suppressed"]
)
def test_non_current_sources_never_queue_record_fact_or_projection_bodies(h, monkeypatch, damage):
    result, _, _ = h.seed()
    with h.store.transaction() as db:
        install_facts(db, result["group_ids"][0])
        use_batch_authority(h, db)
        if damage == "withdrawn":
            db.execute("UPDATE sources SET state='withdrawn'")
        elif damage in {"revision", "epoch"}:
            db.execute(f"UPDATE sources SET {damage}={damage}+1")
        elif damage == "no-lineage":
            db.execute("DELETE FROM lineage")
        elif damage == "unverified":
            db.execute("UPDATE source_admissions SET verified=0")
        else:
            db.execute("INSERT INTO suppression SELECT key FROM sources")
        db.execute("UPDATE records SET payload='bad-json'")
        remote = ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)
        groups, facts = exact_groups(h.service, remote, h.private, ["evidence"], "field", "coffee")
        assert groups == [] and facts.rows == {}
        assert remote.batches == []
        assert not any(
            name in sql
            for sql in remote.statements
            for name in ["FROM records", "FROM projections", "FROM automatic_facts"]
        )


@pytest.mark.parametrize(
    "damage",
    [
        "members",
        "record-version",
        "semantic-group",
        "subject-person",
        "inactive-record",
        "missing-record",
        "projection-missing",
        "projection-withdrawn",
        "projection-version",
        "projection-owner",
    ],
)
def test_incomplete_groups_and_projections_are_omitted_before_bad_fact_json(h, monkeypatch, damage):
    result, _, _ = h.seed(
        [h.draft(scope=h.group, units=[h.unit(), h.unit(statement="second complete member")])]
    )
    with h.store.transaction() as db:
        install_facts(db, result["group_ids"][0], malformed=True)
        record = db.execute("SELECT * FROM records ORDER BY id LIMIT 1").fetchone()
        if damage == "members":
            db.execute("UPDATE groups SET members='[]'")
        elif damage in {"record-version", "semantic-group", "subject-person"}:
            key, value = {
                "record-version": ("record_version", 99),
                "semantic-group": ("semantic_group_id", "unrelated-group"),
                "subject-person": ("subject_person_id", "unrelated-person"),
            }[damage]
            unit = dict(json.loads(record["payload"]), **{key: value})
            db.execute("UPDATE records SET payload=? WHERE id=?", (canonical(unit), record["id"]))
        elif damage == "inactive-record":
            db.execute("UPDATE records SET state='invalidated' WHERE id=?", (record["id"],))
        elif damage == "missing-record":
            db.execute("DELETE FROM projections WHERE record_id=?", (record["id"],))
            db.execute("DELETE FROM records WHERE id=?", (record["id"],))
        else:
            ref = db.execute(
                "SELECT ref FROM projections WHERE record_id=?", (record["id"],)
            ).fetchone()[0]
            if damage == "projection-missing":
                db.execute("DELETE FROM projections WHERE ref=?", (ref,))
            elif damage == "projection-owner":
                other = next(
                    identity for identity in result["record_ids"] if identity != record["id"]
                )
                db.execute("UPDATE projections SET record_id=? WHERE ref=?", (other, ref))
            else:
                column, value = (
                    ("version", 99) if damage == "projection-version" else ("state", "withdrawn")
                )
                db.execute(f"UPDATE projections SET {column}=? WHERE ref=?", (value, ref))
        expected = original_exact(h.service, RelationalPostgres(db), h.group, "field")
        remote = ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)
        actual = exact_groups(h.service, remote, h.group, ["evidence"], "field", "coffee")
        assert expected[0] == actual[0] == []
        assert actual[1].installed and actual[1].rows == actual[1].decoded == {}


@pytest.mark.parametrize("marker", [None, "0", "2"])
def test_disabled_fact_schema_never_touches_the_missing_fact_table(h, monkeypatch, marker):
    h.seed()
    with h.store.transaction() as db:
        if marker is not None:
            db.execute("INSERT INTO metadata VALUES ('automatic_fact_schema',?)", (marker,))
        remote = ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)
        groups, snapshot = exact_groups(
            h.service, remote, h.private, ["evidence"], "field", "coffee"
        )
        assert groups and not snapshot.installed
        assert not any("automatic_facts" in sql for sql in remote.statements)
        assert len(remote.batches) == 1


def test_next_capture_rereads_fact_marker_payload_and_projection_without_decoding_omitted_rows(
    h, monkeypatch
):
    result, _, _ = h.seed([h.draft(scope=h.group)])
    with h.store.transaction() as db:
        install_facts(db, result["group_ids"][0])
        remote = ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)

        def capture():
            remote.approved.clear()
            return exact_groups(h.service, remote, h.group, ["evidence"], "field", "coffee")

        initial = capture()
        assert fitted(h, remote, initial, "field:coffee")["selected_units"]
        fact = dict(initial[1].decoded[result["group_ids"][0]], valid_from="2030-01-01T00:00:00Z")
        db.execute("UPDATE automatic_facts SET payload=?", (canonical(fact),))
        assert not fitted(h, remote, capture(), "field:coffee")["selected_units"]
        db.execute("UPDATE projections SET state='withdrawn'")
        db.execute("UPDATE automatic_facts SET payload='bad-json'")
        assert capture()[0] == []
        db.execute("UPDATE projections SET state='active'")
        db.execute("UPDATE metadata SET value='0' WHERE key='automatic_fact_schema'")
        db.execute("DROP TABLE automatic_facts")
        groups, snapshot = capture()
        assert groups and not snapshot.installed


@pytest.mark.parametrize("count", [GROUP_READ_BATCH, GROUP_READ_BATCH + 1])
def test_exact_metadata_caps_lineage_fanout_and_oversized_fields_keep_all_groups(
    h, monkeypatch, count
):
    seed_groups(h, count)
    with h.store.transaction() as db:
        db.execute("UPDATE groups SET field_key='wide-field' WHERE id LIKE 'batch-group-%'")
        remote = ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)
        groups, _ = exact_groups(h.service, remote, h.private, ["evidence"], "field", "wide-field")
        assert [group[0]["id"] for group in groups] == [
            f"batch-group-{index:04}" for index in range(count)
        ]
        metadata = remote.metadata[0]
        assert len(metadata) == count
        assert {row["exact_count"] for row in metadata} == {count}
        if count > GROUP_READ_BATCH:
            assert all(row["exact_group_id"] is None for row in metadata)
            assert (
                len(
                    [
                        sql
                        for sql in remote.statements
                        if "FROM lineage l JOIN sources" in sql and not sql.startswith("WITH")
                    ]
                )
                == 2
            )
            assert (
                len([sql for sql in remote.statements if "FROM records WHERE group_id IN" in sql])
                == 2
            )
            assert remote.batches == []


def test_select_keeps_zero_budget_and_known_version_short_circuits_and_sqlite_lazy_reads(
    h, monkeypatch
):
    h.seed()
    context = h.config["origins"]["origin-private"]
    with h.store.transaction() as db:
        remote = ExactReads(db)
        observe_authority(monkeypatch, h.service, remote)

        @contextmanager
        def operation(**_options):
            yield remote

        monkeypatch.setattr(h.service, "operation", operation)
        assert h.service.select(h.selection(query="field:coffee", budget=0), context)[
            "omissions"
        ] == ["budget"]
        with pytest.raises(Fault, match="scope_changed"):
            h.service.select(h.selection(query="field:coffee", known=-1), context)
        assert not remote.metadata and not remote.batches
        assert not any("FROM records" in sql or "FROM groups" in sql for sql in remote.statements)

        @contextmanager
        def sqlite_operation(**_options):
            yield db

        monkeypatch.setattr(h.service, "operation", sqlite_operation)
        statements = []
        db.set_trace_callback(statements.append)
        assert h.service.select(h.selection(query="field:coffee"), context)["selected_units"]
        db.set_trace_callback(None)
        assert any("FROM records WHERE group_id=" in sql for sql in statements)
        assert not any(
            "WITH candidates" in sql or "FROM records WHERE group_id IN" in sql
            for sql in statements
        )


@pytest.mark.parametrize("shared", [False, True])
def test_native_pg_exact_complete_groups_and_bad_projection_fact_json(h, shared):
    scope = h.group if shared else h.private
    result, _, _ = h.seed(
        [h.draft(scope=scope, units=[h.unit(), h.unit(statement="second member")])]
    )
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        with store.transaction() as db:
            install_facts(db, result["group_ids"][0])
            expected = fitted(h, db, original_exact(h.service, db, scope, "field"), "field:coffee")
            actual = exact_groups(h.service, db, scope, ["evidence"], "field", "coffee")
            assert fitted(h, db, actual, "field:coffee") == expected
            if shared:
                db.execute("UPDATE projections SET state='withdrawn'")
                db.execute("UPDATE automatic_facts SET payload='bad-json'")
                assert exact_groups(h.service, db, scope, ["evidence"], "field", "coffee")[0] == []


def test_native_pg_oversized_exact_does_not_execute_the_lineage_subplan(h):
    seed_groups(h, GROUP_READ_BATCH + 1)
    with h.store.transaction() as db:
        db.execute("UPDATE groups SET field_key='wide-field' WHERE id LIKE 'batch-group-%'")
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        with store.transaction() as db:
            native, metadata = db.native, []

            def observe(sql, parameters=(), **options):
                if sql.startswith("WITH candidates AS MATERIALIZED"):
                    metadata.append((sql, parameters))
                return native(sql, parameters, **options)

            db.native = observe
            groups, _ = exact_groups(h.service, db, h.private, ["evidence"], "field", "wide-field")
            assert len(groups) == GROUP_READ_BATCH + 1
            statement, parameters = metadata[0]
            plan = native("EXPLAIN (ANALYZE,FORMAT JSON) " + statement, parameters).fetchone()[0]
            if isinstance(plan, str):
                plan = json.loads(plan)

            def nodes(value):
                yield value
                for child in value.get("Plans", []):
                    yield from nodes(child)

            reads = [
                node
                for node in nodes(plan[0]["Plan"])
                if node.get("Relation Name") in {"lineage", "sources"}
            ]
            assert reads and all(node["Actual Loops"] == 0 for node in reads)


def test_native_pg_exact_runs_through_real_source_authority(pg_sync):
    result, _, _ = pg_sync.seed()
    with pg_sync.store.transaction() as db:
        db.execute(
            "UPDATE groups SET field_key='native-exact' WHERE id=?", (result["group_ids"][0],)
        )
    body = pg_sync.selection()
    body["query_text"] = "field:native-exact"
    response = pg_sync.post("memory/select", body)
    assert response.status_code == 200, response.text
    assert {unit["record_id"] for unit in response.json()["selected_units"]} == set(
        result["record_ids"]
    )
