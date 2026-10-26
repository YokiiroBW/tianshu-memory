"""The actor port has its own admission/lifecycle; user ports stay independent."""

import asyncio
import sqlite3
import threading
from dataclasses import asdict

from fastapi import Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from starlette.requests import ClientDisconnect
from tianshu_storage import StorageOptions

from ..chat_requests import Requests
from ..diagnostics import anote_authenticated, note_fault
from ..domain import Fault, fingerprint, require, strict_json
from .application import COMMANDS, Application
from .authority import Authority
from .contracts import Contracts
from .store import Store


class Endpoint:
    def __init__(self, auth, *, max_active):
        self.auth = auth
        self.admission = Requests(max_active)
        self.application = None
        self.registration = None
        self.lock = threading.RLock()
        self.leases = set()
        self.closed = False

    def get(self):
        require(not self.closed and self.auth is not None, "dependency_unavailable", 503)
        config = self.auth.config()
        section = config.get("actor_memory")
        require(isinstance(section, dict), "dependency_unavailable", 503)
        registration = fingerprint(
            {
                "paths": {
                    key: section.get(key)
                    for key in (
                        "database_path",
                        "state_directory",
                        "guard_path",
                        "contract_directory",
                    )
                },
                "storage": asdict(StorageOptions.from_settings(config)),
                "user_database_path": config.get("database_path"),
                "user_guard_path": config.get("source_sync", {}).get("recovery_path"),
            }
        )
        with self.lock:
            require(not self.closed, "dependency_unavailable", 503)
            if self.application is None:
                try:
                    contracts = Contracts(section["contract_directory"])
                    authority = Authority(self.auth.path, contracts)
                    try:
                        store = Store(config)
                    except BaseException:
                        authority.close()
                        raise
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                    raise Fault("dependency_unavailable", 503) from None
                self.application = Application(store, contracts, authority)
                self.registration = registration
            require(self.registration == registration, "source_changed", 409)
            return self.application

    async def aclose(self):
        self.closed = True
        tasks = [lease.task for lease in self.leases if lease.task is not None]
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
        if self.application is not None:
            self.application.close()


def mount(app, auth, *, body_timeout, execute_timeout, max_active):
    endpoint = Endpoint(auth, max_active=max_active)
    app.state.actor_memory = endpoint

    @app.post("/internal/v1/actor-self-memory/operate")
    async def operate(request: Request):
        lease, request_id, payload = None, "req:unavailable", None
        try:
            require(not endpoint.closed and auth is not None, "dependency_unavailable", 503)
            lease = endpoint.admission.claim()
            endpoint.leases.add(lease)
            caller_name, caller = await lease.run(
                auth.authenticate, request.headers.get("authorization")
            )
            require(caller.get("actor_memory_operations"), "forbidden", 403)
            require(await anote_authenticated(), "dependency_unavailable", 503)
            require(
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "application/json",
                "invalid_input",
                400,
            )
            raw = bytearray()
            async with asyncio.timeout(body_timeout):
                async for chunk in request.stream():
                    require(len(raw) + len(chunk) <= 262144, "invalid_input", 413)
                    raw.extend(chunk)
            payload = strict_json(raw)
            if isinstance(payload, dict) and isinstance(payload.get("request_id"), str):
                request_id = payload["request_id"]

            def authorize():
                name, current = auth.authenticate(request.headers.get("authorization"))
                require(
                    name == caller_name
                    and payload["operation"] in current.get("actor_memory_operations", []),
                    "forbidden",
                    403,
                )
                require(
                    payload["actor_id"] in current.get("allowed_actors", [])
                    or current.get("allow_runtime_roles") is True,
                    "forbidden",
                    403,
                )
                return name

            def execute():
                authorize()
                application = endpoint.get()
                return application.execute(payload, caller=caller_name, recheck=authorize)

            lease.request = request
            async with asyncio.timeout(execute_timeout):
                result = await lease.run(execute, business=True)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Fault as error:
            code = "capacity_exceeded" if error.code == "queue_full" else error.code
            status, execution = error.status, "not_started"
        except (ValidationError, ValueError, TypeError, KeyError, UnicodeError):
            code, status, execution = "invalid_input", 400, "not_started"
        except (TimeoutError, ClientDisconnect, OSError, sqlite3.Error):
            execution = (
                "unknown"
                if lease is not None
                and lease.business_started
                and isinstance(payload, dict)
                and payload.get("operation") in COMMANDS
                else "not_started"
            )
            code, status = ("unknown" if execution == "unknown" else "dependency_unavailable"), 503
        finally:
            if lease is not None:
                lease.finish()
                if lease.task is not None and not lease.task.done():
                    lease.task.add_done_callback(lambda task: endpoint.leases.discard(lease))
                else:
                    endpoint.leases.discard(lease)
        note_fault(code)
        # Invalid body request IDs cannot be echoed as unbounded diagnostic text.
        if len(request_id) > 128:
            request_id = "req:unavailable"
        return JSONResponse(
            dict(
                schema_version=1,
                request_id=request_id,
                code=code,
                retryable=code in {"dependency_unavailable", "capacity_exceeded"}
                and execution != "unknown",
                execution=execution,
            ),
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    return endpoint
