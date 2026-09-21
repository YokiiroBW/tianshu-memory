import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from starlette.concurrency import run_in_threadpool

from .auth import Authenticator
from .contracts import Contracts
from .diagnostics import (
    CHAT_SERVICE,
    anote_authenticated,
    note_fault,
    record_execution,
)
from .domain import Fault, fingerprint, now, strict_json
from .service import MemoryService
from .sources import LocalFixtureSources
from .store import Store

OPERATIONS = {
    "/internal/v1/identity/resolve": ("resolve", "resolve_request", "resolve_response"),
    "/internal/v1/identity/register": ("register", "register_request", "identity_response"),
    "/internal/v1/identity/link": ("link", "link_request", "identity_response"),
    "/internal/v1/memory/select": ("select", "select_request", "select_response"),
    "/internal/v1/memory/revise": ("revise", "revise_request", "revise_response"),
    "/internal/v1/memory/turn-commits": ("consume", "committed_event", "consume_receipt"),
}


def module_of(operation, input_type):
    """The contract module an operation's input and output belong to."""
    if operation == "check_sources":
        return "sync-shared"
    if operation == "select_profiles":
        return "profiles"
    return "identity-memory"


async def served(*, service, request, operation, payload, context, module, output_type):
    """Run one operation through the service and validate the shape it produced.

    The one seam this task adds to the request path: the synchronous call is wrapped so the
    diagnostic adapter records when it really started and when it really ended. Nothing about the
    call itself changes — same service object, same arguments, same threadpool.
    """
    execution = record_execution(operation)
    try:
        with execution:
            result = await run_in_threadpool(getattr(service, operation), payload, context)
    except BaseException:
        # A response that ended before the worker could start performs no work at all, and the log
        # says exactly that instead of leaving a start without an end.
        execution.cancelled()
        raise
    service.contracts.validate(f"{module}#{output_type}", result)
    return JSONResponse(
        result,
        headers={
            "Cache-Control": "no-store",
            "X-Memory-Token-Accounting": "utf8-bytes-conservative-estimate",
        },
    )


