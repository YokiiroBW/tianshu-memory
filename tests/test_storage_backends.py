"""Real isolated PostgreSQL plus file-backed recovery; never a production default URL."""

import copy
import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from source_sync_harness import SyncHarness
from test_auth_https import certificates as certificates
from test_knowledge import imported, query
from test_knowledge import knowledge as knowledge
from test_knowledge_content import (
    acquire_upload,
    image,
    post,
    principal,
)
from test_knowledge_content import content as content
from test_knowledge_content import (
    test_actual_http_article_version_original_permissions_and_withdrawal as article_case,
)
from test_knowledge_directories import catalog as catalog
from test_knowledge_directories import (
    test_apply_imports_the_previewed_content_and_a_rescan_is_unchanged as directory_case,
)
from test_knowledge_references import (
    test_collection_check_keeps_early_failure_before_later_malformed_block as reference_check_case,
)
from test_knowledge_references import (
    test_collection_file_change_between_hash_reads_is_still_rejected as reference_file_case,
)
from test_knowledge_references import (
    test_collection_matches_scalar_order_and_body_admission as reference_admission_case,
)
from test_knowledge_references import (
    test_collection_repeats_source_checks_and_reloads_rows_in_each_phase as reference_phase_case,
)
from test_knowledge_references import (
    test_collection_state_recovery_check_budget_and_project_isolation as reference_workflow_case,
)
from test_knowledge_references import (
    test_collection_url_allowlist_remains_a_body_gate as reference_url_case,
)
from test_knowledge_references import (
    test_corrupt_early_evidence_keeps_original_failure_before_later_capture as reference_corrupt_case,
)
from test_lessons import lessons as lessons
from test_lessons import (
    test_global_retrieval_never_leaks_unauthorized_projects as experience_case,
)
from test_lessons import (
    test_record_query_and_check_lesson_with_current_evidence as lesson_case,
)
from test_memory_context import continuity as continuity
from test_memory_context import (
    test_ack_loss_replays_and_receipt_batch_resume_without_duplicate_writes as receipt_case,
)
from test_memory_context import (
    test_natural_recall_all_categories_preserves_whole_group_and_budget as context_case,
)
from test_relationships import relationship as relationship
from test_relationships import (
    test_freeze_blocks_both_directions_and_decay_unfreeze_never_catches_up as freeze_case,
)
from test_relationships import (
    test_positive_daily_budget_source_dedup_and_negative_requires_attestation as budget_case,
)
from test_research_notes import notes as notes
from test_research_notes import (
    test_two_sources_to_note_revision_query_and_withdrawal as note_case,
)
from test_revisions_events import (
    test_revision_invalidates_whole_group_projection_old_index_cache_and_job as revision_case,
)
from test_source_sync import (
    test_configured_https_consume_candidate_select_probe_and_background_check as source_case,
)
from test_source_sync import (
    test_suppression_is_actor_local_and_correction_never_publishes_new_value as suppression_case,
)
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory import source_recovery
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge_catalog_migration import inspect
from tianshu_memory.knowledge_files import FORMAT, read, reference
from tianshu_memory.role_grants import RoleGrants
from tianshu_memory.store import Store


@pytest.mark.parametrize("null_id", [False, True])
def test_native_postgres_named_alias_updates_existing_location(pg_sync, null_id):
    from test_fact_entities import named_alias_location_case

    named_alias_location_case(pg_sync, null_id=null_id)


@pytest.mark.parametrize(
    "reason",
    [
        "ambiguous",
        "wrong_id",
        "foreign_scope",
        "superseded",
        "expired",
        "withdrawn",
        "conditions",
        "missing_quote",
        "incomplete_group",
    ],
)
def test_native_postgres_named_alias_obeys_current_source_identity(pg_sync, reason):
    from test_fact_entities import named_alias_rejection_case

    named_alias_rejection_case(pg_sync, reason)


