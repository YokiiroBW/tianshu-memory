"""Bounded incremental projection maintenance using the existing document ledger."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager

from .domain import Fault
from .memory_reindex import MemoryReindexer


def attach(app, service):
    previous = app.router.lifespan_context
    previous_drain = app.state.drain
    stopped = asyncio.Event()
    running = {}

    async def quiesce():
        stopped.set()

    async def drain():
        await quiesce()
        task = running.get("task")
        try:
            if task is not None:
                await asyncio.shield(task)
        except Exception:
            await previous_drain()
            raise
        await previous_drain()

    app.state.quiesce = quiesce
    app.state.drain = drain

    @asynccontextmanager
    async def lifespan(application):
        stopped.clear()

        async def maintain():
            indexer = MemoryReindexer(service)
            while not stopped.is_set():
                try:
                    result = await asyncio.to_thread(indexer.index_step, batch_size=4)
                except (
                    Fault,
                    OSError,
                    TimeoutError,
                    sqlite3.OperationalError,
                    ValueError,
                    KeyError,
                    TypeError,
                ):
                    result = {"state": "unavailable", "indexed": 0}
                application.state.memory_index = result
                delay = 0.1 if result["state"] == "pending" else 5
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=delay)
                except TimeoutError:
                    pass

        async with previous(application) as state:
            task = asyncio.create_task(maintain())
            running["task"] = task
            try:
                yield state
            finally:
                try:
                    await drain()
                finally:
                    close = getattr(service.retrieval.backend, "close", None)
                    if close is not None:
                        await asyncio.to_thread(close)

    app.router.lifespan_context = lifespan
