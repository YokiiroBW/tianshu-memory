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
    "history": ("request", "response"),
    "input_history": ("request", "response"),
    "propose": ("proposal_request", "receipt"),
    "receipt": ("receipt_request", "receipt_response"),
    "batch": ("batch_request", "batch_response"),
    "association": ("association_request", "association_response"),
    "query_v2": ("query_request", "query_response"),
    "verify_v2": ("verify_request", "verify_response"),
    "forget_v2": ("forget_request", "forget_response"),
}


def mount(app, application, auth, admission, *, body_timeout, execute_timeout):
    def endpoint_for(operation):
        async def endpoint(request: Request):
            lease = None
            request_id = "request-unavailable"
            mutation = operation in {"propose", "association", "forget_v2"}
            version_two = operation.endswith("_v2")

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
                if version_two and application.service.contracts.context_v2_version is None:
                    application.service.contracts.load_memory_v2()
                require(
                    application.service.contracts.context_version == "1.1.0",
                    "dependency_unavailable",
                    503,
                )
                lease = admission.claim()
                async with asyncio.timeout(execute_timeout):
                    caller_name, caller = await lease.run(
                        auth.authenticate, request.headers.get("authorization")
                    )
                history = operation in {"history", "input_history"}
                permission = (
                    "context_propose"
                    if operation == "forget_v2"
                    else "context_query"
                    if history or version_two
                    else "context_" + operation
                )
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
                package = (
                    "memory-context-v2"
                    if version_two
                    else "context-history"
                    if history
                    else "memory-context"
                )
                application.service.contracts.validate(package + "#" + input_type, payload)
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
                        if operation == "verify_v2":
                            contexts = []
                            for check in payload["checks"]:
                                required = (
                                    "select_profiles"
                                    if check["kind"] == "profile"
                                    else "context_query"
                                )
                                require(required in current_caller.get("operations", []))
                                contexts.append(
                                    auth.resolve(
                                        current_name,
                                        current_caller,
                                        check["request"]["query"]["origin"]["assertion_ref"],
                                        request_id,
                                    )
                                )
                            result = application.verify_v2(payload, context, contexts)
                        else:
                            result = getattr(application, operation)(payload, context)
                        application.service.contracts.validate(package + "#" + output_type, result)
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
        path = (
            "/internal/v2/memory/context/" + operation.removesuffix("_v2")
            if operation.endswith("_v2")
            else "/internal/v1/memory/context/" + operation.replace("_", "-")
        )
        app.add_api_route(
            path,
            endpoint_for(operation),
            methods=["POST"],
            name="memory_context_" + operation,
        )
