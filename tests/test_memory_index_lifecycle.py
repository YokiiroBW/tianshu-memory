"""The projection worker retries temporary storage failures and exits cleanly."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tianshu_storage.errors import StorageError

from tianshu_memory import memory_index_lifecycle


@pytest.fixture
def lifecycle(monkeypatch):
    calls, closed = [], []

    @asynccontextmanager
    async def previous(app):
        yield None

    async def drain():
        pass

    app = SimpleNamespace(
        router=SimpleNamespace(lifespan_context=previous), state=SimpleNamespace(drain=drain)
    )
    service = SimpleNamespace(
        retrieval=SimpleNamespace(backend=SimpleNamespace(close=lambda: closed.append(True)))
    )

    async def wait_for(value, *, timeout):
        return await asyncio.wait_for(value, timeout=min(timeout, 0.01))

    monkeypatch.setattr(
        memory_index_lifecycle,
        "asyncio",
        SimpleNamespace(
            Event=asyncio.Event,
            to_thread=asyncio.to_thread,
            create_task=asyncio.create_task,
            shield=asyncio.shield,
            wait_for=wait_for,
        ),
    )
    return app, service, calls, closed


def test_storage_outage_recovers_and_does_not_break_shutdown(lifecycle, monkeypatch):
    app, service, calls, closed = lifecycle

    class Indexer:
        def __init__(self, service):
            pass

        def index_step(self, *, batch_size):
            calls.append(batch_size)
            if len(calls) == 1:
                raise StorageError("synthetic pool unavailable")
            return {"state": "complete", "indexed": 0}

    monkeypatch.setattr(memory_index_lifecycle, "MemoryReindexer", Indexer)
    memory_index_lifecycle.attach(app, service)

    async def run():
        async with app.router.lifespan_context(app):

            async def recovered():
                while getattr(app.state, "memory_index", {}).get("state") != "complete":
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(recovered(), timeout=2)

    asyncio.run(run())
    assert len(calls) >= 2
    assert closed == [True]


def test_programming_error_is_not_swallowed(lifecycle, monkeypatch):
    app, service, calls, closed = lifecycle

    class Indexer:
        def __init__(self, service):
            pass

        def index_step(self, *, batch_size):
            calls.append(batch_size)
            raise RuntimeError("synthetic programming failure")

    monkeypatch.setattr(memory_index_lifecycle, "MemoryReindexer", Indexer)
    memory_index_lifecycle.attach(app, service)

    async def run():
        async with app.router.lifespan_context(app):

            async def started():
                while not calls:
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(started(), timeout=2)

    with pytest.raises(RuntimeError, match="synthetic programming failure"):
        asyncio.run(run())
    assert closed == [True]
