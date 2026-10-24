"""Outer source boundaries keep FIFO position without changing the physical RLock."""

import threading
from collections import defaultdict
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tianshu_memory.domain import Fault
from tianshu_memory.read_lifecycle import cancellable_read
from tianshu_memory.source_authority import SourceAuthority
from tianshu_memory.source_order import SourceOperationOrder


class PhysicalLock:
    def __init__(self, harness):
        self.harness = harness
        self.lock = threading.RLock()
        self.acquisitions = []

    def acquire(self, *args, **kwargs):
        name = threading.current_thread().name
        if self.harness.failure == (name, "acquire"):
            raise RuntimeError("physical acquire failed")
        acquired = self.lock.acquire(*args, **kwargs)
        if acquired:
            self.acquisitions.append(name)
        return acquired

    def release(self):
        # Ordering ownership must still be live during every physical release.
        assert self.harness.authority._operation_order._owner == threading.get_ident()
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class Harness:
    def __init__(self):
        self.authority = object.__new__(SourceAuthority)
        self.authority._operation_order = SourceOperationOrder()
        self.authority._operation_lock = PhysicalLock(self)
        self.authority._bundled = threading.local()
        self.authority._operation = self.operation
        self.authority.coverage = lambda *args: []
        self.service = SimpleNamespace(store=SimpleNamespace(transaction=self.transaction))
        self.failure = None
        self.entered, self.started, self.errors, self.threads = [], [], [], []
        self.waiting = defaultdict(threading.Event)
        self.polled = defaultdict(threading.Event)
        self.waits = defaultdict(list)
        condition = self.authority._operation_order._condition
        original_wait = condition.wait

        def wait(timeout=None):
            name = threading.current_thread().name
            self.waits[name].append(
                (timeout, tuple(id(ticket) for ticket in self.authority._operation_order._waiters))
            )
            self.waiting[name].set()
            if len(self.waits[name]) >= 3:
                self.polled[name].set()
            return original_wait(timeout=timeout)

        condition.wait = wait

    @contextmanager
    def transaction(self):
        yield object()

    @contextmanager
    def operation(self, service, scope, **kwargs):
        name = threading.current_thread().name
        self.started.append(name)
        if self.failure == (name, "barrier"):
            raise RuntimeError("source barrier failed")
        yield object()

    def boundary(self, bundle=False):
        if bundle:
            return self.authority.bundle_operation(self.service, [dict(scope={})])
        return self.authority.operation(self.service, {})

    def work(self, bundle=False):
        with self.boundary(bundle):
            name = threading.current_thread().name
            self.entered.append(name)
            if self.failure == (name, "body"):
                raise RuntimeError("business failed")

    def spawn(self, name, action):
        def run():
            try:
                action()
            except BaseException as error:
                self.errors.append((name, error))

        thread = threading.Thread(target=run, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def queued(self, name, *, polled=False):
        assert (self.polled if polled else self.waiting)[name].wait(3)

    def finish(self):
        for thread in self.threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in self.threads)


@pytest.fixture
def harness():
    value = Harness()
    try:
        yield value
    finally:
        value.finish()


