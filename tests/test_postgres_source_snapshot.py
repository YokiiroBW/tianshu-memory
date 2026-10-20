"""Real PG checks of transaction-local source synchronization reads/no-op writes."""

from contextlib import closing
from copy import deepcopy

import pytest
from test_source_batches import retain_history
from test_source_batches import (
    test_receipt_alias_across_batches_is_rejected_without_partial_sync as receipt_alias_case,
)
from test_source_sync import (
    test_both_owner_rollbacks_or_restoration_generations_are_rejected as rollback_case,
)
from test_source_sync import (
    test_delayed_physical_change_is_valid_after_unrelated_scope_observed_new_global_head as delayed_case,
)
from test_source_sync import (
    test_local_revision_change_during_owner_reads_retries_from_new_m0 as revision_case,
)
from test_source_sync import (
    test_physical_negative_broadcasts_to_all_local_actor_lineage_in_one_transaction as broadcast_case,
)
from test_source_sync import (
    test_platform_cannot_change_grant_at_the_same_owner_head as equivocation_case,
)
from test_source_sync import (
    test_platform_denial_is_actor_local_and_invalidates_before_bad_event as denial_case,
)
from test_source_sync import (
    test_same_core_head_cannot_regrant_a_new_admission_after_physical_edit as regrant_case,
)
from test_storage_backends import certificates as certificates
from test_storage_backends import pg_sync as pg_sync
from test_storage_backends import postgres_store
from test_storage_backends import source_contracts as source_contracts
from test_storage_backends import source_examples as source_examples

from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.postgres_source_snapshot import ADMISSIONS_SQL


def test_stable_postgres_apply_reads_a_whole_batch_without_noop_mutations(pg_sync, monkeypatch):
    retain_history(pg_sync, 30)
    authority = pg_sync.service.source_authority
    original = authority._apply
    calls = []

    def apply(service, db, snapshot, access):
        statements = []
        native = db.native

        def observed(sql, parameters=(), **options):
            statements.append(sql)
            return native(sql, parameters, **options)

        db.native = observed
        try:
            result = original(service, db, snapshot, access)
            calls.append(statements)
            return result
        finally:
            db.native = native

    monkeypatch.setattr(authority, "_apply", apply)
    guard = pg_sync.store.recovery_path
    before_bytes, before_mtime = guard.read_bytes(), guard.stat().st_mtime_ns
    for _ in range(2):
        pg_sync.select(budget=0)
    assert len(calls) == 2
    for statements in calls:
        # Fixed authority/schema probes may accompany the seven batched reads;
        # thirty sources must not turn these into per-source round trips.
        assert len(statements) <= 16
        assert not any(
            sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
        )
    assert guard.read_bytes() == before_bytes
    assert guard.stat().st_mtime_ns == before_mtime


@pytest.mark.parametrize("change", ["edit", "retract", "reality"])
def test_postgres_prefetch_preserves_cross_actor_negative_broadcast_and_epoch(
    pg_sync, change, monkeypatch
):
    authority = pg_sync.service.source_authority
    original = authority._apply
    first_negative_epochs = []

    def apply(service, db, snapshot, access):
        result = original(service, db, snapshot, access)
        if snapshot["head"]["sequence"] == 11 and not first_negative_epochs:
            first_negative_epochs.extend(
                row[0] for row in db.execute("SELECT epoch FROM sources ORDER BY key")
            )
        return result

    monkeypatch.setattr(authority, "_apply", apply)
    broadcast_case(pg_sync, change)
    assert first_negative_epochs == [2, 2]


@pytest.mark.parametrize(
    "case",
    [denial_case, equivocation_case, regrant_case, delayed_case, revision_case, receipt_alias_case],
    ids=[
        "denial-before-bad-event",
        "same-platform-head",
        "same-core-head",
        "delayed-edit",
        "local-cas",
        "cross-batch-receipt",
    ],
)
def test_postgres_prefetch_keeps_authority_failures_and_retry_semantics(pg_sync, case):
    case(pg_sync)


@pytest.mark.parametrize("owner", ["core", "platform"])
@pytest.mark.parametrize("change", ["rollback", "generation"])
def test_postgres_prefetch_rejects_owner_rollback_and_generation(pg_sync, owner, change):
    rollback_case(pg_sync, owner, change)