def pg_options():
    url = os.environ.get("TIANSHU_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TIANSHU_TEST_POSTGRES_URL to an explicit isolated PostgreSQL database")
    return {"postgresql_url": url, "namespace": "ts232" + uuid4().hex}


@contextmanager
def postgres_store(path, *, settings=None, service="memory"):
    import psycopg
    from psycopg import sql

    settings = dict(settings or {}, database_path=str(Path(path).resolve()))
    if "storage_backends" not in settings:
        settings["storage_backends"] = pg_options()
    store = Store.from_config(settings, service=service)
    assert store.backend == "postgresql", store.runtime.status()
    schemas = [store.database.schema]
    if store.role_database is not None:
        schemas.append(store.role_database.schema)
    try:
        yield store, settings
    finally:
        store.close()
        # These names belong only to this test's random namespace and synthetic database.
        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            for name in schemas:
                db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_postgres_recall_consume_suppression_and_replay(h, kind):
    h.store.migrate_profiles(h.directory / "profiles.bak")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "sources.bak", h.contracts)
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        revision_case(h, kind)
        with store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1


@pytest.fixture(params=["self_private", "group"])
def pg_sync(tmp_path, source_contracts, source_examples, certificates, monkeypatch, request):
    harness = SyncHarness(tmp_path, source_contracts, source_examples, certificates, request.param)
    harness.store.close()
    with postgres_store(harness.store.path, settings=harness.config) as (_, settings):
        harness.config["storage_backends"] = settings["storage_backends"]
        harness.save()
        with harness.owners(), harness.runtime(monkeypatch):
            harness.store = harness.service.store
            yield harness


def test_postgres_https_authority_and_consumption(pg_sync):
    source_case(pg_sync)


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_postgres_actor_local_suppression(pg_sync, kind):
    suppression_case(pg_sync, kind)


@pytest.mark.parametrize(
    "case", [receipt_case, context_case], ids=["batch-replay", "natural-recall"]
)
def test_postgres_context_receipts_and_full_groups(continuity, case):
    h = continuity
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        case(h)


def test_postgres_explicit_schema_chain_uses_current_backend_backups(h):
    from tianshu_memory.knowledge_content_migration import migrate as migrate_content
    from tianshu_memory.knowledge_migration import migrate

    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.contracts.load_sources()
        store.migrate_profiles(h.directory / "pg-profile.backup")
        store.migrate_sources(h.directory / "pg-sources.backup", h.contracts)
        with store.transaction() as db:
            db.execute("UPDATE metadata SET value='2147483647' WHERE key='source_revision'")
        store.migrate_users(h.directory / "pg-users.backup")
        store.migrate_observations(h.directory / "pg-observations.backup")
        store.migrate_facts(h.directory / "pg-facts.backup")
        store.migrate_context(h.directory / "pg-context.backup")
        store.migrate_qq_aliases(h.directory / "pg-qq.backup")
        store.migrate_relationships(h.directory / "pg-relationships.backup", clock=h.clock)
        migrate(store, h.directory / "pg-knowledge.backup")
        migrate_content(store, h.directory / "pg-content.backup")
        with store.transaction() as db:
            assert dict(db.execute("SELECT key,value FROM metadata"))["schema"] == "3"
            assert inspect(db) == []
        for path in h.directory.glob("pg-*.backup"):
            assert json.loads(path.read_bytes())["format"] == "tianshu-memory-postgresql-backup/v1"


@pytest.mark.parametrize(
    "case", [freeze_case, budget_case], ids=["freeze-decay", "budget-idempotency"]
)
def test_postgres_relationship_authority(relationship, case):
    h, _, _ = relationship
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        case(relationship)


