"""Actual selected checkpoints overlap remotely and serialize final authority work."""

import asyncio
import copy
import threading
from contextlib import contextmanager

import pytest
import test_context_bundle_checks as context_checks
from test_audit_runtime import until
from test_consolidation import prepare
from test_context_bundle_checks import inherited
from test_contextual_recall import contextual_query, experience
from test_memory_preparation import payload
from test_memory_raw_sources import check_request, original_turn, raw_check
from test_memory_read_cancellation import ObservedLock, client_for
from test_memory_v2 import post, request
from test_query_vector_boundary import indexed
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import persistent_retrieval
from tianshu_memory.app import create_app
from tianshu_memory.domain import Fault
from tianshu_memory.memory_context import read_bundle, read_v2


def empty(h):
    return contextual_query(h, "当前没有选中材料", limits=dict(facts=0, episodes=0))["bundle"]


def spawn(action):
    result = {}

    def run():
        try:
            result["value"] = action()
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def joined(thread, result):
    thread.join(8)
    assert not thread.is_alive(), "The original worker did not finish"
    if "error" in result:
        raise result["error"]
    return result["value"]


def pause_first_snapshot(h, monkeypatch):
    """Pause actual owner facts after a full real reply, before Memory consumes it."""
    entered, release = threading.Event(), threading.Event()
    original = h.service.source_authority.transport.facts
    chosen = []

    def facts(body):
        result = original(body)
        if body["mode"] == "snapshot" and not chosen:
            chosen.append(threading.get_ident())
            entered.set()
            assert release.wait(5), "A test failed to release its source owner reply"
        return result

    monkeypatch.setattr(h.service.source_authority.transport, "facts", facts)
    return entered, release, chosen


def test_two_real_selected_requests_overlap_owner_io_without_db_or_borrow_and_finish_serially(
    sync, monkeypatch
):
    prepare(sync)
    bundle = empty(sync)
    authority = sync.service.source_authority
    depth = threading.local()
    # The configured app owns a separate Store object for this same database.
    # Observe the actual source/business connections, not the fixture reader.
    business_store = sync.service.store
    original_tx = business_store.transaction
    original_facts, original_commit = authority.transport.facts, authority._commit_proof
    both_remote = threading.Barrier(2)
    remote_threads, transaction_threads, finishes = set(), [], []
    mutex = threading.Lock()

    @contextmanager
    def transaction():
        with original_tx() as db:
            thread = threading.get_ident()
            depth.value = getattr(depth, "value", 0) + 1
            transaction_threads.append((id(db), thread))
            try:
                yield db
            finally:
                assert thread == threading.get_ident()
                depth.value -= 1

    def facts(body):
        thread = threading.get_ident()
        assert not getattr(depth, "value", 0)
        assert authority._operation_order._owner != thread
        assert getattr(authority._bundled, "active", None) is None
        if body["mode"] == "snapshot":
            with mutex:
                first = thread not in remote_threads
                remote_threads.add(thread)
            if first:
                both_remote.wait(4)
        return original_facts(body)

    def commit(*args):
        assert authority._operation_order._owner == threading.get_ident()
        assert authority._operation_order._depth == 1
        result = original_commit(*args)
        finishes.append(threading.get_ident())
        return result

    original_selected = read_v2.verify_selected

    def selected(service, db, supplied, context):
        assert authority._operation_order._owner == threading.get_ident()
        assert authority._bundled.active["db"] is db
        assert authority._bundled.active["domains"] == set()
        with pytest.raises(Fault) as rejected:
            with service.operation(scope=sync.scope(), context=context):
                pytest.fail("A selected checkpoint lent an unproved full domain")
        assert rejected.value.code == "dependency_unavailable"
        return original_selected(service, db, supplied, context)

    monkeypatch.setattr(business_store, "transaction", transaction)
    monkeypatch.setattr(authority.transport, "facts", facts)
    monkeypatch.setattr(authority, "_commit_proof", commit)
    monkeypatch.setattr(read_v2, "verify_selected", selected)
    body = payload(sync, bundle)
    first = spawn(lambda: post(sync, "prepare", copy.deepcopy(body)))
    second = spawn(lambda: post(sync, "prepare", copy.deepcopy(body)))
    for thread, result in (first, second):
        response = joined(thread, result)
        assert response.status_code == 200 and response.json()["state"] == "valid", response.text
    assert len(remote_threads) == 2 and len(finishes) == 2
    assert set(finishes) == remote_threads
    assert remote_threads <= {thread for _, thread in transaction_threads}
    assert authority._operation_order._owner is None
    assert not authority._operation_order._waiters


