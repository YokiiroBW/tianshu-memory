"""Independent remote proofs overlap inside the original complete source gate."""

import asyncio
import copy
import threading
from contextlib import contextmanager
from contextvars import ContextVar, copy_context

import pytest
from summary_fixtures import pending, prepare, submit, window, withdraw_source
from test_audit_runtime import until
from test_contextual_recall import contextual_query
from test_group_context import owner
from test_memory_preparation import payload
from test_memory_raw_sources import original_turn, raw_check
from test_memory_read_cancellation import ObservedLock, client_for
from test_memory_v2 import candidate, post, request
from test_segments import archive, front, output, seal
from test_segments import pending as segment_pending
from test_segments import ready as segment_ready
from test_segments import submit as segment_submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import segment_sources, source_authority
from tianshu_memory.app import create_app
from tianshu_memory.domain import Fault, fingerprint
from tianshu_memory.memory_context import read_bundle, read_v2
from tianshu_memory.read_lifecycle import cancellable_read


def empty(h):
    return contextual_query(h, "没有选中材料", limits=dict(facts=0, episodes=0))["bundle"]


def passive(h):
    segment_ready(h)
    descriptor, _ = archive(h, text="本人周末去河边散步。", replies=())
    actor = owner(
        h,
        audience=h.scope()["audience"],
        account=dict(namespace="qq", immutable_account_id="60001"),
    )
    body = dict(
        request(h, "河边散步", actor=actor),
        retrieval_mode="context",
        limits=dict(facts=0, episodes=1),
    )
    response = post(h, "query", body)
    assert response.status_code == 200, response.text
    bundle = response.json()["bundle"]
    assert [item["source_key"] for item in bundle["source_dependencies"]] == [
        descriptor["source_key"]
    ]
    return bundle, actor


def read_once(h, bundle, actor=0):
    context = copy.deepcopy(h.contexts[f"synthetic-viewer:{actor}"])
    body = payload(h, bundle)
    if actor:
        body.update(
            query=request(h, "河边散步", actor=actor)["query"], requested_scope=h.scope(actor)
        )
    items = read_bundle.requirements(h.service, body, context)
    with read_bundle.operation(h.service, items) as db:
        fresh = read_v2.verify_selected(h.service, db, bundle, context)
        authority = h.service.source_authority
        assert authority._bundled.active["db"] is db
        assert authority._bundled.active["domains"] == set()
        with pytest.raises(Fault) as rejected:
            with h.service.operation(h.scope(actor), context=context):
                pytest.fail("A selected checkpoint lent a full data domain")
        assert rejected.value.code == "dependency_unavailable"
        return fresh


def spawn(action):
    result = {}

    def run():
        try:
            result["value"] = action()
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=copy_context().run, args=(run,), daemon=True)
    thread.start()
    return thread, result


def joined(thread, result):
    thread.join(8)
    assert not thread.is_alive(), "The source worker or its remote futures did not finish"
    if "error" in result:
        raise result["error"]
    return result.get("value")


def test_before_join_preserves_common_current_time_and_original_thread_ownership(sync, monkeypatch):
    bundle, actor = passive(sync)
    authority = sync.service.source_authority
    actual_store = sync.service.store
    original_read, original_facts, original_current = (
        segment_sources.read,
        authority.transport.facts,
        authority.transport.current,
    )
    original_transaction, original_clock = actual_store.transaction, sync.service.clock
    started, release, core_done, current_called = (threading.Event() for _ in range(4))
    marker = ContextVar("selected_remote_test_marker", default=None)
    main_threads, child_threads, transaction_threads, phases = [], set(), set(), []
    calls = []

    @contextmanager
    def transaction():
        transaction_threads.add(threading.get_ident())
        with original_transaction() as db:
            yield db

    def clock():
        if authority._operation_order._owner is not None:
            assert threading.get_ident() == authority._operation_order._owner
            assert marker.get() == "request"
        return original_clock()

    def remote():
        thread = threading.get_ident()
        assert thread != authority._operation_order._owner
        assert getattr(authority._bundled, "active", None) is None
        assert marker.get() == "request"
        child_threads.add(thread)

    def read(service, materials):
        remote()
        marker.set("private child context")
        calls.append(1)
        result = original_read(service, materials)
        if len(calls) == 1:
            phases.append("before")
            started.set()
            assert release.wait(5)
        else:
            assert current_called.is_set()
            phases.append("after")
        return result

    def facts(body):
        remote()
        result = original_facts(body)
        if body["mode"] == "snapshot":
            core_done.set()
        return result

    def current(body):
        assert release.is_set() and core_done.is_set() and started.is_set()
        assert marker.get() == "request"
        assert threading.get_ident() == authority._operation_order._owner
        main_threads.append(threading.get_ident())
        phases.append("current")
        current_called.set()
        return original_current(body)

    monkeypatch.setattr(actual_store, "transaction", transaction)
    monkeypatch.setattr(sync.service, "clock", clock)
    monkeypatch.setattr(segment_sources, "read", read)
    monkeypatch.setattr(authority.transport, "facts", facts)
    monkeypatch.setattr(authority.transport, "current", current)
    token = marker.set("request")
    try:
        with cancellable_read(lambda: False):
            task = spawn(lambda: read_once(sync, bundle, actor))
            try:
                assert started.wait(4) and core_done.wait(4)
                assert not current_called.is_set()
                assert authority._operation_order._owner is not None
            finally:
                release.set()
            assert joined(*task)["bundle_id"] == bundle["bundle_id"]
        assert marker.get() == "request"
    finally:
        marker.reset(token)
        release.set()
    assert phases == ["before", "current", "after"]
    assert len(main_threads) == 1 and child_threads.isdisjoint(transaction_threads)
    assert authority._operation_order._owner is None