def test_postgres_knowledge_complete_groups_and_live_file_authority(knowledge):
    run, root, old, path, config = knowledge
    first = imported(run)
    old.close()
    with postgres_store(old.path, service="knowledge") as (store, settings):
        config["storage_backends"] = settings["storage_backends"]
        path.write_text(canonical(config), encoding="utf-8")
        found = query(run)
        assert first["hash"] == found["blocks"][0]["reference"]["hash"]
        assert "Do not resend" in found["blocks"][0]["text"]
        assert "garden" not in found["blocks"][0]["text"]
        assert imported(run, key="same-content", expected_version=1)["status"] == "unchanged"
        with store.transaction() as db:
            assert inspect(db) == []
            row = db.execute("SELECT raw,text FROM knowledge_versions").fetchone()
            assert reference(row["raw"]) and reference(row["text"])
            assert read(db, row["raw"]) == (root / "design.md").read_bytes()
            assert "text_file" in json.loads(
                db.execute("SELECT payload FROM knowledge_blocks LIMIT 1").fetchone()[0]
            )
        (root / "design.md").write_text("The registered file changed.", encoding="utf-8")
        assert not query(run)["blocks"]
        denied = copy.deepcopy(config)
        denied["knowledge"]["clients"]["writer"]["projects"] = []
        path.write_text(canonical(denied), encoding="utf-8")
        with pytest.raises(Fault):
            query(run)


@pytest.fixture
def pg_knowledge_references(knowledge):
    run, root, old, path, config = knowledge
    old.close()
    with postgres_store(old.path, settings=config, service="knowledge") as (store, settings):
        config["storage_backends"] = settings["storage_backends"]
        path.write_text(canonical(config), encoding="utf-8")
        yield run, root, store, path, config


@pytest.mark.parametrize(
    "case", ["valid", "missing_document", "version", "hash", "missing_block", "withdrawn", "file"]
)
def test_postgres_knowledge_reference_collection_admission(pg_knowledge_references, case):
    reference_admission_case(pg_knowledge_references, case)


@pytest.mark.parametrize("corruption", ["body", "provenance", "file_ref"])
@pytest.mark.parametrize("later", ["valid", "malformed", "nul", "surrogate"])
def test_postgres_knowledge_reference_collection_failure_order(
    pg_knowledge_references, corruption, later
):
    reference_corrupt_case(pg_knowledge_references, corruption, later)


@pytest.mark.parametrize(
    "case", [reference_phase_case, reference_workflow_case], ids=["phase", "state"]
)
def test_postgres_knowledge_reference_collection_phases_and_state(pg_knowledge_references, case):
    case(pg_knowledge_references)


@pytest.mark.parametrize("operation", ["write_state", "recover", "check"])
def test_postgres_knowledge_reference_collection_file_freshness(
    pg_knowledge_references, monkeypatch, operation
):
    reference_file_case(pg_knowledge_references, monkeypatch, operation)


def test_postgres_knowledge_reference_collection_url_allowlist(
    pg_knowledge_references, monkeypatch
):
    reference_url_case(pg_knowledge_references, monkeypatch)


@pytest.mark.parametrize("first", ["stale", "corrupt"])
def test_postgres_knowledge_reference_collection_malformed_package(pg_knowledge_references, first):
    reference_check_case(pg_knowledge_references, first)


@pytest.mark.parametrize("case", [lesson_case, experience_case], ids=["lessons", "experience"])
def test_postgres_knowledge_derived_books_authorization_and_search(lessons, case):
    lessons.store.close()
    with postgres_store(lessons.store.path, settings=lessons.config, service="knowledge") as (
        store,
        settings,
    ):
        lessons.store = store
        lessons.config["storage_backends"] = settings["storage_backends"]
        lessons.path.write_text(canonical(lessons.config), encoding="utf-8")
        case(lessons)


def test_postgres_research_note_revision_query_and_withdrawal(notes):
    notes.store.close()
    with postgres_store(notes.store.path, settings=notes.config, service="knowledge") as (
        store,
        settings,
    ):
        notes.store = store
        notes.config["storage_backends"] = settings["storage_backends"]
        notes.path.write_text(canonical(notes.config), encoding="utf-8")
        note_case(notes)


def test_postgres_directory_apply_rechecks_confirmed_files_and_versions(catalog):
    catalog.store.close()
    with postgres_store(catalog.store.path, settings=catalog.config, service="knowledge") as (
        store,
        settings,
    ):
        catalog.store = store
        catalog.config["storage_backends"] = settings["storage_backends"]
        catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
        directory_case(catalog)


