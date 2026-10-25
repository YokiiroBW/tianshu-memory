"""One existing request pool reserves identity headroom without releasing late work."""

import asyncio
import json
import threading

import anyio
import pytest
from starlette.concurrency import run_in_threadpool
from test_audit_runtime import Driver, payload, until

from tianshu_memory.app import create_app
from tianshu_memory.chat_requests import Requests
from tianshu_memory.domain import Fault


class RouteDriver(Driver):
    def __init__(self, app, path):
        async def route(scope, receive, send):
            await app(dict(scope, path=path, raw_path=path.encode()), receive, send)

        super().__init__(route)


@pytest.mark.parametrize("limit", [1, 4, 8])
def test_unreserved_requests_keep_their_original_total_capacity(limit):
    requests = Requests(limit)
    leases = [requests.claim() for _ in range(limit)]
    assert requests.active == requests.ordinary_active == limit
    with pytest.raises(Fault, match="queue_full"):
        requests.claim(identity=True)
    for lease in leases:
        lease.finish()
        lease.release()
    assert requests.active == requests.ordinary_active == 0
    identity = requests.claim(identity=True)
    ordinary = [requests.claim() for _ in range(limit - 1)]
    assert requests.active == limit
    for lease in [identity, *ordinary]:
        lease.finish()
    assert requests.active == requests.ordinary_active == 0


@pytest.mark.parametrize("limit", [1, 2, 8])
def test_reserved_requests_keep_small_limits_and_the_same_total_bound(limit):
    requests = Requests(limit, reserve_identity=True)
    ordinary = [requests.claim() for _ in range(max(1, limit - 1))]
    with pytest.raises(Fault, match="queue_full"):
        requests.claim()
    if limit == 1:
        with pytest.raises(Fault, match="queue_full"):
            requests.claim(identity=True)
        identity = []
    else:
        identity = [requests.claim(identity=True)]
        assert requests.active == limit
        with pytest.raises(Fault, match="queue_full"):
            requests.claim(identity=True)
    for lease in [*identity, *ordinary]:
        lease.finish()
        lease.finish()
        lease.release()
    assert requests.active == requests.ordinary_active == 0


def test_authentication_and_business_runs_keep_one_classified_lease():
    async def scenario():
        requests = Requests(8, reserve_identity=True)
        lease = requests.claim()
        for business in (False, False, True):
            observed = await lease.run(
                lambda: (requests.active, requests.ordinary_active), business=business
            )
            assert observed == (1, 1)
            assert requests.active == requests.ordinary_active == 1
        assert lease.business_started
        lease.finish()
        assert requests.active == requests.ordinary_active == 0

    asyncio.run(scenario())


