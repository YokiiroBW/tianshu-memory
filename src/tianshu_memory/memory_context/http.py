"""Authenticated, bounded service entry points for Memory continuity operations."""

import asyncio
import sqlite3

from fastapi import Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from starlette.requests import ClientDisconnect

from ..diagnostics import anote_authenticated, note_fault, record_execution
from ..domain import Fault, require, strict_json

DEFINITIONS = {
    "query": ("query_request", "query_response"),
    "propose": ("proposal_request", "receipt"),
    "receipt": ("receipt_request", "receipt_response"),
    "batch": ("batch_request", "batch_response"),
    "association": ("association_request", "association_response"),
}


def mount(app, application, auth, admission, *, body_timeout, execute_timeout):
    def endpoint_for(operation):
        async def endpoint(request: Request):
            lease = None
            request_id = "request-unavailable"
            mutation = operation in {"propose", "association"}

            def failed(error, *, uncertain=False):
                note_fault(error.code)
                wire = error.wire(request_id)
                if uncertain:
                    wire.update(execution_state="unknown", retryable=False)
                return JSONResponse(
                    wire, status_code=error.status, headers={"Cache-Control": "no-store"}
                )

            try:
                require(application is not None and auth is not None, "dependency_unavailable", 503)
                require(
                    application.service.contracts.context_version == "1.0.0",
                    "dependency_unavailable",
                    503,
                )
                lease = admission.claim()
                async with asyncio.timeout(execute_timeout):
                    caller_name, caller = await lease.run(
                        auth.authenticate, request.headers.get("authorization")
                    )
                permission = "context_" + operation
                require(permission in caller.get("operations", []))
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
                        require(len(body) + len(chunk) <= 262144, "invalid_input", 400)
                        body.extend(chunk)
                payload = strict_json(body)
                input_type, output_type = DEFINITIONS[operation]
                application.service.contracts.validate("memory-context#" + input_type, payload)
                header = payload["command" if mutation else "query"]
                request_id = header["request_id"]
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
                        context = auth.resolve(
                            current_name,
                            current_caller,
                            header["origin"]["assertion_ref"],
                            request_id,
                        )
                        result = getattr(application, operation)(payload, context)
                        application.service.contracts.validate(
                            "memory-context#" + output_type, result
                        )
                        if result.get("state") == "rejected":
                            note_fault(result["error_code"])
                        return result

                async with asyncio.timeout(execute_timeout):
                    result = await lease.run(execute, business=True)
                return JSONResponse(
                    result,
                    headers={
                        "Cache-Control": "no-store",
                        "X-Memory-Token-Accounting": "utf8-bytes-conservative-estimate",
                    },
                )
            except Fault as error:
                return failed(error)
            except (TimeoutError, ClientDisconnect):
                return failed(
                    Fault("timeout", 408),
                    uncertain=mutation and lease is not None and lease.business_started,
                )
            except (ValidationError, ValueError, KeyError, TypeError):
                return failed(
                    Fault("invalid_input", 400),
                    uncertain=mutation and lease is not None and lease.business_started,
                )
            except (sqlite3.Error, OSError):
                return failed(
                    Fault("dependency_unavailable", 503),
                    uncertain=mutation and lease is not None and lease.business_started,
                )
            finally:
                if lease is not None:
                    lease.finish()

        return endpoint

    for operation in DEFINITIONS:
        app.add_api_route(
            "/internal/v1/memory/context/" + operation,
            endpoint_for(operation),
            methods=["POST"],
            name="memory_context_" + operation,
        )