def test_postgres_content_upload_original_grants_and_withdrawal(content):
    h = content
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        article_case(h)
        raw = image()
        acquired = acquire_upload(h, raw, "image/png")
        read_request = {
            "principal": principal(h),
            "content_ref": acquired["content_ref"],
            "range": {"unit": "bytes", "start": 0, "end": len(raw)},
            "budget_bytes": 100000,
        }
        value = post(h, "read", read_request)
        assert value["representations"][0]["source_sha256"] == hashlib.sha256(raw).hexdigest()
        with store.transaction() as db:
            stored = db.execute(
                "SELECT raw FROM knowledge_versions WHERE hash=?",
                (hashlib.sha256(raw).hexdigest(),),
            ).fetchone()[0]
            assert json.loads(stored)["format"] == FORMAT
            assert read(db, stored) == raw
        file_path = store.files.root / reference(stored)["relative_path"]
        file_path.write_bytes(b"corrupted synthetic file")
        response = h.client.post(
            "/internal/v1/knowledge/content/read",
            json=read_request,
            headers={"Authorization": "Bearer test-only-companion-secret"},
        )
        assert response.status_code == 503


def test_postgres_role_sidecar_cas_restart_and_no_sqlite_fallback(tmp_path):
    import psycopg
    from psycopg import sql
    from tianshu_storage import StorageError

    side = tmp_path / "roles.sqlite"
    initial = RoleGrants(side)
    request = dict(
        request_id="role-register",
        actor_id="actor:storage",
        expected_version=0,
        enabled=True,
        legacy=False,
    )
    assert initial.apply(request, [])["enabled"]
    with postgres_store(
        tmp_path / "memory.sqlite", settings={"role_grants_database_path": str(side)}
    ) as (store, settings):
        roles = RoleGrants(side, database=store.role_database)
        assert roles.status("actor:storage") == initial.status("actor:storage")
        assert roles.apply(request, []) == initial.apply(request, [])
        with pytest.raises(Fault, match="version_conflict"):
            roles.apply(dict(request, request_id="stale"), [])
        roles.apply(dict(request, request_id="disable", expected_version=1, enabled=False), [])
        store.close()
        restarted = Store.from_config(settings)
        try:
            assert (
                RoleGrants(side, database=restarted.role_database).status("actor:storage")[
                    "enabled"
                ]
                is False
            )
        finally:
            restarted.close()
        with pytest.raises(StorageError):
            Store.from_config(
                dict(
                    settings,
                    storage_backends={"namespace": settings["storage_backends"]["namespace"]},
                )
            )
        assert initial.status("actor:storage")["enabled"] is True
        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            db.execute(
                sql.SQL("DROP TABLE {}.grants").format(sql.Identifier(store.role_database.schema))
            )
        with pytest.raises(StorageError):
            Store.from_config(settings)
        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            assert (
                db.execute(
                    "SELECT to_regclass(%s)", (store.role_database.schema + ".grants",)
                ).fetchone()[0]
                is None
            )