def test_real_explicit_window_proof_finishes_before_platform_current(sync, monkeypatch):
    prepare(sync)
    # The configured app/transport owns a separate lazily loaded Contracts.
    # Reach its real v2 entry before installing the remote-window wait.
    empty(sync)
    job = candidate(sync, "准备在河边见面。", 1, capture=False)[0]
    manifest = window(sync, [job])["manifest"]
    authority = sync.service.source_authority
    window_done, release, core_done, current_called = (threading.Event() for _ in range(4))
    original_window = authority.transport.window_check
    original_facts, original_current = authority.transport.facts, authority.transport.current

    def check(value):
        result = original_window(value)
        window_done.set()
        assert release.wait(5)
        return result

    def facts(value):
        result = original_facts(value)
        if value["mode"] == "snapshot":
            core_done.set()
        return result

    def current(value):
        assert release.is_set() and window_done.is_set() and core_done.is_set()
        assert threading.get_ident() == authority._operation_order._owner
        current_called.set()
        return original_current(value)

    def read():
        context = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
        with sync.service.operation(sync.scope(), context=context, windows=[manifest]) as db:
            assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        return True

    monkeypatch.setattr(authority.transport, "window_check", check)
    monkeypatch.setattr(authority.transport, "facts", facts)
    monkeypatch.setattr(authority.transport, "current", current)
    task = spawn(read)
    try:
        before_finished = window_done.wait(4) and core_done.wait(4)
        early_current = current_called.is_set()
    finally:
        release.set()
    assert joined(*task) is True
    assert before_finished and not early_current and current_called.is_set()


@pytest.mark.parametrize("first", ["passive", "windows", "core"])
def test_before_failures_keep_original_priority_and_join_all_futures(sync, monkeypatch, first):
    prepare(sync)
    bundle = empty(sync)
    authority = sync.service.source_authority
    errors = dict(
        passive=Fault("forbidden", 403),
        windows=Fault("scope_changed", 409),
        core=Fault("dependency_unavailable", 503),
    )
    expected = errors[first]
    together = threading.Barrier(3)
    finished = []
    original_read, original_windows, original_facts = (
        segment_sources.read,
        authority._check_windows,
        authority.transport.facts,
    )

    def work(kind, original, *args):
        together.wait(4)
        try:
            if ["passive", "windows", "core"].index(kind) >= ["passive", "windows", "core"].index(
                first
            ):
                raise errors[kind]
            return original(*args)
        finally:
            finished.append(kind)

    monkeypatch.setattr(
        segment_sources, "read", lambda *args: work("passive", original_read, *args)
    )
    monkeypatch.setattr(
        authority, "_check_windows", lambda *args: work("windows", original_windows, *args)
    )
    monkeypatch.setattr(
        authority.transport, "facts", lambda *args: work("core", original_facts, *args)
    )
    with pytest.raises(Fault) as rejected:
        read_once(sync, bundle)
    assert rejected.value is expected
    assert set(finished) == {"passive", "windows", "core"}
    assert authority._operation_order._owner is None


