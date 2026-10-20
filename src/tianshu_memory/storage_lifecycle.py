"""Close the assembled storage owner after the existing application lifespan finishes."""

from contextlib import asynccontextmanager


def attach(app, store):
    app.state.storage_store = store
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with previous(application) as state:
                yield state
        finally:
            store.close()

    app.router.lifespan_context = lifespan