def test_postgres_receipt_conflict_in_an_unrequested_physical_is_rejected(pg_sync):
    pg_sync.seed()
    unrelated = deepcopy(pg_sync.admissions[0])
    unrelated["selector"]["key"]["message_id"] = "synthetic-unrequested-message"
    unrelated["selector"]["actor_id"] = "actor:unrequested"
    unrelated["scope"].update(actor_id="actor:unrequested", conversation_id="unrequested-scope")
    unrelated["source"]["message_key"]["message_id"] = "synthetic-unrequested-message"
    key = fingerprint(unrelated["selector"])
    physical_key = fingerprint(unrelated["selector"]["key"])
    with pg_sync.store.transaction() as db:
        db.execute("INSERT INTO physical_sources VALUES (?,NULL,1,'active')", (physical_key,))
        db.execute(
            "INSERT INTO sources VALUES (?,1,1,'withdrawn',?,'real',?)",
            (key, canonical(unrelated["scope"]), canonical(unrelated["source"])),
        )
        db.execute(
            "INSERT INTO source_admissions VALUES (?,?,?,?,?,1)",
            (
                key,
                physical_key,
                "actor:unrequested",
                canonical(unrelated["selector"]),
                canonical(unrelated),
            ),
        )
    guard = pg_sync.store.recovery_path.read_bytes()
    response = pg_sync.post("memory/select", pg_sync.selection(budget=0))
    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"
    assert pg_sync.store.recovery_path.read_bytes() == guard


def test_postgres_admission_branches_use_existing_indexes_and_keep_null_aliases(tmp_path):
    with postgres_store(tmp_path / "index.sqlite") as (store, _):
        with store.transaction() as db:
            db.native(
                "INSERT INTO physical_sources SELECT 'physical-'||i,NULL,1,'active' "
                "FROM generate_series(1,4000) i"
            )
            db.native(
                "INSERT INTO sources SELECT 'source-'||i,1,1,'active','scope','real','{}' "
                "FROM generate_series(1,4000) i"
            )
            db.native(
                "INSERT INTO source_admissions SELECT 'source-'||i,'physical-'||i,'actor',"
                "'selector-'||i,CASE WHEN i=21 THEN NULL ELSE "
                "jsonb_build_object('source',jsonb_build_object('receipt_id','receipt-'||i))::text "
                "END,1 FROM generate_series(1,4000) i"
            )
            db.native("ANALYZE source_admissions")
            parameters = (["physical-21"], ["source-22"], ["receipt-23"])
            assert {row["key"] for row in db.native(ADMISSIONS_SQL, parameters)} == {
                "source-21",
                "source-22",
                "source-23",
            }
            plan = db.native("EXPLAIN (FORMAT JSON) " + ADMISSIONS_SQL, parameters).fetchone()[0]
            indexes = set()

            def visit(node):
                if "Index Name" in node:
                    indexes.add(node["Index Name"])
                for child in node.get("Plans", []):
                    visit(child)

            visit(plan[0]["Plan"])
            assert {
                "admissions_physical",
                "source_admissions_pkey",
                "admissions_receipt",
            } <= indexes


@pytest.mark.parametrize(
    "damage", ["marker", "instance", "revision", "recovery", "missing-revision"]
)
def test_postgres_fresh_metadata_still_refuses_guard_or_marker_damage(tmp_path, damage):
    with postgres_store(tmp_path / "guard.sqlite") as (store, _):
        before = store.recovery_path.read_bytes()
        with closing(store.database.connect(isolation_level=None)) as db:
            if damage == "missing-revision":
                db.native("DELETE FROM metadata WHERE key='source_revision'")
            else:
                key, value = {
                    "marker": ("postgres_memory_schema", "0"),
                    "instance": ("source_instance", "unrelated"),
                    "revision": ("source_revision", "-1"),
                    "recovery": ("source_recovery", "pending"),
                }[damage]
                db.native("UPDATE metadata SET value=? WHERE key=?", (value, key))
        with pytest.raises(Fault, match="dependency_unavailable"):
            with store.transaction():
                pytest.fail("damaged authority became available")
        assert store.recovery_path.read_bytes() == before
