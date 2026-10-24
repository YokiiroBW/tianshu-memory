"""Local authority writers share the live source boundary and real guarded Store.

The case functions accept the existing SyncHarness interface, including pg_sync
in the native runner. No alternate transaction or source authority is installed.
"""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest
from test_audit_runtime import until
from test_memory_context import ProofIssuer
from test_memory_read_cancellation import ObservedLock, client_for, read_request
from test_memory_v2 import candidate, setup
from test_memory_v2_domains import private_conversation
from test_observations import Verifier, event, source
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.app import create_app
from tianshu_memory.candidate_extraction import CandidateExtraction
from tianshu_memory.domain import Fault, utc
from tianshu_memory.observations import ObservationLedger
from tianshu_memory.qq_identity import observe_alias
from tianshu_memory.workflow import LocalWorkflow, TrustedWorkflow

WRITERS = (
    "register",
    "observation",
    "candidate_schema",
    "qq_alias",
    "association",
    "legacy_link",
    "rejected_receipt",
    "acknowledge",
)


def revision(h):
    with h.store.transaction() as db:
        return h.service.source_authority.revision(db)


def registration(h, *, new=False):
    context = copy.deepcopy(h.contexts["synthetic-viewer:0"])
    account = h.account
    if new:
        account = dict(namespace="qq", immutable_account_id="77880011")
        context.update(
            verified_account=account, allowed_scope=dict(context["allowed_scope"], person_id=None)
        )
    return dict(command=h.command(), account=account, display_name="协调后的名字"), context