def test_real_core_observation_alias_forget_during_remote_gap_cannot_revive_a_child(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("An actual observation/Core physical mapping")
    authority = sync.service.source_authority
    writers = []

    def during_remote(_monkeypatch, forget):
        entered, release, reader = pause_first_snapshot(sync, monkeypatch)

        def write():
            try:
                assert entered.wait(4)
                assert authority._operation_order._owner != reader[0]
                # The original public forget runs as a separate real HTTP
                # request after the reader captured its source-owner reply.
                forget()
            finally:
                release.set()

        writers.append(spawn(write))

    monkeypatch.setattr(context_checks, "before_checkpoint", during_remote)
    context_checks.test_child_episode_proves_both_real_aliases_and_public_forget_cannot_be_optional_revival(
        sync, monkeypatch, "core"
    )
    assert len(writers) == 1
    joined(*writers[0])


@pytest.mark.parametrize("component", ["primary", "optional", "required"])
def test_public_forget_during_unlocked_remote_proof_is_durable_and_never_replayed_as_positive(
    sync, monkeypatch, component
):
    prepare(sync)
    saved = experience(sync)
    selected = contextual_query(sync, "河边散步", limits=dict(facts=1, episodes=0))["bundle"]
    main = selected if component == "primary" else empty(sync)
    checks = [] if component == "primary" else [inherited(sync, selected)]
    body = payload(sync, main, checks, [checks[0]["check_id"]] if component == "required" else [])
    entered, release, _ = pause_first_snapshot(sync, monkeypatch)
    thread, result = spawn(lambda: post(sync, "prepare", body))
    try:
        assert entered.wait(4)
        forgotten = sync.post("memory/revise", sync.revision(saved["result"]["record_ids"][0]))
        assert forgotten.status_code == 200, forgotten.text
        assert forgotten.json()["authoritative_state"] == "tombstoned"
    finally:
        release.set()
    response = joined(thread, result)
    assert response.status_code == 200, response.text
    if component == "primary":
        assert response.json()["component"] == "current_selection"
    elif component == "required":
        assert response.json()["component"] == "required_check"
        assert response.json()["check_id"] == checks[0]["check_id"]
    else:
        assert response.json()["state"] == "valid"
        assert response.json()["checks"][0]["reason"] == "dependency_changed"
    with sync.store.transaction() as db:
        keys = [item["source_key"] for item in selected["source_dependencies"]]
        assert all(
            db.execute("SELECT 1 FROM suppression WHERE source_key=?", (key,)).fetchone()
            for key in keys
        )
        assert all(
            db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0] == "withdrawn"
            for key in keys
        )
    strict = post(sync, "verify", check_request(sync, main, checks, "verify"))
    assert strict.status_code == 409, strict.text


