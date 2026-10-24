"""Expired queued reads stop before sync; started work retains its completion rules."""

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_audit_runtime import until
from test_memory_context import ProofIssuer
from test_memory_preparation import checks, payload, seeded
from test_memory_v2 import post, query, request
from test_memory_verification_contexts import verify_payload
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.app import create_app
from tianshu_memory.domain import Fault
from tianshu_memory.read_lifecycle import begin_source_read, cancellable_read, source_read_lock
from tianshu_memory.source_authority import SourceAuthority
from tianshu_memory.source_order import SourceOperationOrder


class ObservedLock:
    def __init__(self, lock):
        self.lock = lock
        self.waiting, self.acquired = threading.Event(), threading.Event()
        self.polled = threading.Event()

    def acquire(self, *args, **kwargs):
        self.waiting.set()
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.acquired.set()
        else:
            self.polled.set()
        return acquired

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def read_request(h, operation):
    bundle = seeded(h)
    if operation == "query":
        return request(h, "我喜欢什么咖啡")
    catalog = checks(h, bundle)
    if operation == "prepare":
        return payload(h, bundle, catalog)
    if operation == "verify":
        return verify_payload(h, bundle, catalog)
    profile = catalog[-1]["request"]
    return dict(
        schema_version=2,
        query=profile["query"],
        requested_scope=profile["requester_scope"],
        requests=[profile],
    )


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-only-companion-secret"},
    )


