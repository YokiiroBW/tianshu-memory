"""Authenticated candidate processing using the existing consume grant and request lease."""

import asyncio
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse
from jsonschema import ValidationError
from starlette.requests import ClientDisconnect

from .candidate_extraction import CandidateExtraction
from .diagnostics import anote_authenticated, record_execution
from .domain import Fault, require, strict_json


def mount(app, service, auth, admission, *, body_timeout, execute_timeout):
    @app.post("/internal/v1/memory/candidates/process")
    async def process(request: Request):
        lease = None
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
            require("job_ids" in payload or len(body) <= 65536, "invalid_input", 413)
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
                    return CandidateExtraction(service).execute(payload, publisher)

            async with asyncio.timeout(execute_timeout):
                result = await lease.run(execute, business=True)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Fault as error:
            code, status = error.code, error.status
        except (TimeoutError, ClientDisconnect):
            code, status = "timeout", 408
        except (ValidationError, ValueError, TypeError, KeyError):
            code, status = "invalid_input", 400
        except (OSError, sqlite3.Error):
            code, status = "dependency_unavailable", 503
        finally:
            if lease is not None:
                lease.finish()
        return JSONResponse(
            dict(
                code=code,
                execution_state="unknown" if lease and lease.business_started else "not_started",
            ),
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