@pytest.mark.parametrize("remote_rollback", [False, True])
def test_real_concurrent_newer_checkpoint_retries_m0_without_masking_remote_rollback(
    sync, monkeypatch, remote_rollback
):
    prepare(sync)
    bundle = empty(sync)
    old_head = copy.deepcopy(sync.core_head)
    entered, release, first_thread = pause_first_snapshot(sync, monkeypatch)
    collected = []
    authority = sync.service.source_authority
    original = authority._collect_proof

    def collect(*args, **kwargs):
        value = original(*args, **kwargs)
        collected.append((threading.get_ident(), value["m0"]))
        return value

    monkeypatch.setattr(authority, "_collect_proof", collect)
    thread, result = spawn(lambda: post(sync, "prepare", payload(sync, bundle)))
    try:
        assert entered.wait(4)
        sync.core_head["sequence"] += 1
        writer = post(sync, "query", request(sync, "真实更晚的完整域checkpoint"))
        assert writer.status_code == 200, writer.text
        with sync.store.transaction() as db:
            assert (
                db.execute("SELECT sequence FROM owner_heads WHERE owner='core'").fetchone()[0]
                > old_head["sequence"]
            )
        if remote_rollback:
            sync.core_head["sequence"] = old_head["sequence"] - 1
    finally:
        release.set()
    response = joined(thread, result)
    attempts = [revision for identity, revision in collected if identity == first_thread[0]]
    if remote_rollback:
        assert response.status_code == 503 and response.json()["code"] == "dependency_unavailable"
        assert len(attempts) == 1  # A real remote contract failure is not a CAS retry.
    else:
        assert response.status_code == 200 and response.json()["state"] == "valid", response.text
        assert len(attempts) == 2 and attempts[1] > attempts[0]


def test_before_refresh_head_still_rejects_true_local_rollback_when_m0_is_unchanged(
    sync, monkeypatch
):
    prepare(sync)
    bundle = empty(sync)
    authority = sync.service.source_authority
    original = authority.transport.facts
    snapshots = []

    def facts(body):
        result = original(body)
        if body["mode"] == "snapshot":
            snapshots.append(1)
            if len(snapshots) == 1:
                result["head"]["sequence"] -= 1
        return result

    monkeypatch.setattr(authority.transport, "facts", facts)
    response = post(sync, "prepare", payload(sync, bundle))
    assert response.status_code == 503 and response.json()["code"] == "dependency_unavailable"
    assert len(snapshots) == 2  # Fresh after-facts cannot erase the bad original head.


def test_a_real_local_change_on_each_attempt_keeps_the_original_three_attempt_bound(
    sync, monkeypatch
):
    prepare(sync)
    bundle = empty(sync)
    authority = sync.service.source_authority
    original = authority._observe_proof
    calls = []

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        with sync.service.local_commit() as db:
            sync.service._bump(db, sync.scope(), history_changed=False)
        calls.append(1)
        return result

    monkeypatch.setattr(authority, "_observe_proof", observe)
    response = post(sync, "prepare", payload(sync, bundle))
    assert response.status_code == 503 and response.json()["code"] == "dependency_unavailable"
    assert calls == [1, 1, 1]
    assert authority._operation_order._owner is None and not authority._operation_order._waiters


@pytest.mark.parametrize("boundary", ["full_explicit_read", "selected_without_read_capability"])
def test_full_or_implicit_selected_callers_keep_the_original_serial_remote_boundary(
    sync, monkeypatch, boundary
):
    prepare(sync)
    bundle = empty(sync)
    context = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
    body = (
        payload(sync, bundle)
        if boundary == "selected_without_read_capability"
        else request(sync, "原完整域")
    )
    items = read_bundle.requirements(sync.service, body, context)
    authority = sync.service.source_authority
    original = authority.transport.facts
    entered = []

    def facts(value):
        assert authority._operation_order._owner == threading.get_ident()
        entered.append(1)
        return original(value)

    monkeypatch.setattr(authority.transport, "facts", facts)
    selected = (
        read_bundle.operation(sync.service, items)
        if boundary == "full_explicit_read"
        else authority.bundle_operation(sync.service, items)
    )
    with selected as db:
        assert authority._operation_order._owner == threading.get_ident()
        assert authority._bundled.active["db"] is db
    assert entered and authority._operation_order._owner is None