@pytest.mark.parametrize("operation", ["query", "prepare", "verify", "profiles"])
def test_expired_queued_v2_read_does_not_start_sync_and_keeps_admission_until_exit(
    sync, monkeypatch, operation
):
    data = read_request(sync, operation)
    authority = sync.service.source_authority
    lock = authority._operation_lock
    observed = ObservedLock(lock)
    monkeypatch.setattr(authority, "_operation_lock", observed)
    barriers, transactions_after_lock = [], []
    original_barrier, original_transaction = authority.barrier, sync.store.transaction

    def barrier(*args, **kwargs):
        barriers.append(1)
        return original_barrier(*args, **kwargs)

    @contextmanager
    def transaction(*args, **kwargs):
        if observed.acquired.is_set():
            transactions_after_lock.append(1)
        with original_transaction(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(authority, "barrier", barrier)
    monkeypatch.setattr(sync.store, "transaction", transaction)
    sync.calls.clear()

    async def scenario():
        app = create_app(
            service=sync.service,
            auth=sync.client.app.state.auth,
            execute_timeout=0.5,
            max_active=1,
        )
        path = "/internal/v2/memory/context/" + operation
        async with client_for(app) as client:
            lock.acquire()
            try:
                pending = asyncio.create_task(client.post(path, json=data))
                await until(observed.waiting.is_set)
                assert app.state.chat_requests.active == 1
                assert (await client.post(path, json=data)).status_code == 429
                response = await pending
                assert response.status_code == 408, response.text
                assert response.json()["execution_state"] == "not_started"
                assert response.json()["retryable"] is True
                # The original source lock is still held by this thread. The
                # abandoned worker must exit without first acquiring it.
                await until(lambda: app.state.chat_requests.active == 0)
                assert not observed.acquired.is_set()
            finally:
                lock.release()
                await until(lambda: app.state.chat_requests.active == 0)
            assert not observed.acquired.is_set()
            assert barriers == [] and transactions_after_lock == []
            assert [kind for kind, _, _ in sync.calls if kind != "origin"] == []
            # The worker drained and returned its slot; fresh work still has its full barrier.
            recovered = await client.post(path, json=data)
            assert recovered.status_code == 200, recovered.text
            assert barriers

    asyncio.run(scenario())


def test_live_queued_read_keeps_waiting_then_reads_under_the_same_lock(sync, monkeypatch):
    data = read_request(sync, "query")
    authority = sync.service.source_authority
    lock = authority._operation_lock
    observed = ObservedLock(lock)
    monkeypatch.setattr(authority, "_operation_lock", observed)
    entered = threading.Event()
    original = authority.barrier

    def barrier(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(authority, "barrier", barrier)

    async def scenario():
        app = create_app(
            service=sync.service,
            auth=sync.client.app.state.auth,
            execute_timeout=3,
            max_active=1,
        )
        async with client_for(app) as client:
            lock.acquire()
            try:
                pending = asyncio.create_task(
                    client.post("/internal/v2/memory/context/query", json=data)
                )
                await until(observed.polled.is_set)
                assert app.state.chat_requests.active == 1
                assert not entered.is_set() and not pending.done()
            finally:
                lock.release()
            response = await pending
            assert response.status_code == 200, response.text
            assert observed.acquired.is_set() and entered.is_set()
            assert app.state.chat_requests.active == 0

    asyncio.run(scenario())


def test_timed_out_v2_read_already_inside_its_barrier_still_finishes(sync, monkeypatch):
    data = read_request(sync, "query")
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    authority = sync.service.source_authority
    original_barrier = authority.barrier

    def barrier(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_barrier(*args, **kwargs)

    monkeypatch.setattr(authority, "barrier", barrier)
    sync.calls.clear()

    async def scenario():
        app = create_app(
            service=sync.service,
            auth=sync.client.app.state.auth,
            execute_timeout=0.5,
            max_active=1,
        )
        original = app.state.memory_context.query_v2

        def query(*args, **kwargs):
            result = original(*args, **kwargs)
            completed.set()
            return result

        monkeypatch.setattr(app.state.memory_context, "query_v2", query)
        async with client_for(app) as client:
            pending = asyncio.create_task(
                client.post("/internal/v2/memory/context/query", json=data)
            )
            try:
                await until(entered.is_set)
                response = await pending
                assert response.status_code == 408, response.text
                assert app.state.chat_requests.active == 1
            finally:
                release.set()
                await until(lambda: app.state.chat_requests.active == 0)
            assert completed.is_set()
            assert [kind for kind, _, _ in sync.calls if kind != "origin"] == [
                "snapshot",
                "current",
                "head",
            ]

    asyncio.run(scenario())


def test_queued_v2_forget_keeps_unknown_and_commits_once_after_timeout(sync, monkeypatch):
    seeded(sync)
    selected = query(sync, "我喜欢什么咖啡")
    target = next(item for item in selected["evidence"] if item["kind"] == "episode")
    data = dict(
        command=sync.command(),
        scope=sync.scope(),
        batch_ref=None,
        item_id="forget:late",
        kind="forget_episode",
        target=dict(episode_ref=target["episode_id"], episode_version=target["episode_version"]),
        evidence_refs=[sync.admissions[-1]["source"]],
        proof_ref="test:proof",
    )
    application = sync.client.app.state.memory_context
    application.proofs = ProofIssuer()
    application.proofs.issue(data, "revision", [sync.account], [sync.scope()])
    receipts = []
    original = application.forget_v2

    def forget(*args, **kwargs):
        result = original(*args, **kwargs)
        receipts.append(result)
        return result

    monkeypatch.setattr(application, "forget_v2", forget)
    lock = sync.service.source_authority._operation_lock
    observed = ObservedLock(lock)
    monkeypatch.setattr(sync.service.source_authority, "_operation_lock", observed)

    async def scenario():
        app = create_app(
            service=sync.service,
            auth=sync.client.app.state.auth,
            memory_context=application,
            execute_timeout=0.5,
            max_active=1,
        )
        async with client_for(app) as client:
            lock.acquire()
            try:
                pending = asyncio.create_task(
                    client.post("/internal/v2/memory/context/forget", json=data)
                )
                await until(observed.waiting.is_set)
                response = await pending
                assert response.status_code == 408, response.text
                assert response.json()["execution_state"] == "unknown"
                assert response.json()["retryable"] is False
                assert app.state.chat_requests.active == 1
            finally:
                lock.release()
                await until(lambda: app.state.chat_requests.active == 0)
        assert len(receipts) == 1 and receipts[0]["state"] == "tombstoned"

    asyncio.run(scenario())
    replay = post(sync, "forget", data)
    assert replay.status_code == 200 and replay.json() == receipts[0]
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM suppression").fetchone()[0] == 1


@pytest.mark.parametrize("bundle", [False, True])
def test_cancel_checkpoint_precedes_every_outer_authority_transaction(bundle):
    authority = object.__new__(SourceAuthority)
    authority._operation_order = SourceOperationOrder()
    authority._operation_lock, authority._bundled = threading.RLock(), threading.local()

    def forbidden():
        pytest.fail("Cancelled queued read opened a transaction")

    service = SimpleNamespace(store=SimpleNamespace(transaction=forbidden))
    with cancellable_read(lambda: True):
        with pytest.raises(Fault) as stopped:
            with (
                authority.bundle_operation(service, [dict(scope={})])
                if bundle
                else authority.operation(service, {})
            ):
                pytest.fail("Cancelled queued read reached business work")
    assert (stopped.value.code, stopped.value.status) == ("timeout", 408)


def test_started_read_is_not_cancelled_between_later_boundaries_and_scope_is_reset():
    abandoned = False
    lock = threading.RLock()
    with cancellable_read(lambda: abandoned):
        with source_read_lock(lock):
            begin_source_read()
        abandoned = True
        with source_read_lock(lock):
            begin_source_read()
    with cancellable_read(lambda: True):
        with pytest.raises(Fault):
            begin_source_read()
    # Untagged work, including writes and sync, has no cancellation checkpoint.
    with source_read_lock(lock):
        begin_source_read()


@pytest.mark.parametrize("bundle", [False, True])
def test_cancel_after_successful_acquire_still_hits_the_locked_guard(bundle):
    abandoned = False

    class CancelOnAcquire(ObservedLock):
        def acquire(self, *args, **kwargs):
            nonlocal abandoned
            acquired = super().acquire(*args, **kwargs)
            if acquired:
                abandoned = True
            return acquired

    lock = CancelOnAcquire(threading.RLock())
    authority = object.__new__(SourceAuthority)
    authority._operation_order = SourceOperationOrder()
    authority._operation_lock, authority._bundled = lock, threading.local()

    def forbidden():
        pytest.fail("Cancelled acquired read opened a transaction")

    service = SimpleNamespace(store=SimpleNamespace(transaction=forbidden))
    with cancellable_read(lambda: abandoned):
        with pytest.raises(Fault) as stopped:
            with (
                authority.bundle_operation(service, [dict(scope={})])
                if bundle
                else authority.operation(service, {})
            ):
                pytest.fail("Cancelled acquired read reached business work")
    assert lock.acquired.is_set()
    assert (stopped.value.code, stopped.value.status) == ("timeout", 408)
    # A second thread can take the same lock: the cancelled acquisition released it.
    released = threading.Event()

    def probe():
        if lock.lock.acquire(timeout=1):
            lock.lock.release()
            released.set()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive() and released.is_set()
