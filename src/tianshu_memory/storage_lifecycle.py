"""Close the assembled storage owner after the existing application lifespan finishes."""

from contextlib import asynccontextmanager


def attach(app, store, *, drain=None, before_close=None):
    app.state.storage_store = store
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with previous(application) as state:
                yield state
        finally:
            if drain is not None:
                await drain()
            if before_close is not None:
                before_close()
            store.close()

    app.router.lifespan_context = lifespan


def attach_drain(app, drain):
    """Finish detached synchronous work before any inner resource owner exits."""
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application) as state:
            try:
                yield state
            finally:
                await drain()

    app.router.lifespan_context = lifespan
