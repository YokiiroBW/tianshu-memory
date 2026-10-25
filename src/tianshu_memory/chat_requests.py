"""Admission ownership outlives a timed-out response while its worker still runs."""

import asyncio
from contextlib import nullcontext

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .domain import Fault
from .read_lifecycle import cancellable_read


class Requests:
    def __init__(self, limit=8, *, reserve_identity=False):
        self.limit, self.active = limit, 0
        self.ordinary_limit = max(1, limit - 1) if reserve_identity else limit
        self.ordinary_active = 0

    def claim(self, *, identity=False):
        if self.active >= self.limit or (
            not identity and self.ordinary_active >= self.ordinary_limit
        ):
            raise Fault("queue_full", 429)
        self.active += 1
        if not identity:
            self.ordinary_active += 1
        return Lease(self, identity=identity)


class Lease:
    def __init__(self, owner, *, identity=False):
        self.owner, self.identity = owner, identity
        self.task = None
        self.abandoned = False
        self.released = False
        self.business_started = False
        self.request = None

    async def run(self, operation, *args, business=False, read_only=False):
        def execute():
            if self.abandoned:
                raise Fault("timeout", 408)
            if business:
                self.business_started = True
            with cancellable_read(lambda: self.abandoned) if read_only else nullcontext():
                return operation(*args)

        self.task = asyncio.create_task(run_in_threadpool(execute))
        # Drain a late fault even when the response has already ended.
        self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        if self.request is None:
            return await asyncio.shield(self.task)

        async def disconnected():
            while True:
                if (await self.request.receive())["type"] == "http.disconnect":
                    return

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((self.task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                self.abandoned = True
                raise ClientDisconnect()
            return self.task.result()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def finish(self):
        self.abandoned = True
        if self.task is not None and not self.task.done():
            self.task.add_done_callback(lambda task: self.release())
        else:
            self.release()

    def release(self):
        if not self.released:
            self.released = True
            self.owner.active -= 1
            if not self.identity:
                self.owner.ordinary_active -= 1