def test_actual_http_identity_uses_eighth_slot_while_timed_out_normal_worker_still_runs(
    h, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    register, resolve = h.service.register, h.service.resolve
    calls, observed = [], []

    def slow_register(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return register(*args)

    monkeypatch.setattr(h.service, "register", slow_register)

    async def scenario():
        app = create_app(service=h.service, auth=h.auth, execute_timeout=0.15, body_timeout=5)
        requests = app.state.chat_requests
        claim = requests.claim
        leases = []

        def remember(**kwargs):
            lease = claim(**kwargs)
            leases.append(lease)
            return lease

        monkeypatch.setattr(requests, "claim", remember)

        def identity(*args):
            observed.append((requests.active, requests.ordinary_active))
            return resolve(*args)

        monkeypatch.setattr(h.service, "resolve", identity)
        ordinary_path = "/internal/v1/identity/register"
        slow = RouteDriver(app, ordinary_path)
        slow.feed(json.dumps(dict(command=h.command(), account=h.account)).encode())
        slow_task = asyncio.create_task(slow.run())
        waiting, tasks = [], []
        try:
            await until(entered.is_set)
            waiting = [RouteDriver(app, ordinary_path) for _ in range(6)]
            tasks = [asyncio.create_task(driver.run()) for driver in waiting]
            await until(lambda: all(driver.reads for driver in waiting))
            assert requests.active == requests.ordinary_active == 7
            status, body = await slow_task
            assert status == 408 and body["execution_state"] == "unknown"
            assert body["retryable"] is False and requests.active == requests.ordinary_active == 7
            rejected = RouteDriver(app, ordinary_path)
            assert (await rejected.run())[0] == 429 and rejected.reads == 0

            lookup = Driver(app)
            lookup.feed(payload(h))
            status, body = await lookup.run()
            assert status == 200 and body["person_id"] == h.person
            assert observed == [(8, 7)]
            assert requests.active == requests.ordinary_active == 7

            # Identity has no separate pool: its held body consumes the last
            # original total slot, so a ninth identity is refused as well.
            held_identity = Driver(app)
            tasks.append(asyncio.create_task(held_identity.run()))
            await until(lambda: held_identity.reads > 0)
            assert requests.active == 8 and requests.ordinary_active == 7
            ninth = Driver(app)
            assert (await ninth.run())[0] == 429 and ninth.reads == 0
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert requests.active == requests.ordinary_active == 1
            assert len(calls) == 1  # The timed-out register still owns both counters.
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release.set()
            await asyncio.gather(slow_task, return_exceptions=True)
            await until(lambda: requests.active == 0)
        assert requests.ordinary_active == 0
        for lease in leases:
            lease.finish()
            lease.release()
        assert requests.active == requests.ordinary_active == 0
        recovered = RouteDriver(app, ordinary_path)
        recovered.feed(json.dumps(dict(command=h.command(), account=h.account)).encode())
        assert (await recovered.run())[0] == 200
        assert requests.active == requests.ordinary_active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_unstarted_normal_worker_keeps_both_counters_until_its_actual_exit(ending):
    entered, release = threading.Event(), threading.Event()
    called = []

    def block_thread():
        entered.set()
        assert release.wait(5)

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        blocker = asyncio.create_task(run_in_threadpool(block_thread))
        requests = Requests(2, reserve_identity=True)
        lease = requests.claim()

        async def queued():
            if ending == "timeout":
                async with asyncio.timeout(0.05):
                    await lease.run(lambda: called.append(1), business=True)
            else:
                await lease.run(lambda: called.append(1), business=True)

        pending, identity = None, None
        try:
            await until(entered.is_set)
            pending = asyncio.create_task(queued())
            await until(lambda: limiter.statistics().tasks_waiting == 1)
            if ending == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                with pytest.raises(TimeoutError):
                    await pending
            lease.finish()
            assert requests.active == requests.ordinary_active == 1
            assert not lease.business_started and called == []
            identity = requests.claim(identity=True)
            assert requests.active == 2 and requests.ordinary_active == 1
            with pytest.raises(Fault, match="queue_full"):
                requests.claim()
            identity.finish()
        finally:
            release.set()
            try:
                await blocker
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                if identity is not None:
                    identity.finish()
                lease.finish()
                await until(lambda: requests.active == 0)
            finally:
                limiter.total_tokens = previous
        assert requests.ordinary_active == 0 and not lease.business_started and called == []
        with pytest.raises(Fault, match="timeout"):
            await lease.task
        lease.release()
        assert requests.active == requests.ordinary_active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    [
        "identity/register",
        "identity/link",
        "memory/select",
        "memory/candidates/process",
        "/internal/v2/memory/context/query",
    ],
)
def test_only_the_identity_resolve_route_can_use_reserved_capacity(h, path):
    async def scenario():
        app = create_app(service=h.service, auth=h.auth)
        requests = app.state.chat_requests
        held = [requests.claim() for _ in range(7)]
        try:
            route = path if path.startswith("/") else "/internal/v1/" + path
            ordinary = RouteDriver(app, route)
            assert (await ordinary.run())[0] == 429 and ordinary.reads == 0
            lookup = Driver(app)
            lookup.feed(payload(h))
            status, body = await lookup.run()
            assert status == 200 and body["person_id"] == h.person
            assert requests.active == requests.ordinary_active == 7
        finally:
            for lease in held:
                lease.finish()
        assert requests.active == requests.ordinary_active == 0

    asyncio.run(scenario())
