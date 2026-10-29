"""Authenticated candidate processing using the existing consume grant and request lease."""

import asyncio
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse
from jsonschema import ValidationError
from starlette.requests import ClientDisconnect

from .diagnostics import anote_authenticated, record_execution
from .domain import Fault, require, strict_json


def failure_location(error):
    """Code locations and fixed peer labels only, never exception text or values."""
    result = {}
    trace = error.__traceback__
    while trace is not None:
        module = trace.tb_frame.f_globals.get("__name__", "")
        function = trace.tb_frame.f_code.co_name
        if module.startswith("tianshu_memory.") and (module, function) != (
            "tianshu_memory.domain",
            "require",
        ):
            result["site"] = f"{module}:{function}:{trace.tb_lineno}"
        trace = trace.tb_next
    if getattr(error, "source_owner", None) in {"core", "platform"}:
        result["source_owner"] = error.source_owner
    if getattr(error, "source_operation", None) in {
        "facts",
        "current_access",
        "summary_window_check",
        "segment_frame",
    }:
        result["source_operation"] = error.source_operation
    status = getattr(error, "source_http_status", None)
    if type(status) is int and 100 <= status <= 599:
        result["source_http_status"] = status
    return result


def mount(app, service, auth, admission, *, body_timeout, execute_timeout):
    @app.post("/internal/v1/memory/candidates/process")
    @app.post("/internal/v1/memory/candidates/materials")
    @app.post("/internal/v1/memory/candidates/segments")
    async def process(request: Request):
        lease = None
        diagnostic = None
        try:
            require(service is not None and auth is not None, "dependency_unavailable", 503)
            lease = admission.claim()
            async with asyncio.timeout(execute_timeout):
                caller_name, caller = await lease.run(
                    auth.authenticate, request.headers.get("authorization")
                )
            require(caller_name == "companion" and "consume" in caller.get("operations", []))
            require(await anote_authenticated(), "log_unavailable", 503)
            require(not request.headers.get("origin") and not request.headers.get("cookie"))
            require(
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "application/json",
                "invalid_input",
                415,
            )
            body = bytearray()
            async with asyncio.timeout(body_timeout):
                async for chunk in request.stream():
                    require(len(body) + len(chunk) <= 262144, "invalid_input", 413)
                    body.extend(chunk)
            payload = strict_json(body)
            require(isinstance(payload, dict), "invalid_input", 400)
            lease.request = request

            def execute():
                with record_execution("candidate_process"):
                    current_name, current = auth.authenticate(request.headers.get("authorization"))
                    require(
                        current_name == "companion" and "consume" in current.get("operations", [])
                    )
                    publisher = dict(
                        authenticated_service=current_name,
                        allowed_scopes=current.get("event_scopes", []),
                        allowed_role_actors=auth.role_grants.active_ids()
                        if current.get("allow_runtime_roles") is True and auth.role_grants
                        else [],
                        blocked_role_actors=auth.role_grants.inactive_ids()
                        if auth.role_grants
                        else [],
                    )
                    if service.contracts.extraction_version is None:
                        service.contracts.load_memory_v2()
                    if request.url.path.endswith("/materials"):
                        from .segment_materials import SegmentMaterials

                        return SegmentMaterials(service).execute(payload, publisher)
                    if request.url.path.endswith("/segments"):
                        from .segments import Segments

                        return Segments(service).seal(payload, publisher)
                    from .segments import Segments

                    require(payload.get("extraction_schema") == 6, "invalid_input", 400)
                    return Segments(service).execute(payload, publisher)

            async with asyncio.timeout(execute_timeout):
                result = await lease.run(execute, business=True)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Fault as error:
            code, status = error.code, error.status
            diagnostic = failure_location(error)
        except (TimeoutError, ClientDisconnect):
            code, status = "timeout", 408
        except (ValidationError, ValueError, TypeError, KeyError) as error:
            code, status = "invalid_input", 400
            diagnostic = failure_location(error)
        except (OSError, sqlite3.Error) as error:
            code, status = "dependency_unavailable", 503
            diagnostic = failure_location(error)
        finally:
            if lease is not None:
                lease.finish()
        body = dict(
            code=code,
            execution_state="unknown" if lease and lease.business_started else "not_started",
        )
        if diagnostic and lease and lease.business_started:
            body["diagnostic"] = diagnostic
        return JSONResponse(
            body,
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