def test_postgres_writer_serializes_guard_and_refuses_an_old_restore(tmp_path):
    with postgres_store(tmp_path / "memory.sqlite") as (store, settings):
        entered, release, second = threading.Event(), threading.Event(), threading.Event()

        def first():
            with store.transaction() as db:
                db.execute("INSERT INTO people VALUES ('synthetic-first')")
                entered.set()
                assert release.wait(5)

        def next_writer():
            assert entered.wait(5)
            with store.transaction() as db:
                second.set()
                db.execute("INSERT INTO people VALUES ('synthetic-second')")

        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(first), pool.submit(next_writer)
            assert entered.wait(5)
            assert not second.wait(0.1)
            release.set()
            a.result(5)
            b.result(5)
        guard = json.loads(store.recovery_path.read_text())
        assert guard["revision"] == 2
        import psycopg
        from psycopg import sql

        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            db.execute(
                sql.SQL("UPDATE {}.metadata SET value='0' WHERE key='source_revision'").format(
                    sql.Identifier(store.database.schema)
                )
            )
        with pytest.raises(Fault, match="dependency_unavailable"):
            with store.transaction():
                pytest.fail("Old restored revision must never become ready")
        assert json.loads(store.recovery_path.read_text()) == guard
        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            db.execute(
                sql.SQL("UPDATE {}.metadata SET value='2' WHERE key='schema'").format(
                    sql.Identifier(store.database.schema)
                )
            )
        with pytest.raises(Fault, match="dependency_unavailable"):
            with store.transaction():
                pytest.fail("Restoring a pre-guard schema must not bypass the checkpoint")
        assert json.loads(store.recovery_path.read_text()) == guard


def test_postgres_unassembled_probe_reads_current_backend_and_never_stale_sqlite(tmp_path):
    from tianshu_memory.diagnostics import CHAT_SERVICE, Diagnostics
    from tianshu_memory.runtime_probes import ProbeConfig, assess

    with postgres_store(tmp_path / "probe.sqlite") as (store, settings):
        # A retained/replaced SQLite copy must never answer the selected PG probe.
        with sqlite3.connect(store.path) as db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO metadata VALUES ('schema','1')")
        config = dict(settings, mode="source_sync")
        path = tmp_path / "probe.json"
        path.write_text(canonical(config), encoding="utf-8")
        diagnostics = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path / "logs")})
        probe = ProbeConfig(
            service=CHAT_SERVICE,
            diagnostics=diagnostics,
            config_path=path,
            contract_path=tmp_path / "absent-contract",
        )
        files = (Path(store.path), store.recovery_path, store.runtime.state_path)
        snapshot = {p: p.read_bytes() for p in files}
        checks = assess(probe, diagnostics)
        assert checks["database"].state == checks["guard"].state == "ok"
        config["storage_backends"] = {"namespace": settings["storage_backends"]["namespace"]}
        path.write_text(canonical(config), encoding="utf-8")
        checks = assess(probe, diagnostics)
        assert checks["database"].state == checks["guard"].state == "failed"
        assert snapshot == {p: p.read_bytes() for p in files}


@pytest.mark.parametrize("table", ["metadata", "records"])
def test_postgres_ready_main_never_recreates_missing_authority(tmp_path, table):
    import psycopg
    from psycopg import sql

    with postgres_store(tmp_path / "authority.sqlite") as (store, settings):
        guard = store.recovery_path.read_bytes()
        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            db.execute(
                sql.SQL("DROP TABLE {}.{} CASCADE").format(
                    sql.Identifier(store.database.schema), sql.Identifier(table)
                )
            )
        if table == "metadata":
            with pytest.raises(sqlite3.Error):
                with store.transaction():
                    pytest.fail("A live PG owner must reject missing authority metadata")
        store.close()
        with pytest.raises(ValueError, match="authority.*missing"):
            Store.from_config(settings)
        assert store.recovery_path.read_bytes() == guard
        with psycopg.connect(settings["storage_backends"]["postgresql_url"], autocommit=True) as db:
            assert (
                db.execute(
                    "SELECT to_regclass(%s)", (store.database.schema + "." + table,)
                ).fetchone()[0]
                is None
            )


def test_postgres_shared_memory_knowledge_path_refuses_second_authority(tmp_path):
    with postgres_store(tmp_path / "shared.sqlite") as (store, settings):
        marker = store.runtime.state_directory / ".tianshu-storage-knowledge.json"
        assert not marker.exists()
        with pytest.raises(ValueError, match="another PostgreSQL authority"):
            Store.from_config(settings, service="knowledge")
        assert not marker.exists()
        with store.transaction() as db:
            assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == "3"