@pytest.mark.parametrize("bundle", [False, True])
def test_outer_entries_keep_one_fifo_ticket_across_cancellation_polls(harness, bundle):
    h = harness

    def reader():
        with cancellable_read(lambda: False):
            h.work(bundle)

    with h.boundary():
        h.spawn("reader", reader)
        h.queued("reader")
        first_ticket = h.waits["reader"][0][1][0]
        h.spawn("writer", lambda: h.work(not bundle))
        h.queued("writer")
        h.queued("reader", polled=True)
        assert all(queue[0] == first_ticket for _, queue in h.waits["reader"])
        assert all(timeout == 0.1 for timeout, _ in h.waits["reader"])
        assert all(timeout is None for timeout, _ in h.waits["writer"])
        assert h.entered == []
    h.finish()
    assert h.errors == []
    assert h.entered == ["reader", "writer"]
    assert h.authority._operation_lock.acquisitions[-2:] == ["reader", "writer"]


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("middle", [False, True])
def test_cancelled_head_or_middle_leaves_fifo_while_owner_still_holds_lock(harness, bundle, middle):
    h = harness
    cancelled, finished = threading.Event(), threading.Event()

    def reader():
        try:
            with cancellable_read(cancelled.is_set):
                h.work(bundle)
        finally:
            finished.set()

    with h.boundary():
        if middle:
            h.spawn("earlier", lambda: h.work(not bundle))
            h.queued("earlier")
        h.spawn("cancelled", reader)
        h.queued("cancelled")
        h.spawn("later", lambda: h.work(not bundle))
        h.queued("later")
        cancelled.set()
        assert finished.wait(3)
        assert h.entered == []
        assert "cancelled" not in h.started
        assert "cancelled" not in h.authority._operation_lock.acquisitions
    h.finish()
    assert len(h.errors) == 1
    assert h.errors[0][0] == "cancelled"
    assert isinstance(h.errors[0][1], Fault) and h.errors[0][1].code == "timeout"
    assert h.entered == (["earlier", "later"] if middle else ["later"])
    assert not h.authority._operation_order._waiters
    assert h.authority._operation_order._owner is None


def test_same_thread_outer_reentry_does_not_queue_behind_its_waiter(harness):
    h = harness
    order = h.authority._operation_order
    with h.boundary():
        h.spawn("other", h.work)
        h.queued("other")
        with h.boundary():
            assert order._owner == threading.get_ident() and order._depth == 2
            assert h.entered == []
        assert order._depth == 1 and h.entered == []
    h.finish()
    assert h.errors == [] and h.entered == ["other"]
    assert order._owner is None and order._depth == 0


@pytest.mark.parametrize("bundle", [False, True])
@pytest.mark.parametrize("stage", ["acquire", "barrier", "body"])
def test_failed_selected_owner_releases_order_and_physical_lock(harness, bundle, stage):
    h = harness
    h.failure = ("failure", stage)
    with h.boundary():
        h.spawn("failure", lambda: h.work(bundle))
        h.queued("failure")
        h.spawn("successor", lambda: h.work(not bundle))
        h.queued("successor")
    h.finish()
    assert len(h.errors) == 1 and h.errors[0][0] == "failure"
    assert isinstance(h.errors[0][1], RuntimeError)
    assert h.entered[-1] == "successor"
    assert h.authority._operation_order._owner is None
    assert not h.authority._operation_order._waiters
    # A subsequent boundary and its nested reentry also remain usable.
    with h.boundary():
        with h.boundary():
            pass


@pytest.mark.parametrize("bundle", [False, True])
def test_cancel_after_order_reservation_cleans_owner_before_physical_read(harness, bundle):
    h = harness
    order = h.authority._operation_order
    with cancellable_read(lambda: order._owner == threading.get_ident()):
        with pytest.raises(Fault) as stopped:
            h.work(bundle)
    assert stopped.value.code == "timeout"
    assert h.started == [] and h.authority._operation_lock.acquisitions == []
    assert order._owner is None and not order._waiters
    h.spawn("successor", lambda: h.work(not bundle))
    h.finish()
    assert h.errors == [] and h.entered == ["successor"]


@pytest.mark.parametrize("bundle", [False, True])
def test_started_second_boundary_gets_a_fresh_tail_position_without_cancellation(harness, bundle):
    h = harness
    first_done, second_ready = threading.Event(), threading.Event()
    abandoned = threading.Event()

    def started():
        with cancellable_read(abandoned.is_set):
            with h.boundary(bundle):
                pass
            first_done.set()
            assert second_ready.wait(3)
            h.work(bundle)

    h.spawn("started", started)
    assert first_done.wait(3)
    with h.boundary():
        h.spawn("earlier", lambda: h.work(not bundle))
        h.queued("earlier")
        abandoned.set()
        second_ready.set()
        h.queued("started")
        assert all(timeout is None for timeout, _ in h.waits["started"])
    h.finish()
    assert h.errors == [] and h.entered == ["earlier", "started"]