def local_writer(h, kind, monkeypatch):
    """Build genuine public-service commands before concurrency begins."""
    context = h.contexts["synthetic-viewer:0"]
    if kind == "register":
        request, context = registration(h)

        def check(value):
            assert value["person_id"] == h.person and value["created"] is False
            with h.store.transaction() as db:
                assert h.service._binding(db, h.account)["display_name"] == request["display_name"]

        return lambda: h.service.register(request, context), check

    if kind in {"observation", "qq_alias"}:
        h.store.migrate_qq_aliases(h.directory / "local-writers-before-alias.sqlite")
    if kind == "observation":
        h.store.migrate_observations(h.directory / "local-writers-before-observation.sqlite")
        value = event()
        value.update(schema_version=3, nickname="观察昵称", group_card="观察名片")
        request = source(value)
        verifier = Verifier()
        verifier.values[request["source_ref"]] = request
        ledger = ObservationLedger(h.store, verifier, local_commit=h.service.local_commit)

        def check(value):
            assert value["state"] == "accepted"
            with h.store.transaction() as db:
                assert db.execute("SELECT count(*) FROM observation_sources").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM qq_aliases").fetchone()[0] == 2
                account = dict(namespace="qq", immutable_account_id=request["event"]["account_id"])
                assert h.service._binding(db, account) is not None

        return lambda: ledger.ingest("companion", request), check

    if kind == "candidate_schema":
        job, _ = candidate(h, "我喜欢大麦茶。", capture=False)
        request = dict(job_id=job, output=None, supported_extraction_schemas=[3, 4])
        publisher = dict(authenticated_service="companion", allowed_scopes=[h.scope()])

        def check(value):
            assert value["state"] == "pending" and value["extraction_schema"] == 4
            with h.store.transaction() as db:
                assert (
                    db.execute("SELECT extraction_schema FROM jobs WHERE id=?", (job,)).fetchone()[
                        0
                    ]
                    == 4
                )

        return lambda: CandidateExtraction(h.service).execute(request, publisher), check

    if kind == "qq_alias":
        request, context = registration(h, new=True)
        h.service.register(request, context)
        alias = dict(
            schema_version=1,
            request_id="local-writer-alias",
            account_id="77880011",
            bot_id="4242",
            conversation_id="group:123",
            nickname="昵称",
            group_card="名片",
            event_ref="writer:alias",
            observed_at="2026-10-24T00:00:00Z",
        )

        def check(value):
            assert value["deduplicated"] is False
            with h.store.transaction() as db:
                assert (
                    db.execute(
                        "SELECT count(*) FROM qq_alias_events WHERE event_ref=?",
                        (alias["event_ref"],),
                    ).fetchone()[0]
                    == 1
                )

        return lambda: observe_alias(h.store, alias, local_commit=h.service.local_commit), check

    if kind in {"association", "legacy_link"}:
        if h.scope()["audience"] != "self_private":
            pytest.skip("Associations require the existing private endpoint pair")
        other = private_conversation(h, "private:local-writer-peer", same_person=False)
        application = h.service.memory_context
        proof = ProofIssuer()
        application.proofs = proof
        accounts = [h.account, h.physicals[other]["author"]]
        scopes = [h.scope(), h.scope(other)]
        if kind == "association":
            request = dict(
                command=h.command(),
                action="link",
                source_account=accounts[0],
                target_account=accounts[1],
                association_id=None,
                expected_version=0,
                scopes=scopes,
                proof_ref="writer:association",
            )
            proof.issue(request, "association", accounts, scopes)

            def action():
                return application.association(request, context)
        else:
            request = dict(
                command=h.command(),
                source_account=accounts[0],
                target_account=accounts[1],
                source_binding_version=1,
                target_binding_version=1,
                verification_ref="writer:legacy-link",
            )
            proof.issue(
                dict(request, proof_ref=request["verification_ref"]),
                "association",
                accounts,
                scopes,
            )

            def action():
                return application.legacy_link(request, context)

        def check(value):
            with h.store.transaction() as db:
                rows = db.execute(
                    "SELECT state,source_person,target_person FROM context_associations"
                ).fetchall()
            assert len(rows) == 1 and rows[0]["state"] == "linked"
            assert [rows[0]["source_person"], rows[0]["target_person"]] == [
                s["person_id"] for s in scopes
            ]

        return action, check

    if kind == "rejected_receipt":
        request = dict(
            command=h.command(),
            scope=h.scope(),
            batch_ref=None,
            item_id="writer:rejected",
            kind="no_op",
            target=dict(
                category="evidence",
                field_key="writer.test",
                item_key=None,
                record_id=None,
                expected_version=None,
            ),
            units=[],
            evidence_refs=[],
            proof_ref=None,
        )
        original = h.service.operation

        @contextmanager
        def conflict(**kwargs):
            # Exercise the independent post-rollback receipt path. The reader
            # still uses the actual full SourceAuthority barrier and Store.
            if threading.current_thread().name == "local-writer":
                raise Fault("version_conflict", 409)
            with original(**kwargs) as db:
                yield db

        monkeypatch.setattr(h.service, "operation", conflict)

        def check(value):
            assert value["state"] == "rejected" and value["error_code"] == "version_conflict"
            with h.store.transaction() as db:
                assert db.execute("SELECT count(*) FROM context_operations").fetchone()[0] == 1
                assert (
                    db.execute(
                        "SELECT count(*) FROM outbox WHERE kind='memory.context_item_completed'"
                    ).fetchone()[0]
                    == 1
                )

        return lambda: h.service.memory_context.propose(request, context), check

    assert kind == "acknowledge"
    h.seed()
    workflow = TrustedWorkflow(h.service)
    identity = workflow.outbox()[0]["event_id"]

    def check(value):
        with h.store.transaction() as db:
            assert (
                db.execute(
                    "SELECT acknowledged FROM outbox WHERE event_id=?", (identity,)
                ).fetchone()[0]
                == 1
            )

    return lambda: workflow.acknowledge(identity), check


