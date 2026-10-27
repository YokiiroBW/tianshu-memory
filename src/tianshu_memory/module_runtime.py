"""Optional diagnostic ownership for applications assembled without a listener."""

from contextlib import asynccontextmanager

from .diagnostics import Diagnostics


def install_diagnostics(
    app,
    service,
    config_path,
    contract_path,
    *,
    diagnostics=None,
    log_directory=None,
    diagnostics_environ=None,
):
    if diagnostics is None and log_directory is None:
        return
    owned = diagnostics is None
    if owned:
        from .server_runtime import diagnostics_contract_path, read_configuration

        raw = read_configuration(config_path)
        raw["log_directory"] = str(log_directory) if log_directory is not False else False
        diagnostics = Diagnostics(
            service,
            raw,
            config_path=config_path,
            contract_path=diagnostics_contract_path(raw, config_path, contract_path),
            environ=diagnostics_environ,
        )
    diagnostics.install(app)
    if not owned:
        return  # The injecting native Assembly/host owns this writer's lifecycle.
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        await diagnostics.aemit("runtime.starting", level="INFO", outcome="started")
        try:
            async with previous(application) as state:
                await diagnostics.aemit("runtime.started", level="INFO", outcome="succeeded")
                try:
                    yield state
                finally:
                    await diagnostics.aemit("runtime.stopping", level="INFO", outcome="started")
        finally:
            await diagnostics.aemit("runtime.stopped", level="INFO", outcome="succeeded")
            await diagnostics.ashutdown()

    app.router.lifespan_context = lifespan