@pytest.mark.parametrize("first", ["final", "refresh", "after"])
def test_after_failure_waits_for_final_head_and_main_thread_refresh(sync, monkeypatch, first):
    prepare(sync)
    bundle = empty(sync)
    authority = sync.service.source_authority
    original_read, original_facts = segment_sources.read, authority.transport.facts
    original_refresh = authority._refresh_unchanged
    after_done, refreshed = threading.Event(), []
    reads = []
    early = Fault("dependency_unavailable", 503)
    late = Fault("forbidden", 403)

    def read(*args):
        reads.append(1)
        if len(reads) == 2:
            after_done.set()
            raise late
        return original_read(*args)

    def facts(body):
        value = original_facts(body)
        if body["mode"] == "head":
            assert after_done.wait(4)
            if first == "final":
                raise early
            if first == "refresh":
                value = copy.deepcopy(value)
                value["head"]["sequence"] += 1
        return value

    def refresh(*args):
        assert threading.get_ident() == authority._operation_order._owner
        refreshed.append(True)
        if first == "refresh":
            raise early
        return original_refresh(*args)

    monkeypatch.setattr(segment_sources, "read", read)
    monkeypatch.setattr(authority.transport, "facts", facts)
    monkeypatch.setattr(authority, "_refresh_unchanged", refresh)
    with pytest.raises(Fault) as rejected:
        read_once(sync, bundle)
    assert rejected.value is (late if first == "after" else early)
    assert bool(refreshed) is (first == "refresh") and after_done.is_set()
    assert authority._operation_order._owner is None


@pytest.mark.parametrize("when", ["before_return", "after_current"])
def test_actual_passive_archive_revocation_during_checkpoint_is_durable(sync, monkeypatch, when):
    bundle, actor = passive(sync)
    authority = sync.service.source_authority
    original_read, original_current = segment_sources.read, authority.transport.current
    changed = []

    def revoke():
        if not changed:
            changed.append(True)
            sync.passive_owner.archive_epoch += 1

    def read(*args):
        value = original_read(*args)
        if when == "before_return":
            revoke()
        return value

    def current(*args):
        value = original_current(*args)
        if when == "after_current":
            revoke()
        return value

    monkeypatch.setattr(segment_sources, "read", read)
    monkeypatch.setattr(authority.transport, "current", current)
    body = dict(
        payload(sync, bundle),
        query=request(sync, "河边散步", actor=actor)["query"],
        requested_scope=sync.scope(actor),
    )
    response = post(sync, "prepare", body)
    assert response.status_code == 200 and response.json()["state"] == "invalid", response.text
    assert changed
    with sync.store.transaction() as db:
        for item in bundle["source_dependencies"]:
            assert (
                db.execute(
                    "SELECT state FROM sources WHERE key=?", (item["source_key"],)
                ).fetchone()[0]
                == "withdrawn"
            )


def test_core_foreign_context_withdrawal_after_current_cannot_pass_or_mix_owners(sync, monkeypatch):
    if sync.scope()["audience"] != "group":
        pytest.skip("An actual second author in the same group")
    prepare(sync)
    other = owner(sync)
    prior = candidate(sync, "九点去河边。", 1, other, capture=False)[0]
    current = candidate(sync, "我到时一起去散步。", 2, capture=False)[0]
    supplied = window(sync, [current], context=[prior])
    submit(sync, pending(sync, [current], supplied), "本人准备一起去河边散步。")
    bundle = contextual_query(sync, "河边散步", limits=dict(facts=1, episodes=0))["bundle"]
    assert bundle["group_dependencies"]
    authority = sync.service.source_authority
    original = authority.transport.current
    changed = []

    def access(body):
        value = original(body)
        if not changed:
            changed.append(True)
            withdraw_source(sync, prior)
        return value

    monkeypatch.setattr(authority.transport, "current", access)
    result = post(sync, "prepare", payload(sync, bundle))
    assert result.status_code == 200 and result.json()["state"] == "invalid", result.text
    with sync.store.transaction() as db:
        assert all(
            db.execute(
                "SELECT state FROM groups WHERE id=?", (item["group"]["semantic_group_id"],)
            ).fetchone()[0]
            != "active"
            for item in bundle["group_dependencies"]
        )