def test_corrupt_ranking_cleanup_stays_in_the_real_full_initial_boundary(sync, monkeypatch):
    backend = indexed(sync)
    backend.config["rerank"] = True  # The initial ranking must retain original full authority.
    with sync.store.transaction() as db:
        row = db.execute("SELECT document_id FROM memory_vectors LIMIT 1").fetchone()
        assert row is not None
        db.execute("UPDATE memory_vectors SET vector='{}' WHERE document_id=?", (row[0],))
    authority = sync.service.source_authority
    original = persistent_retrieval.discard_corrupt
    deleted = []

    def discard(db, plan):
        assert authority._operation_order._owner == threading.get_ident()
        assert authority._bundled.active["domains"]
        deleted.extend(plan.corrupted)
        return original(db, plan)

    monkeypatch.setattr(persistent_retrieval, "discard_corrupt", discard)
    response = contextual_query(sync, "睡前乌龙茶")
    assert response["bundle"]["retrieval_mode"] == "context" and deleted
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT 1 FROM memory_vectors WHERE document_id=?", (row[0],)).fetchone()
            is None
        )


def test_raw_negative_commits_before_a_bad_turn_relationship_is_rejected(sync):
    prepare(sync)
    bundle = empty(sync)
    turn = original_turn(sync, "negative-before-bad-input")
    check = raw_check(sync, turn=turn)
    check["request"]["source_check"]["input_revision"] += 1
    key = sync.service.source_identity(turn["input_sources"][0], turn["scope"])
    physical = sync.physicals[-1]
    physical.update(
        revision=2,
        state="withdrawn",
        kind="retract",
        physical_receipt_id="raw:physical:retracted-before-bad-turn",
        content_digest="b" * 64,
    )
    sync.core_head["sequence"] += 1
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    assert response.status_code == 503 and response.json()["code"] == "dependency_unavailable"
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0] == "withdrawn"
        )
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["queued", "remote_started"])
def test_selected_read_cancellation_and_late_worker_keep_the_original_lease_lifetime(
    sync, monkeypatch, stage
):
    prepare(sync)
    bundle = empty(sync)
    body = payload(sync, bundle)
    authority = sync.service.source_authority
    lock = authority._operation_lock
    observed = ObservedLock(lock)
    monkeypatch.setattr(authority, "_operation_lock", observed)
    completed = threading.Event()
    entered, release, _ = pause_first_snapshot(sync, monkeypatch)
    sync.calls.clear()

    async def scenario():
        application = sync.client.app.state.memory_context
        original = application.prepare_v2

        def prepare_context(*args, **kwargs):
            result = original(*args, **kwargs)
            completed.set()
            return result

        monkeypatch.setattr(application, "prepare_v2", prepare_context)
        app = create_app(
            service=sync.service,
            auth=sync.client.app.state.auth,
            memory_context=application,
            execute_timeout=0.5,
            max_active=1,
        )
        path = "/internal/v2/memory/context/prepare"
        async with client_for(app) as client:
            if stage == "queued":
                lock.acquire()
            pending = asyncio.create_task(client.post(path, json=body))
            try:
                await until(observed.waiting.is_set if stage == "queued" else entered.is_set)
                response = await pending
                assert response.status_code == 408, response.text
                if stage == "queued":
                    await until(lambda: app.state.chat_requests.active == 0)
                    assert not entered.is_set() and not completed.is_set()
                    assert [kind for kind, _, _ in sync.calls if kind != "origin"] == []
                else:
                    assert app.state.chat_requests.active == 1
                    assert (await client.post(path, json=body)).status_code == 429
            finally:
                release.set()
                if stage == "queued":
                    lock.release()
                await until(lambda: app.state.chat_requests.active == 0)
            assert completed.is_set() is (stage == "remote_started")
            recovered = await client.post(path, json=body)
            assert recovered.status_code == 200 and recovered.json()["state"] == "valid", (
                recovered.text
            )
            assert app.state.chat_requests.active == 0
            assert (
                authority._operation_order._owner is None
                and not authority._operation_order._waiters
            )

    asyncio.run(scenario())
