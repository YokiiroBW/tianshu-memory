import asyncio
import json
import math
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from starlette.requests import ClientDisconnect

from . import browser
from .auth import Authenticator
from .chat_requests import Requests
from .contracts import Contracts
from .diagnostics import (
    CHAT_SERVICE,
    anote_authenticated,
    note_fault,
    record_execution,
)
from .domain import Fault, fingerprint, now, strict_json
from .qq_identity import observe_alias
from .qq_identity import profiles as qq_profiles
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


async def served(*, service, request, operation, payload, context, module, output_type, lease):
    """Validate the result and record actual worker execution, including late completion."""

    def execute():
        with record_execution(operation):
            return getattr(service, operation)(payload, context)

    result = await lease.run(execute, business=True, read_only=operation == "check_sources")
    service.contracts.validate(f"{module}#{output_type}", result)
    return JSONResponse(
        result,
        headers={
            "Cache-Control": "no-store",
            "X-Memory-Token-Accounting": "utf8-bytes-conservative-estimate",
        },
    )


def create_app(
    *,
    service=None,
    auth=None,
    relationships=None,
    memory_context=None,
    body_timeout=5.0,
    execute_timeout=15.0,
    max_active=8,
    internal_clients=None,
    storage_environ=None,
):
    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            try:
                await application.state.drain()
            finally:
                try:
                    try:
                        await application.state.actor_memory.aclose()
                    finally:
                        if auth is not None and hasattr(auth, "close"):
                            auth.close()
                finally:
                    try:
                        transport = getattr(
                            getattr(service, "source_authority", None), "transport", None
                        )
                        if transport is not None and hasattr(transport, "close"):
                            transport.close()
                    finally:
                        verifier = getattr(getattr(service, "observations", None), "verifier", None)
                        if verifier is not None and hasattr(verifier, "close"):
                            verifier.close()

    app = FastAPI(
        title="Tianshu Memory",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if (
        not all(
            type(v) in (int, float) and math.isfinite(v) and v > 0
            for v in (body_timeout, execute_timeout)
        )
        or type(max_active) is not int
        or not 1 <= max_active <= 64
    ):
        raise ValueError("Invalid chat request limits")
    admission = Requests(max_active, reserve_identity=True)
    app.state.chat_requests = admission
    from .actor_memory.http import mount as mount_actor_memory

    mount_actor_memory(
        app,
        auth,
        body_timeout=body_timeout,
        execute_timeout=execute_timeout,
        max_active=max_active,
        internal_clients=internal_clients,
        storage_environ=storage_environ,
    )

    async def drain():
        # Do not stop callback admission here. The host stops external root work
        # first and drains all domains before closing their shared exchange.
        while True:
            await asyncio.gather(admission.drain(), app.state.actor_memory.drain())
            if not admission.active and not app.state.actor_memory.admission.active:
                return

    app.state.drain = drain
    from .candidate_http import mount as mount_candidates

    mount_candidates(
        app, service, auth, admission, body_timeout=body_timeout, execute_timeout=execute_timeout
    )
    app.state.memory = service
    from .relationships import Relationships
    from .relationships.http import mount as mount_relationships

    if relationships is None and service is not None:
        relationships = Relationships(service, auth=auth)
    if service is not None:
        service.relationships = relationships
    mount_relationships(
        app,
        relationships,
        auth,
        admission,
        body_timeout=body_timeout,
        execute_timeout=execute_timeout,
    )
    app.state.relationships = relationships
    from .memory_context import MemoryContext
    from .memory_context.http import mount as mount_context
    from .memory_context.proofs import PlatformProofs

    if memory_context is None and service is not None:
        memory_context = MemoryContext(
            service, proofs=PlatformProofs(auth, service.contracts, service.clock) if auth else None
        )
    if service is not None:
        service.memory_context = memory_context
    app.state.memory_context = memory_context
    mount_context(
        app,
        memory_context,
        auth,
        admission,
        body_timeout=body_timeout,
        execute_timeout=execute_timeout,
    )
    from .knowledge_content import KnowledgeContent
    from .knowledge_content_http import mount as mount_content

    content = KnowledgeContent(service, auth) if service is not None and auth is not None else None
    app.state.knowledge_content = content
    mount_content(
        app, content, auth, admission, body_timeout=body_timeout, execute_timeout=execute_timeout
    )

    async def qq_identity_endpoint(request: Request, operation: str):
        """Explicit Platform service ports; neither is available to a chat caller."""
        lease = None
        request_id = "request-unavailable"
        try:
            if service is None or auth is None:
                raise Fault("dependency_unavailable", 503)
            callers = auth.config().get("callers", {})
            if type(callers) is not dict or any(
                type(caller) is not dict for caller in callers.values()
            ):
                raise Fault("dependency_unavailable", 503)
            for protected in ("platform_qq_profiles", "platform_qq_alias"):
                credential = callers.get(protected, {}).get("token")
                if (
                    type(credential) is not str
                    or not credential
                    or sum(caller.get("token") == credential for caller in callers.values()) != 1
                ):
                    raise Fault("dependency_unavailable", 503)
            lease = admission.claim()
            async with asyncio.timeout(execute_timeout):
                caller_name, caller = await lease.run(
                    auth.authenticate, request.headers.get("authorization")
                )
            expected_caller = {
                "qq_profiles": "platform_qq_profiles",
                "qq_alias": "platform_qq_alias",
            }[operation]
            if caller_name != expected_caller or operation not in caller.get("operations", []):
                raise Fault("forbidden", 403)
            if not await anote_authenticated():
                raise Fault("log_unavailable", 503)
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise Fault("invalid_input", 400)
            body = bytearray()
            async with asyncio.timeout(body_timeout):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 4096:
                        raise Fault("invalid_input", 400)
                    body.extend(chunk)
            payload = strict_json(body)
            if type(payload) is not dict or payload.get("schema_version") != 1:
                raise Fault("invalid_input", 400)
            request_id = payload.get("request_id")
            if type(request_id) is not str or not 1 <= len(request_id) <= 128:
                request_id = "request-unavailable"
                raise Fault("invalid_input", 400)
            if operation == "qq_profiles":
                if set(payload) != {"schema_version", "request_id", "limit", "after"}:
                    raise Fault("invalid_input", 400)
                result = await lease.run(
                    lambda: qq_profiles(
                        service.store, limit=payload["limit"], after=payload["after"]
                    ),
                    business=True,
                )
            else:
                result = await lease.run(
                    lambda: observe_alias(
                        service.store, payload, local_commit=service.local_commit
                    ),
                    business=True,
                )
            return JSONResponse(
                {**result, "request_id": request_id}, headers={"Cache-Control": "no-store"}
            )
        except Fault as error:
            return JSONResponse(
                error.wire(request_id),
                status_code=error.status,
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, TypeError, KeyError):
            return JSONResponse(Fault("invalid_input", 400).wire(request_id), status_code=400)
        except (sqlite3.Error, OSError, TimeoutError):
            return JSONResponse(
                Fault("dependency_unavailable", 503).wire(request_id), status_code=503
            )
        finally:
            if lease is not None:
                lease.finish()

    @app.post("/internal/v1/identity/qq-profiles")
    async def qq_profiles_endpoint(request: Request):
        return await qq_identity_endpoint(request, "qq_profiles")

    @app.post("/internal/v1/identity/qq-alias")
    async def qq_alias_endpoint(request: Request):
        return await qq_identity_endpoint(request, "qq_alias")

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
                "retrieval": (
                    "postgresql_fts_and_exact"
                    if service.store.backend == "postgresql"
                    else "sqlite_fts5_and_exact"
                )
                if ready
                else "unavailable",
                "embedding": "configured_not_verified"
                if service and service.retrieval is not None
                else "not_configured",
                "postgresql": service.store.runtime.status()["postgresql"]
                if service
                else "not_configured",
                "raw_source_read": "unavailable",
                "token_accounting": "utf8_bytes_conservative_estimate",
            },
            status_code=200 if ready else 503,
        )

    def endpoint_for(operation, input_type, output_type):
        async def endpoint(request: Request):
            request_id = "request-unavailable"
            lease = None
            try:
                if service is None or auth is None:
                    await anote_authenticated("dependency_unavailable")
                    raise Fault("dependency_unavailable", 503)
                lease = admission.claim(identity=operation == "resolve")
                async with asyncio.timeout(execute_timeout):
                    authenticated_service, caller = await lease.run(
                        auth.authenticate, request.headers.get("authorization")
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
                async with asyncio.timeout(body_timeout):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 262144:
                            raise Fault("invalid_input", 400)
                        body.extend(chunk)
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
                lease.request = request
                async with asyncio.timeout(execute_timeout):
                    if operation in {"consume", "check_sources"}:
                        context = auth.publisher(authenticated_service, caller)
                    else:
                        context = await lease.run(
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
                        lease=lease,
                    )
            except Fault as error:
                if error.code == "idempotency_conflict":
                    # Separate transaction after the rejected operation rolled back; no raw payload.
                    def conflict():
                        with service.store.transaction() as db:
                            db.execute(
                                "INSERT INTO conflicts(operation,request_id,digest) VALUES (?,?,?)",
                                (operation, request_id, fingerprint(payload)),
                            )

                    try:
                        async with asyncio.timeout(execute_timeout):
                            await lease.run(conflict)
                    except TimeoutError:
                        pass
                note_fault(error.code)
                return JSONResponse(
                    error.wire(request_id),
                    status_code=error.status,
                    headers={"Cache-Control": "no-store"},
                )
            except TimeoutError:
                note_fault("timeout")
                result = Fault("timeout", 408).wire(request_id)
                if lease is not None and lease.business_started:
                    result.update(execution_state="unknown", retryable=False)
                return JSONResponse(result, status_code=408, headers={"Cache-Control": "no-store"})
            except ClientDisconnect:
                note_fault("invalid_input")
                result = Fault("invalid_input", 400).wire(request_id)
                if lease is not None and lease.business_started:
                    result.update(execution_state="unknown", retryable=False)
                return JSONResponse(result, status_code=400, headers={"Cache-Control": "no-store"})
            except (ValidationError, ValueError, KeyError, TypeError):
                note_fault("invalid_input")
                return JSONResponse(Fault("invalid_input").wire(request_id), status_code=400)
            except (sqlite3.Error, OSError):
                note_fault("dependency_unavailable")
                return JSONResponse(
                    Fault("dependency_unavailable", 503).wire(request_id), status_code=503
                )
            finally:
                if lease is not None:
                    lease.finish()

        return endpoint

    def browser_endpoint_for(operation):
        async def endpoint(request: Request):
            request_id = "request-unavailable"
            lease = None

            def failed(code, status):
                return JSONResponse(
                    {"status": "failed", "code": code, "request_id": request_id},
                    status_code=status,
                    headers={"Cache-Control": "no-store"},
                )

            try:
                if service is None or auth is None or service.source_authority is None:
                    raise Fault("dependency_unavailable", 503)
                lease = admission.claim()
                async with asyncio.timeout(execute_timeout):
                    caller_name, caller = await lease.run(
                        auth.authenticate, request.headers.get("authorization")
                    )
                if "browse" not in caller.get("operations", []):
                    await anote_authenticated("forbidden")
                    raise Fault("forbidden", 403)
                if not await anote_authenticated():
                    raise Fault("log_unavailable", 503)
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise Fault("invalid_input", 415)
                body = bytearray()
                async with asyncio.timeout(body_timeout):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > (
                            262144 if operation == "archive-check" else 16384
                        ):
                            raise Fault("invalid_input", 413)
                        body.extend(chunk)
                payload = strict_json(body)
                if operation in {"topics", "archive-check"}:
                    package = service.contracts.load_browser_extension(operation)
                    service.contracts.validate(package + "#request", payload)
                else:
                    browser.validate_request(operation, payload)
                request_id = payload["request_id"]
                lease.request = request
                async with asyncio.timeout(execute_timeout):
                    context = await lease.run(
                        auth.resolve,
                        caller_name,
                        caller,
                        payload["origin"]["assertion_ref"],
                        request_id,
                    )

                    def execute():
                        with record_execution("browse"):
                            current_name, current_caller = auth.authenticate(
                                request.headers.get("authorization")
                            )
                            if current_name != caller_name or "browse" not in current_caller.get(
                                "operations", []
                            ):
                                raise Fault("forbidden", 403)
                            if operation == "topics":
                                from .browser_topics import read

                                return read(
                                    service, auth, caller_name, current_caller, payload, context
                                )
                            if operation == "archive-check":
                                from .browser_archive import read

                                return read(
                                    service, auth, caller_name, current_caller, payload, context
                                )
                            return browser.read(
                                service,
                                auth,
                                caller_name,
                                current_caller,
                                payload,
                                context,
                                operation,
                            )

                    result = await lease.run(execute, business=True)
                    return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except Fault as error:
                note_fault(error.code)
                return failed(error.code, error.status)
            except TimeoutError:
                note_fault("timeout")
                return failed("timeout", 408)
            except ClientDisconnect:
                note_fault("invalid_input")
                return failed("invalid_input", 400)
            except (ValidationError, ValueError, KeyError, TypeError):
                note_fault("invalid_input")
                return failed("invalid_input", 400)
            except (sqlite3.Error, OSError, RuntimeError):
                note_fault("dependency_unavailable")
                return failed("dependency_unavailable", 503)
            finally:
                if lease is not None:
                    lease.finish()

        return endpoint

    for path, (operation, input_type, output_type) in OPERATIONS.items():
        app.add_api_route(
            path, endpoint_for(operation, input_type, output_type), methods=["POST"], name=operation
        )

    from .role_grants_http import mount as mount_role_grants

    mount_role_grants(app, auth, admission, body_timeout=body_timeout)

    for operation in ("overview", "subjects", "records", "topics", "archive-check"):
        app.add_api_route(
            f"/internal/v1/memory/browser/{operation}",
            browser_endpoint_for(operation),
            methods=["POST"],
            name=f"browse_{operation}",
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

    async def observation_endpoint(request: Request, operation: str):
        lease = None
        try:
            if service is None or auth is None or not hasattr(service, "observations"):
                raise Fault("dependency_unavailable", 503)
            lease = admission.claim()
            caller_name, caller = await lease.run(
                auth.authenticate, request.headers.get("authorization")
            )
            if operation not in caller.get("operations", []):
                raise Fault("forbidden", 403)
            if not await anote_authenticated():
                raise Fault("log_unavailable", 503)
            if (
                request.headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
            ):
                raise Fault("invalid_input", 400)
            body = bytearray()
            async with asyncio.timeout(body_timeout):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 65536:
                        raise Fault("invalid_input", 413)
                    body.extend(chunk)
            payload = strict_json(body)
            method = (
                service.observations.ingest
                if operation == "observe_ingest"
                else service.observations.query
            )
            result = await lease.run(method, caller_name, payload, business=True)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Fault as exc:
            return JSONResponse(
                exc.wire("request-unavailable"),
                status_code=exc.status,
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, KeyError, TypeError):
            return JSONResponse(
                Fault("invalid_input", 400).wire("request-unavailable"), status_code=400
            )
        except (sqlite3.Error, OSError, TimeoutError):
            return JSONResponse(
                Fault("dependency_unavailable", 503).wire("request-unavailable"), status_code=503
            )
        finally:
            if lease is not None:
                lease.finish()

    @app.post("/internal/v2/memory/observations")
    async def observation_ingest(request: Request):
        return await observation_endpoint(request, "observe_ingest")

    @app.post("/internal/v2/memory/observations/query")
    async def observation_query(request: Request):
        return await observation_endpoint(request, "observe_query")

    return app


def runtime_app(
    config_path,
    contract_path,
    *,
    internal_clients=None,
    storage_environ=None,
    diagnostics=None,
    log_directory=None,
    diagnostics_environ=None,
):
    """The composition root the deployment CLI hands to `server_runtime`.

    It is the same `configured_app` factory the product already had, plus the one thing a probe
    cannot read from a file: whether this process really holds its own service and authenticator.
    No route, no authorization rule and no repair path is added here.
    """
    from .runtime_probes import ProbeConfig

    app = configured_app(
        config_path,
        internal_clients=internal_clients,
        storage_environ=storage_environ,
        diagnostics=diagnostics,
        log_directory=log_directory,
        diagnostics_contract=contract_path,
        diagnostics_environ=diagnostics_environ,
    )

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
            storage_environ=storage_environ,
        )

    app.state.probe_settings = probe_settings
    return app


def configured_app(
    config_path=None,
    *,
    internal_clients=None,
    storage_environ=None,
    diagnostics=None,
    log_directory=None,
    diagnostics_contract=None,
    diagnostics_environ=None,
):
    config_path = config_path or os.environ.get("TIANSHU_MEMORY_CONFIG")
    if not config_path:
        return create_app()
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    # All paths and credentials are explicit; no production database or remote service defaults.
    contracts = Contracts(config["contract_directory"])
    wants_context = any(
        any(operation.startswith("context_") for operation in caller.get("operations", []))
        for caller in config.get("callers", {}).values()
    )
    if any(
        "select_profiles" in caller.get("operations", [])
        for caller in config.get("callers", {}).values()
    ) or bool(config.get("browser_readers")):
        contracts.load_profiles()
    store = Store.from_config(config, storage_environ=storage_environ)
    sources = LocalFixtureSources() if config.get("mode") == "local_fixture" else None
    if config.get("mode") == "source_sync":
        from .source_authority import SourceAuthority
        from .source_transport import SourceTransport

        contracts.load_sources()
        sources = SourceAuthority(
            SourceTransport(config_path, contracts, internal_clients=internal_clients), contracts
        )
    if wants_context:
        contracts.load_context()
    if any(
        any(operation.startswith("content_") for operation in caller.get("operations", []))
        for caller in config.get("callers", {}).values()
    ):
        contracts.load_content()
    retrieval = None
    if config.get("retrieval") is not None:
        from .retrieval import Retrieval
        from .retrieval_transport import RetrievalTransport

        if config["retrieval"].get("backend") == "local_python":
            from .local_embedding import LocalEmbeddingBackend

            retrieval = Retrieval(LocalEmbeddingBackend(config["retrieval"]))
        else:
            retrieval = Retrieval(
                RetrievalTransport(config_path, internal_clients=internal_clients)
            )
    service = MemoryService(
        store,
        contracts,
        source_authority=sources,
        retrieval=retrieval,
        consolidation_ttl_days=config.get("memory_consolidation", {}).get("ttl_days"),
    )
    if config.get("observation_source") is not None:
        from .observations import ObservationLedger, PlatformVerifier

        service.observations = ObservationLedger(
            store,
            PlatformVerifier(config_path, internal_clients=internal_clients),
            local_commit=service.local_commit,
        )
    auth = Authenticator(
        config_path,
        contracts,
        now,
        role_database=store.role_database,
        internal_clients=internal_clients,
    )
    from .relationships import Policy, Relationships

    policy_config = dict(config.get("relationships_policy", {}))
    for name in ("boundaries", "decay_rates"):
        if name in policy_config:
            policy_config[name] = tuple(policy_config[name])
    relationships = Relationships(service, auth=auth, policy=Policy(**policy_config))
    app = create_app(
        service=service,
        auth=auth,
        relationships=relationships,
        internal_clients=internal_clients,
        storage_environ=storage_environ,
    )
    app.state.auth = auth
    if retrieval is not None:
        from .memory_index_lifecycle import attach as attach_index

        attach_index(app, service)
    from .storage_lifecycle import attach

    attach(app, store)
    from .module_runtime import install_diagnostics

    install_diagnostics(
        app,
        CHAT_SERVICE,
        config_path,
        diagnostics_contract,
        diagnostics=diagnostics,
        log_directory=log_directory,
        diagnostics_environ=diagnostics_environ,
    )
    return app