@pytest.mark.parametrize("kind", WRITERS)
def test_local_writer_waits_outside_its_transaction_until_the_fresh_source_business_commits(
    sync, monkeypatch, kind
):
    sync.store = sync.service.store
    setup(sync)
    action, check = local_writer(sync, kind, monkeypatch)
    authority = sync.service.source_authority
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    events, reader_barriers, active_writer_transactions = [], [], set()
    physical = []

    class PhysicalLock(ObservedLock):
        def acquire(self, *args, **kwargs):
            acquired = super().acquire(*args, **kwargs)
            if acquired:
                physical.append((threading.current_thread().name, "acquire"))
            return acquired

        def release(self):
            assert authority._operation_order._owner == threading.get_ident()
            assert threading.get_ident() not in active_writer_transactions
            physical.append((threading.current_thread().name, "release"))
            super().release()

    monkeypatch.setattr(authority, "_operation_lock", PhysicalLock(authority._operation_lock))
    original_mutate, original_tx, original_barrier = (
        sync.mutate,
        sync.store.transaction,
        authority.barrier,
    )
    condition = authority._operation_order._condition
    original_wait = condition.wait

    def wait(timeout=None):
        if threading.current_thread().name == "local-writer":
            assert threading.get_ident() not in active_writer_transactions
            queued.set()
        return original_wait(timeout=timeout)

    def hold(kind, request, response):
        original_mutate(kind, request, response)
        if kind == "current" and request.get("viewer") and not entered.is_set():
            entered.set()
            assert release.wait(5)

    @contextmanager
    def transaction():
        thread = threading.get_ident()
        writer = threading.current_thread().name == "local-writer"
        if writer:
            active_writer_transactions.add(thread)
            if authority._operation_order._owner == thread:
                events.append("writer-transaction")
        try:
            with original_tx() as db:
                yield db
            if writer and authority._operation_order._owner == thread:
                events.append("writer-commit-finished")
        finally:
            if writer:
                active_writer_transactions.remove(thread)

    def barrier(*args, **kwargs):
        if threading.current_thread().name == "source-reader":
            reader_barriers.append(1)
        return original_barrier(*args, **kwargs)

    def reader():
        threading.current_thread().name = "source-reader"
        with sync.service.operation(
            scope=sync.scope(), context=sync.contexts["synthetic-viewer:0"]
        ):
            events.append("reader-business")

    def writer():
        threading.current_thread().name = "local-writer"
        return action()

    monkeypatch.setattr(condition, "wait", wait)
    monkeypatch.setattr(sync.store, "transaction", transaction)
    monkeypatch.setattr(authority, "barrier", barrier)
    sync.mutate = hold
    before = revision(sync)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(reader)
        try:
            assert entered.wait(3)
            write = pool.submit(writer)
            assert queued.wait(3)
            assert events == [] and not write.done()
        finally:
            release.set()
        read.result(timeout=10)
        result = write.result(timeout=10)
    assert events[0] == "reader-business" and "writer-transaction" in events[1:]
    assert reader_barriers == [1]
    assert ("local-writer", "acquire") in physical
    assert ("local-writer", "release") in physical
    assert revision(sync) > before
    check(result)


@pytest.mark.parametrize("failure", ["body", "checkpoint"])
def test_register_rollback_restores_authority_and_releases_local_ownership(
    sync, monkeypatch, failure
):
    from tianshu_memory import source_recovery

    request, context = registration(sync, new=True)
    before, guard = revision(sync), sync.store.recovery_path.read_bytes()
    original = sync.service._idempotent
    original_persist = source_recovery.persist

    def fail_after_real_action(db, operation, payload, resolved, action):
        action()
        raise RuntimeError("synthetic failure after real identity inserts")

    def fail_before_checkpoint_write(*args, **kwargs):
        assert sync.service.source_authority._operation_order._owner == threading.get_ident()
        raise RuntimeError("synthetic checkpoint failure after real identity inserts")

    if failure == "body":
        monkeypatch.setattr(sync.service, "_idempotent", fail_after_real_action)
    else:
        monkeypatch.setattr(source_recovery, "persist", fail_before_checkpoint_write)
    with pytest.raises(RuntimeError, match="real identity"):
        sync.service.register(request, context)
    assert revision(sync) == before and sync.store.recovery_path.read_bytes() == guard
    with sync.store.transaction() as db:
        assert sync.service._binding(db, request["account"]) is None
    assert sync.service.source_authority._operation_order._owner is None
    monkeypatch.setattr(sync.service, "_idempotent", original)
    monkeypatch.setattr(source_recovery, "persist", original_persist)
    first = sync.service.register(request, context)
    assert first["created"] is True
    assert sync.service.register(request, context) == first


@pytest.mark.parametrize("expires", ["command", "context"])
def test_queued_registration_rechecks_its_real_deadline_and_context(sync, monkeypatch, expires):
    request, context = registration(sync)
    initial = sync.service.clock()
    if expires == "command":
        request["command"]["deadline_at"] = utc(initial + timedelta(seconds=1))
    else:
        context["expires_at"] = utc(initial + timedelta(seconds=1))
    authority = sync.service.source_authority
    condition, queued = authority._operation_order._condition, threading.Event()
    original_wait = condition.wait

    def wait(timeout=None):
        queued.set()
        return original_wait(timeout=timeout)

    monkeypatch.setattr(condition, "wait", wait)
    before, guard = revision(sync), sync.store.recovery_path.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with sync.service.local_commit():
            future = pool.submit(sync.service.register, request, context)
            assert queued.wait(3)
            monkeypatch.setattr(sync.service, "clock", lambda: initial + timedelta(seconds=2))
        with pytest.raises(Fault) as error:
            future.result(timeout=5)
    assert error.value.code == ("timeout" if expires == "command" else "forbidden")
    assert revision(sync) == before and sync.store.recovery_path.read_bytes() == guard
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM requests WHERE key=?",
                (request["command"]["idempotency_key"],),
            ).fetchone()[0]
            == 0
        )