@pytest.mark.parametrize("boundary", ["turn_pages", "selector_pages"])
def test_multiple_pages_keep_original_serial_proof_order(sync, monkeypatch, boundary):
    prepare(sync)
    bundle = empty(sync)
    catalog = []
    if boundary == "turn_pages":
        catalog = [
            raw_check(sync, turn=original_turn(sync, f"many:{index}"), identity=f"many:{index}")
            for index in range(33)
        ]
    elif boundary == "selector_pages":
        catalog = [
            raw_check(sync, turn=original_turn(sync, "many-sources", count=256)),
            raw_check(sync, turn=original_turn(sync, "last-source"), identity="last-source"),
        ]
    body = payload(sync, bundle, catalog)
    context = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
    items = read_bundle.requirements(
        sync.service, body, context, legacy=[(check, context) for check in catalog]
    )
    authority = sync.service.source_authority
    original = authority.transport.facts
    seen = []

    def disallow(*args, **kwargs):
        pytest.fail("A multi-page checkpoint created remote workers")

    def facts(value):
        seen.append(value["mode"])
        assert threading.get_ident() == authority._operation_order._owner
        return original(value)

    monkeypatch.setattr(source_authority, "ThreadPoolExecutor", disallow)
    monkeypatch.setattr(authority.transport, "facts", facts)
    with read_bundle.operation(sync.service, items):
        pass
    assert seen and seen[-1] == "head"
    if boundary.endswith("pages"):
        assert seen.count("snapshot") == 2


def test_direct_barrier_without_operation_keeps_original_serial_owner_calls(sync, monkeypatch):
    prepare(sync)
    authority = sync.service.source_authority
    original = authority.transport.facts
    main = threading.get_ident()
    seen = []

    def disallow(*args, **kwargs):
        pytest.fail("An ungated direct barrier created remote workers")

    def facts(value):
        assert threading.get_ident() == main and authority._operation_order._owner is None
        seen.append(value["mode"])
        return original(value)

    monkeypatch.setattr(source_authority, "ThreadPoolExecutor", disallow)
    monkeypatch.setattr(authority.transport, "facts", facts)
    result, viewer = authority.barrier(sync.service, sync.scope(), context=None)
    assert result["local_revision"] >= 0 and viewer is None
    assert seen == ["snapshot", "head"]


@pytest.mark.parametrize("entry", ["consume", "check", "segment_commit"])
def test_default_event_check_and_segment_commit_keep_the_same_gated_remote_proof(
    sync, monkeypatch, entry
):
    prepare(sync)
    authority = sync.service.source_authority
    segment = None
    if entry == "segment_commit":
        segment_ready(sync)
        material, _ = front(sync, "准备去河边。", "single-checkpoint")
        sealed, _ = seal(sync, [material])
        segment = segment_pending(sync, sealed)
    original_facts = authority.transport.facts
    original_commit = authority._commit_observations
    remote, committed = [], []

    def facts(value):
        if not value["include_content"]:
            assert threading.get_ident() != authority._operation_order._owner
            assert authority._operation_order._owner is not None
            assert getattr(authority._bundled, "active", None) is None
            remote.append(value["mode"])
        return original_facts(value)

    def commit(*args, **kwargs):
        assert threading.get_ident() == authority._operation_order._owner
        committed.append(True)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(authority.transport, "facts", facts)
    monkeypatch.setattr(authority, "_commit_observations", commit)
    if entry == "consume":
        result = sync.post("memory/turn-commits", sync.event())
        assert result.status_code == 200 and result.json()["state"] == "accepted", result.text
    elif entry == "check":
        result = sync.post("memory/source-sync/check", sync.check())
        assert result.status_code == 200, result.text
    else:
        result, _ = segment_submit(sync, segment, output(segment))
        assert result.status_code == 200 and result.json()["state"] == "completed", result.text
        with sync.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1
    assert remote and committed and authority._operation_order._owner is None


