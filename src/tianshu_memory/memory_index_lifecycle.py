"""Bounded incremental projection maintenance using the existing document ledger."""

import asyncio
from contextlib import asynccontextmanager

from .domain import Fault
from .memory_reindex import MemoryReindexer


def attach(app, service):
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        stopped = asyncio.Event()

        async def maintain():
            indexer = MemoryReindexer(service)
            while not stopped.is_set():
                try:
                    result = await asyncio.to_thread(indexer.index_step, batch_size=4)
                except (Fault, OSError, TimeoutError, ValueError, KeyError, TypeError):
                    result = {"state": "unavailable", "indexed": 0}
                application.state.memory_index = result
                delay = 0.1 if result["state"] == "pending" else 5
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=delay)
                except TimeoutError:
                    pass

        async with previous(application) as state:
            task = asyncio.create_task(maintain())
            try:
                yield state
            finally:
                stopped.set()
                close = getattr(service.retrieval.backend, "close", None)
                if close is not None:
                    await asyncio.to_thread(close)
                await task

    app.router.lifespan_context = lifespan
