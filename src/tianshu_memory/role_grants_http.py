"""One thin role authority transport shared by Memory and standalone Knowledge."""

import asyncio
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse

from .diagnostics import anote_authenticated, note_fault
from .domain import Fault, strict_json


def mount(app, auth, admission, *, body_timeout):
    @app.post("/internal/v1/role-runtime/authorize")
    async def authorize_role(request: Request):
        lease = None
        try:
            if auth is None or auth.role_grants is None:
                raise Fault("dependency_unavailable", 503)
            lease = admission.claim()
            caller_name, caller = await lease.run(
                auth.authenticate, request.headers.get("authorization")
            )
            if caller_name != "platform" or caller.get("role_admin") is not True:
                await anote_authenticated("forbidden")
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
                    if len(body) + len(chunk) > 16384:
                        raise Fault("invalid_input", 400)
                    body.extend(chunk)
            payload = strict_json(body)
            if isinstance(payload, dict) and payload.get("operation") == "status":
                if set(payload) != {"operation", "actor_id"} or not isinstance(
                    payload["actor_id"], str
                ):
                    raise Fault("invalid_input", 400)
                result = await lease.run(auth.role_grants.status, payload["actor_id"])
            else:
                static_actors = {
                    actor
                    for entry in auth.config().get("callers", {}).values()
                    for actor in entry.get("allowed_actors", [])
                }
                result = await lease.run(
                    auth.role_grants.apply, payload, static_actors, business=True
                )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Fault as error:
            note_fault(error.code)
            return JSONResponse(
                {"code": error.code},
                status_code=error.status,
                headers={"Cache-Control": "no-store"},
            )
        except (TimeoutError, ValueError, TypeError, KeyError, sqlite3.Error):
            note_fault("dependency_unavailable")
            return JSONResponse(
                {"code": "dependency_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            if lease is not None:
                lease.finish()