def create_app(*, service=None, auth=None):
    app = FastAPI(title="Tianshu Memory", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.memory = service

    @app.get("/health")
    def health():
        ready = service is not None and auth is not None and service.source_authority is not None
        return JSONResponse(
            {
                "state": "ready" if ready else "unavailable",
                "contract_version": "1.0.0",
                "profile_contract_version": service.contracts.profile_version if service else None,
                "source_backend": "source_sync_https"
                if ready and service.synchronized
                else "local_fixture"
                if ready and isinstance(service.source_authority, LocalFixtureSources)
                else "unconfigured",
                "retrieval": "sqlite_fts5_and_exact" if ready else "unavailable",
                "embedding": "not_configured",
                "postgresql": "not_implemented",
                "raw_source_read": "unavailable",
                "token_accounting": "utf8_bytes_conservative_estimate",
            },
            status_code=200 if ready else 503,
        )

    def endpoint_for(operation, input_type, output_type):
        async def endpoint(request: Request):
            request_id = "request-unavailable"
            try:
                if service is None or auth is None:
                    await anote_authenticated("dependency_unavailable")
                    raise Fault("dependency_unavailable", 503)
                authenticated_service, caller = auth.authenticate(
                    request.headers.get("authorization")
                )
                if operation not in caller.get("operations", []):
                    await anote_authenticated("forbidden")
                    raise Fault("forbidden", 403)
                # The verdict is recorded here, straight after the same two checks that always
                # decided it: nothing about who may call what has changed. The wait for that record
                # happens off the event loop, and a record that could not be confirmed refuses the
                # request rather than letting an unaccountable call proceed: the credential was
                # accepted, so acting on it now would be a side effect this process cannot log.
                if not await anote_authenticated():
                    raise Fault("log_unavailable", 503)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 262144:
                        raise Fault("invalid_input", 400)
                payload = strict_json(body)
                if not isinstance(payload, dict):
                    raise Fault("invalid_input", 400)
                header = (
                    payload
                    if operation in {"consume", "check_sources"}
                    else payload.get("command", payload.get("query", {}))
                )
                if (
                    isinstance(header, dict)
                    and isinstance(header.get("schema_version"), int)
                    and header["schema_version"] != 1
                ):
                    raise Fault("unsupported_version", 400)
                schema = (
                    f"sync-shared#{input_type}"
                    if operation == "check_sources"
                    else f"conversation#{input_type}"
                    if operation == "consume"
                    else f"profiles#{input_type}"
                    if operation == "select_profiles"
                    else f"identity-memory#{input_type}"
                )
                service.contracts.validate(schema, payload)
                request_id = payload["event_id"] if operation == "consume" else header["request_id"]
                if operation in {"consume", "check_sources"}:
                    context = {
                        "authenticated_service": authenticated_service,
                        "allowed_scopes": caller.get("event_scopes", []),
                    }
                else:
                    context = await run_in_threadpool(
                        auth.resolve,
                        authenticated_service,
                        caller,
                        header["origin"]["assertion_ref"],
                        request_id,
                    )
                if (
                    operation in {"select", "select_profiles", "consume", "revise"}
                    and service.source_authority is None
                ):
                    raise Fault("dependency_unavailable", 503)
                return await served(
                    service=service,
                    request=request,
                    operation=operation,
                    payload=payload,
                    context=context,
                    module=module_of(operation, input_type),
                    output_type=output_type,
                )
            except Fault as error:
                if error.code == "idempotency_conflict":
                    # Separate transaction after the rejected operation rolled back; no raw payload.
                    with service.store.transaction() as db:
                        db.execute(
                            "INSERT INTO conflicts(operation,request_id,digest) VALUES (?,?,?)",
                            (operation, request_id, fingerprint(payload)),
                        )
                note_fault(error.code)
                return JSONResponse(
                    error.wire(request_id),
                    status_code=error.status,
                    headers={"Cache-Control": "no-store"},
                )
            except (ValidationError, ValueError, KeyError, TypeError):
                note_fault("invalid_input")
                return JSONResponse(Fault("invalid_input").wire(request_id), status_code=400)
            except (sqlite3.Error, OSError):
                note_fault("dependency_unavailable")
                return JSONResponse(
                    Fault("dependency_unavailable", 503).wire(request_id), status_code=503
                )

        return endpoint

    for path, (operation, input_type, output_type) in OPERATIONS.items():
        app.add_api_route(
            path, endpoint_for(operation, input_type, output_type), methods=["POST"], name=operation
        )
    # Cannot load or expose the extension until a coordinator-published package is pinned.
    if service is not None and service.contracts.profile_version is not None:
        app.add_api_route(
            "/internal/v1/memory/profiles/select",
            endpoint_for("select_profiles", "select_request", "select_response"),
            methods=["POST"],
            name="select_profiles",
        )
    if service is not None and service.contracts.source_version is not None:
        app.add_api_route(
            "/internal/v1/memory/source-sync/check",
            endpoint_for("check_sources", "check_request", "check_response"),
            methods=["POST"],
            name="check_sources",
        )
    return app


def runtime_app(config_path, contract_path):
    """The composition root the deployment CLI hands to `server_runtime`.

    It is the same `configured_app` factory the product already had, plus the one thing a probe
    cannot read from a file: whether this process really holds its own service and authenticator.
    No route, no authorization rule and no repair path is added here.
    """
    from .runtime_probes import ProbeConfig

    app = configured_app()

    def handles():
        return app.state.memory is not None and getattr(app.state, "auth", None) is not None

    def probe_settings(assembly):
        return ProbeConfig(
            service=CHAT_SERVICE,
            diagnostics=assembly.diagnostics,
            config_path=config_path,
            contract_path=contract_path,
            runtime=assembly,
            handles=handles,
        )

    app.state.probe_settings = probe_settings
    return app


def configured_app():
    config_path = os.environ.get("TIANSHU_MEMORY_CONFIG")
    if not config_path:
        return create_app()
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    # All paths and credentials are explicit; no production database or remote service defaults.
    contracts = Contracts(config["contract_directory"])
    if any(
        "select_profiles" in caller.get("operations", [])
        for caller in config.get("callers", {}).values()
    ):
        contracts.load_profiles()
    store = Store(
        config["database_path"], recovery_path=config.get("source_sync", {}).get("recovery_path")
    )
    sources = LocalFixtureSources() if config.get("mode") == "local_fixture" else None
    if config.get("mode") == "source_sync":
        from .source_authority import SourceAuthority
        from .source_transport import SourceTransport

        contracts.load_sources()
        sources = SourceAuthority(SourceTransport(config_path, contracts), contracts)
    service = MemoryService(store, contracts, source_authority=sources)
    auth = Authenticator(config_path, contracts, now)
    app = create_app(service=service, auth=auth)
    app.state.auth = auth
    return app