def test_postgres_interrupted_first_guard_commit_resumes_without_rewriting_guard(
    tmp_path, monkeypatch
):
    settings = {"database_path": str(tmp_path / "fresh.sqlite"), "storage_backends": pg_options()}
    persist = source_recovery.persist

    def interrupted(*args, **kwargs):
        persist(*args, **kwargs)
        raise RuntimeError("synthetic first migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(source_recovery, "persist", interrupted)
        with pytest.raises(RuntimeError, match="synthetic first migration interruption"):
            Store.from_config(settings)
    guard_path = Path(settings["database_path"] + ".source-guard.json")
    guard = guard_path.read_bytes()
    with postgres_store(settings["database_path"], settings=settings) as (store, _):
        assert store.recovery_path.read_bytes() == guard
        with store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0


def test_postgres_import_preserves_existing_records_outbox_and_receipts(h):
    h.store.migrate_profiles(h.directory / "profiles.bak")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "sources.bak", h.contracts)
    seeded, job, event = h.seed()
    before = h.select()
    with h.store.transaction() as db:
        counts = {
            t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "records",
                "history",
                "lineage",
                "outbox",
                "write_ledger",
                "source_writes",
                "inbox",
                "turn_inputs",
            )
        }
    guard = h.store.recovery_path.read_bytes()
    h.store.close()
    with postgres_store(h.store.path) as (store, _):
        h.store = h.service.store = store
        assert store.recovery_path.read_bytes() == guard
        assert h.select()["selected_units"] == before["selected_units"]
        result = h.post("memory/turn-commits", event).json()
        assert result["state"] == "duplicate" and result["candidate_job_ref"] == job
        assert h.workflow.commit_candidate(job, [h.draft()])["record_ids"] == seeded["record_ids"]
        with store.transaction() as db:
            assert counts == {
                t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts
            }


def test_file_migration_is_backed_up_preserves_coordinates_and_fails_closed(knowledge):
    run, root, old, _, _ = knowledge
    imported(run)
    with old.transaction() as db:
        raw = read(db, db.execute("SELECT raw FROM knowledge_versions").fetchone()[0])
        text = read(db, db.execute("SELECT text FROM knowledge_versions").fetchone()[0], text=True)
        blocks = [
            (r[0], json.loads(r[1])) for r in db.execute("SELECT id,payload FROM knowledge_blocks")
        ]
        # Reconstruct the actual pre-file schema state with synthetic bytes only.
        db.execute("DELETE FROM metadata WHERE key='knowledge_files_schema'")
        db.execute("UPDATE knowledge_versions SET raw=?,text=?", (raw, text))
        for identity, unit in blocks:
            unit["text"] = read(db, unit.pop("text_file"), text=True)
            db.execute(
                "UPDATE knowledge_blocks SET payload=? WHERE id=?", (canonical(unit), identity)
            )
    old.close()
    upgraded = Store(old.path)
    try:
        backups = list(
            Path(old.path).parent.glob(Path(old.path).name + ".pre-knowledge-files-*.bak")
        )
        assert len(backups) == 1
        assert Path(str(backups[0]) + ".source-guard.json").is_file()
        assert query(run)["blocks"][0]["spans"] == [[1, 1], [2, 3]]
        with upgraded.transaction() as db:
            stored = db.execute("SELECT raw,text FROM knowledge_versions").fetchone()
            assert read(db, stored["raw"]) == raw == (root / "design.md").read_bytes()
            assert read(db, stored["text"], text=True) == text
        guard = upgraded.recovery_path.read_bytes()
        with pytest.raises(RuntimeError):
            persist = source_recovery.persist

            def interrupted(*args, **kwargs):
                persist(*args, **kwargs)
                raise RuntimeError("synthetic interruption after file checkpoint")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(source_recovery, "persist", interrupted)
                with upgraded.transaction() as db:
                    db.execute("INSERT INTO people VALUES ('interrupted-synthetic')")
        assert upgraded.recovery_path.read_bytes() != guard
        with pytest.raises(Fault):
            with upgraded.transaction():
                pytest.fail("Interrupted guard must remain failed closed")
    finally:
        upgraded.close()
