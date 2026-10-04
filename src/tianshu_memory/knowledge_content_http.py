"""Private service routes, including real bounded binary upload and original bytes."""

import asyncio
import json
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from jsonschema.exceptions import ValidationError
from starlette.requests import ClientDisconnect

from .diagnostics import anote_authenticated, note_fault, record_execution
from .domain import Fault, require, strict_json

DEFINITIONS = {
    "acquire": ("acquire_request", "acquire_response"),
    "read": ("read_request", "read_response"),
    "original": ("original_request", None),
    "uploads": ("upload_request", "upload_response"),
    "upload_status": ("upload_status_request", "upload_response"),
    "access": ("access_request", "access_response"),
}


def mount(app, application, auth, admission, *, body_timeout, execute_timeout):
    def endpoint_for(operation, *, binary=False):
        async def endpoint(request: Request):
            lease, request_id = None, "request-unavailable"
            mutation = operation in {"acquire", "uploads", "access"}

            def failed(error, uncertain=False):
                note_fault(error.code)
                wire = error.wire(request_id)
                if uncertain:
                    wire.update(execution_state="unknown", retryable=False)
                return JSONResponse(
                    wire, status_code=error.status, headers={"Cache-Control": "no-store"}
                )

            try:
                require(
                    application is not None
                    and auth is not None
                    and application.service.contracts.content_version == "1.0.0",
                    "dependency_unavailable",
                    503,
                )
                lease = admission.claim()
                permission = "content_" + operation
                caller_name, caller = await lease.run(
                    auth.authenticate, request.headers.get("authorization")
                )
                require(permission in caller.get("operations", []))
                require(await anote_authenticated(), "log_unavailable", 503)
                require(not request.headers.get("origin") and not request.headers.get("cookie"))
                content_type = (
                    request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                require(
                    content_type == ("application/octet-stream" if binary else "application/json"),
                    "invalid_input",
                    415,
                )
                if binary:
                    upload_id = request.path_params["upload_id"]

                    def descriptor_principal():
                        with application.service.store.transaction() as db:
                            row = db.execute(
                                "SELECT owner FROM knowledge_content_uploads WHERE id=?",
                                (upload_id,),
                            ).fetchone()
                            require(row is not None, "not_found", 404)
                            owner = json.loads(row["owner"])
                        rid = request.headers.get("x-tianshu-request-id", upload_id)
                        require(1 <= len(rid) <= 128, "invalid_input", 400)
                        if owner["kind"] == "user":
                            return {
                                "kind": "user",
                                "query": {
                                    "schema_version": 1,
                                    "request_id": rid,
                                    "origin": {
                                        "assertion_ref": request.headers.get(
                                            "x-tianshu-assertion-ref"
                                        )
                                    },
                                },
                                "scope": owner["scope"],
                            }
                        return {
                            "kind": "actor",
                            "request_id": rid,
                            "actor_id": request.headers.get("x-tianshu-actor-id"),
                            "operation_ref": request.headers.get("x-tianshu-operation-ref"),
                        }

                    principal = await lease.run(descriptor_principal)
                    application.service.contracts.validate("knowledge-content#principal", principal)
                    authority = await lease.run(
                        application.authorize, principal, caller_name, caller
                    )
                    request_id = authority["request_id"]
                    limit = await lease.run(application.upload_size, upload_id, authority)
                    length = request.headers.get("content-length")
                    require(
                        length is None or length.isdigit() and int(length) == limit,
                        "invalid_input",
                        400,
                    )
                else:
                    limit = 32768
                raw = bytearray()
                async with asyncio.timeout(max(body_timeout, 30) if binary else body_timeout):
                    async for chunk in request.stream():
                        require(len(raw) + len(chunk) <= limit, "source_too_large", 413)
                        raw.extend(chunk)
                if not binary:
                    payload = strict_json(raw)
                    input_type, output_type = DEFINITIONS[operation]
                    application.service.contracts.validate(
                        "knowledge-content#" + input_type, payload
                    )
                    principal = payload["principal"]
                    request_id = (
                        principal["query"]["request_id"]
                        if principal["kind"] == "user"
                        else principal["request_id"]
                    )
                else:
                    output_type = "upload_response"
                if operation == "access" and principal["kind"] == "actor":
                    require("content_actor_access" in caller.get("operations", []))
                lease.request = request

                def refresh():
                    name, current = auth.authenticate(request.headers.get("authorization"))
                    require(name == caller_name and permission in current.get("operations", []))
                    if operation == "access" and principal["kind"] == "actor":
                        require("content_actor_access" in current.get("operations", []))
                    return application.authorize(principal, name, current)

                def execute():
                    with record_execution(permission):
                        authority = refresh()
                        if binary:
                            result = application.put_upload(upload_id, bytes(raw), authority)
                        elif operation in {"acquire", "read"}:
                            result = getattr(application, operation)(payload, authority, refresh)
                        else:
                            result = getattr(application, operation)(payload, authority)
                        if output_type is not None:
                            application.service.contracts.validate(
                                "knowledge-content#" + output_type, result
                            )
                        return result

                async with asyncio.timeout(max(execute_timeout, 25)):
                    result = await lease.run(execute, business=True)
                if operation == "original":
                    return Response(result[0], headers=result[1])
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except Fault as error:
                return failed(error)
            except (TimeoutError, ClientDisconnect):
                return failed(
                    Fault("timeout", 408), mutation and lease is not None and lease.business_started
                )
            except (ValidationError, ValueError, KeyError, TypeError):
                return failed(
                    Fault("invalid_input", 400),
                    mutation and lease is not None and lease.business_started,
                )
            except (sqlite3.Error, OSError):
                return failed(
                    Fault("dependency_unavailable", 503),
                    mutation and lease is not None and lease.business_started,
                )
            finally:
                if lease is not None:
                    lease.finish()

        return endpoint

    for operation in DEFINITIONS:
        app.add_api_route(
            "/internal/v1/knowledge/content/" + operation.replace("_", "-"),
            endpoint_for(operation),
            methods=["POST"],
            name="knowledge_content_" + operation,
        )
    app.add_api_route(
        "/internal/v1/knowledge/content/uploads/{upload_id}",
        endpoint_for("uploads", binary=True),
        methods=["PUT"],
        name="knowledge_content_upload_bytes",
    )