def test_queued_http_registration_keeps_unknown_and_admission_until_late_commit(sync, monkeypatch):
    request, _ = registration(sync)
    authority = sync.service.source_authority
    observed = ObservedLock(authority._operation_lock)
    monkeypatch.setattr(authority, "_operation_lock", observed)
    condition, queued = authority._operation_order._condition, threading.Event()
    original_wait = condition.wait

    def wait(timeout=None):
        queued.set()
        return original_wait(timeout=timeout)

    monkeypatch.setattr(condition, "wait", wait)

    async def scenario():
        app = create_app(
            service=sync.service, auth=sync.client.app.state.auth, execute_timeout=1, max_active=1
        )
        async with client_for(app) as client:
            with sync.service.local_commit():
                pending = asyncio.create_task(
                    client.post("/internal/v1/identity/register", json=request)
                )
                await until(queued.is_set)
                response = await pending
                assert (
                    response.status_code == 408 and response.json()["execution_state"] == "unknown"
                )
                assert response.json()["retryable"] is False and app.state.chat_requests.active == 1
            await until(lambda: app.state.chat_requests.active == 0)
            first = await client.post("/internal/v1/identity/register", json=request)
            replay = await client.post("/internal/v1/identity/register", json=request)
            assert first.status_code == replay.status_code == 200
            assert first.json() == replay.json()
        assert observed.acquired.is_set()

    asyncio.run(scenario())
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM requests WHERE key=?",
                (request["command"]["idempotency_key"],),
            ).fetchone()[0]
            == 1
        )


def test_queued_http_query_can_cancel_while_a_local_writer_still_owns_its_transaction(
    sync, monkeypatch
):
    data = read_request(sync, "query")
    authority = sync.service.source_authority
    condition, queued = authority._operation_order._condition, threading.Event()
    original_wait, original_barrier = condition.wait, authority.barrier
    original_transaction = sync.service.store.transaction
    holder = threading.get_ident()
    barriers, transactions_after_queue = [], []

    def wait(timeout=None):
        assert timeout == 0.1
        queued.set()
        return original_wait(timeout=timeout)

    def barrier(*args, **kwargs):
        barriers.append(1)
        return original_barrier(*args, **kwargs)

    @contextmanager
    def transaction(*args, **kwargs):
        if threading.get_ident() != holder and queued.is_set():
            transactions_after_queue.append(1)
        with original_transaction(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(condition, "wait", wait)
    monkeypatch.setattr(authority, "barrier", barrier)
    monkeypatch.setattr(sync.service.store, "transaction", transaction)
    sync.calls.clear()

    async def scenario():
        app = create_app(
            service=sync.service, auth=sync.client.app.state.auth, execute_timeout=1, max_active=1
        )
        async with client_for(app) as client:
            # Real origin/local authorization must finish before this test's
            # writer owns its DB transaction. Hold only the source gate first.
            with authority._operation_order.turn(), authority._operation_lock:
                pending = asyncio.create_task(
                    client.post("/internal/v2/memory/context/query", json=data)
                )
                await until(queued.is_set)
                # Reenter as the same owner, opening the real guarded writer
                # transaction while the already queued reader is cancelled.
                with sync.service.local_commit():
                    response = await pending
                    assert response.status_code == 408
                    assert (
                        response.json()["execution_state"] == "not_started"
                        and response.json()["retryable"] is True
                    )
                    await until(lambda: app.state.chat_requests.active == 0)
                    assert barriers == [] and transactions_after_queue == []
                    assert not authority._operation_order._waiters
            assert [kind for kind, _, _ in sync.calls if kind != "origin"] == []

    asyncio.run(scenario())


def test_fixture_workflow_acknowledge_keeps_its_existing_local_store_transaction(h):
    job, _ = h.candidate()
    h.workflow.commit_candidate(job, [h.draft()])
    workflow = LocalWorkflow(h.service)
    identity = workflow.outbox()[0]["event_id"]
    assert not h.service.synchronized
    workflow.acknowledge(identity)
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT acknowledged FROM outbox WHERE event_id=?", (identity,)).fetchone()[
                0
            ]
            == 1
        )