def test_false_refresh_discards_speculative_after_error_and_preserves_raw_negative(
    sync, monkeypatch
):
    prepare(sync)
    bundle = empty(sync)
    turn = original_turn(sync, "changed-after-current")
    check = raw_check(sync, turn=turn)
    key = sync.service.source_identity(turn["input_sources"][0], turn["scope"])
    authority = sync.service.source_authority
    original_current, original_read = authority.transport.current, segment_sources.read
    original_refresh = authority._refresh_unchanged
    changed, reads, late_errors, refreshes, currents = [], [], [], [], []

    def current(body):
        value = original_current(body)
        currents.append(True)
        if not changed:
            changed.append(True)
            sync.physicals[-1].update(
                revision=2,
                state="withdrawn",
                kind="retract",
                physical_receipt_id="physical:changed-after-current",
                content_digest="b" * 64,
            )
            sync.core_head["sequence"] += 1
        return value

    def read(*args):
        reads.append(True)
        if len(reads) == 2:
            late_errors.append(True)
            raise Fault("dependency_unavailable", 503)
        return original_read(*args)

    def refresh(*args):
        result = original_refresh(*args)
        refreshes.append(result)
        return result

    monkeypatch.setattr(authority.transport, "current", current)
    monkeypatch.setattr(segment_sources, "read", read)
    monkeypatch.setattr(authority, "_refresh_unchanged", refresh)
    sync.calls.clear()
    result = post(sync, "prepare", payload(sync, bundle, [check]))
    # Current owner facts no longer match the exact raw input. The original
    # source-check error is global even after its negative was committed.
    assert result.status_code == 503 and result.json()["code"] == "dependency_unavailable"
    assert "checks" not in result.json()
    assert late_errors and refreshes == [False] and len(currents) >= 2
    assert len([kind for kind, _, _ in sync.calls if kind == "snapshot"]) >= 3
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0] == "withdrawn"
        )
        assert (
            db.execute(
                "SELECT state FROM physical_sources WHERE key=?",
                (fingerprint(sync.physicals[-1]["key"]),),
            ).fetchone()[0]
            == "withdrawn"
        )


@pytest.mark.parametrize("each_attempt", [False, True])
def test_real_local_revision_change_retains_original_cas_and_three_attempt_bound(
    sync, monkeypatch, each_attempt
):
    prepare(sync)
    bundle = empty(sync)
    authority = sync.service.source_authority
    original = authority.transport.current
    writes = []

    def current(body):
        value = original(body)
        if not writes or each_attempt:
            assert threading.get_ident() == authority._operation_order._owner
            with sync.service.local_commit() as db:
                sync.service._bump(db, sync.scope(), history_changed=False)
            writes.append(True)
        return value

    monkeypatch.setattr(authority.transport, "current", current)
    sync.calls.clear()
    result = post(sync, "prepare", payload(sync, bundle))
    if each_attempt:
        assert result.status_code == 503 and result.json()["code"] == "dependency_unavailable"
        assert len(writes) == 3
    else:
        assert result.status_code == 200 and result.json()["state"] == "valid", result.text
        assert len(writes) == 1
    assert authority._operation_order._owner is None


@pytest.mark.parametrize("stage", ["queued", "remote_started"])
def test_cancellation_keeps_gate_and_lease_until_all_remote_futures_finish(
    sync, monkeypatch, stage
):
    prepare(sync)
    bundle = empty(sync)
    body = payload(sync, bundle)
    authority = sync.service.source_authority
    lock = authority._operation_lock
    observed = ObservedLock(lock)
    monkeypatch.setattr(authority, "_operation_lock", observed)
    entered, release, completed = (threading.Event() for _ in range(3))
    original = authority.transport.facts

    def facts(value):
        result = original(value)
        if value["mode"] == "snapshot" and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(authority.transport, "facts", facts)

    async def scenario():
        application = sync.client.app.state.memory_context
        prepare_v2 = application.prepare_v2

        def prepared(*args, **kwargs):
            result = prepare_v2(*args, **kwargs)
            completed.set()
            return result

        monkeypatch.setattr(application, "prepare_v2", prepared)
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
            task = asyncio.create_task(client.post(path, json=body))
            try:
                await until(observed.waiting.is_set if stage == "queued" else entered.is_set)
                response = await task
                assert response.status_code == 408, response.text
                if stage == "queued":
                    await until(lambda: app.state.chat_requests.active == 0)
                    assert not entered.is_set() and not completed.is_set()
                else:
                    assert app.state.chat_requests.active == 1
                    assert authority._operation_order._owner is not None
                    assert (await client.post(path, json=body)).status_code == 429
            finally:
                release.set()
                if stage == "queued":
                    lock.release()
                await until(lambda: app.state.chat_requests.active == 0)
            assert completed.is_set() is (stage == "remote_started")
            assert authority._operation_order._owner is None
            recovered = await client.post(path, json=body)
            assert recovered.status_code == 200 and recovered.json()["state"] == "valid", (
                recovered.text
            )

    asyncio.run(scenario())
