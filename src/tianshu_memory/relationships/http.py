"""Internal relationship ports with bounded requests and live authorization."""

import asyncio
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from starlette.requests import ClientDisconnect

from ..diagnostics import anote_authenticated, note_fault, record_execution
from ..domain import Fault, require, strict_json
from .history import read as read_history
from .ledger import identifier, pair_key


def mount(app, relationships, auth, admission, *, body_timeout, execute_timeout):
    def endpoint_for(operation):
        async def endpoint(request: Request):
            lease = None
            request_id = "request-unavailable"

            def failed(code, status):
                note_fault(code)
                return JSONResponse(
                    Fault(code, status).wire(request_id),
                    status_code=status,
                    headers={"Cache-Control": "no-store"},
                )

            try:
                require(
                    relationships is not None and auth is not None, "dependency_unavailable", 503
                )
                require(
                    relationships.service.source_authority is not None,
                    "dependency_unavailable",
                    503,
                )
                lease = admission.claim()
                permission = "relationships." + ("read" if operation == "history" else operation)
                async with asyncio.timeout(execute_timeout):
                    caller_name, caller = await lease.run(
                        auth.authenticate, request.headers.get("authorization")
                    )
                require(permission in caller.get("operations", []))
                if operation == "manage":
                    require(caller.get("role_admin") is True)
                require(await anote_authenticated(), "log_unavailable", 503)
                # Browser login/CSRF belongs to Platform, never these service ports.
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
                        require(len(body) + len(chunk) <= 16384, "invalid_input", 413)
                        body.extend(chunk)
                payload = strict_json(body)
                require(
                    type(payload) is dict
                    and type(payload.get("schema_version")) is int
                    and payload["schema_version"] == 1,
                    "invalid_input",
                    400,
                )
                allowed = {"schema_version", "request_id", "origin"} | {
                    "read": {"pair", "managed"},
                    "history": {"pair", "managed"},
                    "check": {"pair", "managed", "expected_version"},
                    "manage": {"command"},
                    "settle": {"candidate"},
                }[operation]
                require(set(payload) <= allowed, "invalid_input", 400)
                proposed_id = payload.get("request_id")
                require(identifier(proposed_id), "invalid_input", 400)
                request_id = proposed_id
                origin = payload.get("origin")
                require(
                    type(origin) is dict
                    and set(origin) == {"assertion_ref"}
                    and type(origin["assertion_ref"]) is str
                    and 0 < len(origin["assertion_ref"]) <= 128,
                    "invalid_input",
                    400,
                )
                if operation in {"read", "check", "history"}:
                    pair_key(payload.get("pair"))
                    require(type(payload.get("managed", False)) is bool, "invalid_input", 400)
                if operation == "history":
                    require(payload.get("managed") is True, "invalid_input", 400)
                if operation == "manage":
                    require(
                        type(payload.get("command")) is dict
                        and payload["command"].get("request_id") == request_id,
                        "invalid_input",
                        400,
                    )
                lease.request = request

                def execute():
                    with record_execution(permission):
                        current_name, current_caller = auth.authenticate(
                            request.headers.get("authorization")
                        )
                        require(
                            current_name == caller_name
                            and permission in current_caller.get("operations", [])
                        )
                        if operation == "manage":
                            return relationships.manage(
                                payload["command"],
                                authorization=request.headers.get("authorization"),
                                assertion_ref=origin["assertion_ref"],
                            )
                        if operation == "history":
                            return read_history(
                                relationships,
                                payload["pair"],
                                authorization=request.headers.get("authorization"),
                                assertion_ref=origin["assertion_ref"],
                                request_id=request_id,
                            )
                        if payload.get("managed", False):
                            current = relationships.read_managed(
                                payload["pair"],
                                authorization=request.headers.get("authorization"),
                                assertion_ref=origin["assertion_ref"],
                                request_id=request_id,
                            )
                            if operation == "read":
                                return current
                            return relationships.check_projection(
                                current, payload.get("expected_version")
                            )
                        context = auth.resolve(
                            current_name, current_caller, origin["assertion_ref"], request_id
                        )
                        scope = context["allowed_scope"]
                        if operation in {"read", "check"}:
                            require(
                                payload["pair"] == {k: scope[k] for k in ("actor_id", "person_id")}
                            )
                            if operation == "read":
                                return relationships.read(scope, context)
                            return relationships.check(
                                scope, context, payload.get("expected_version")
                            )
                        return relationships.settle(payload.get("candidate"), scope, context)

                async with asyncio.timeout(execute_timeout):
                    result = await lease.run(execute, business=True)
                field = (
                    "history"
                    if operation == "history"
                    else "settlement"
                    if operation == "settle"
                    else "check"
                    if operation == "check"
                    else "projection"
                )
                return JSONResponse(
                    {"schema_version": 1, "request_id": request_id, field: result},
                    headers={"Cache-Control": "no-store"},
                )
            except Fault as error:
                return failed(error.code, error.status)
            except TimeoutError:
                return failed("timeout", 408)
            except (ValidationError, ValueError, KeyError, TypeError, ClientDisconnect):
                return failed("invalid_input", 400)
            except (sqlite3.Error, OSError, RuntimeError):
                return failed("dependency_unavailable", 503)
            finally:
                if lease is not None:
                    lease.finish()

        return endpoint

    for operation in ("read", "manage", "settle", "check", "history"):
        app.add_api_route(
            "/internal/v1/relationships/" + operation,
            endpoint_for(operation),
            methods=["POST"],
            name="relationships_" + operation,
        )
