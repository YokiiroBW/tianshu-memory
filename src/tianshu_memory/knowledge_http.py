"""Restricted loopback HTTP entry for project knowledge and research notes.

A thin transport for one already-integrated application. It adds no rule of its own:

- every request builds a fresh `KnowledgeApplication` and calls only its public `execute`;
- the identity is the fixed knowledge client the operator named when the process started, so no
  field of a request body, query string or header can choose who the caller is;
- the credential is whatever `Authorization: Bearer` carried, exactly as the CLI and MCP
  entrypoints pass their credential, so `knowledge.clients` stays the only authority for a
  digest, a project list and a permission list;
- no database, source context, project adapter or other product client is held here, and the
  domain never imports this module.

The route surface is fixed: one health route and one action route, with a ten-operation
allowlist. Everything else — authorization, idempotency, version conflicts, byte budgets,
evidence freshness and the transaction boundary — is decided inside the domain that owns it.

The boundaries enforced here are transport concerns only:

- strictly bounded request reading: at most 262144 bytes counted while streaming, never from
  `Content-Length`, within a 10 second read deadline;
- at most 4 executes running at once; a request that arrives while all four are busy is refused
  immediately with 503 instead of waiting in a queue, and a slot is released only once the
  synchronous call it guards has really returned — a timeout or a disconnect never speaks for
  work that is still running, and no write is retried here;
- loopback only: the Host must name 127.0.0.1 or localhost on the port this process was told to
  serve, a browser `Origin` request is refused, there is no CORS or cookie surface, and no proxy
  forwarding header is trusted;
- no OpenAPI/docs/redoc route, no response without `no-store`, and failure bodies that expose
  only a stable code — never a path, a credential, SQL or source text.

Failures are reported in one envelope, `{"status": "failed", "code": "<stable code>"}`. The status
tells a caller how to react, and the code tells it what happened: 401 for a missing or mismatched
credential, 503 for saturation and for a dependency that is down, and 408/413/415 for a deadline, an
oversized body and a media type this entry does not carry. Everything else — including a body whose
shape or argument the domain refuses — is 422 with the refusing layer's own code, so a caller never
has to guess whether a 4xx meant "I could not read your request" or "I read it and refused it".
"""

import asyncio
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .domain import Fault, require, strict_json
from .knowledge import KnowledgeApplication

ACTION_PATH = "/local/v1/project-knowledge/action"
HEALTH_PATH = "/health"
# Exactly the operations this entry may carry: the project-knowledge reads plus the whole
# research-note lifecycle. Every other operation the domain knows stays unreachable from here,
# even for a client that holds its permission.
ALLOWED_OPERATIONS = frozenset(
    {
        "query",
        "recover",
        "check",
        "note_record",
        "note_revise",
        "note_withdraw",
        "note_query",
        "note_recover",
        "note_status",
        "note_check",
    }
)
MAX_BODY_BYTES = 262144
# The deadline covers reading the request and the execute call together, so one request can
# never occupy its admission slot indefinitely.
READ_TIMEOUT_SECONDS = 10.0
MAX_ACTIVE_EXECUTES = 4
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAX_CLIENT_LENGTH = 128
# JSON object keys of the request body. Nothing else is read from the request.
REQUEST_FIELDS = frozenset({"operation", "project_id", "arguments"})
NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# 400/413/415 are transport verdicts and 503 says this process cannot serve the request now
# (all slots busy, or the storage the domain needs is unusable). Every refusal the domain itself
# raised is reported in one 422 envelope with its own code, so a caller never has to guess a
# permission class from a status code.
DOMAIN_STATUS = 422
# The codes the transport itself decides, and the status each is reported with.
TRANSPORT_CODES = frozenset(
    {
        "invalid_host",
        "browser_origin_refused",
        "request_timeout",
        "request_too_large",
        "unsupported",
    }
)
TRANSPORT_STATUS = {
    "invalid_host": 400,
    "browser_origin_refused": 400,
    "request_timeout": 408,
    "request_too_large": 413,
    "unsupported": 415,
}
DEPENDENCY_CODES = frozenset({"overloaded", "dependency_unavailable", "dependency_or_input_error"})


def failed(code: str, status: int) -> JSONResponse:
    """The only failure shape this entry returns: a status and a stable code."""
    return JSONResponse({"status": "failed", "code": code}, status_code=status, headers=NO_STORE)


class Credential:
    """The service credential, read from the operator's environment variable per request.

    Reading it at every request instead of caching it at startup is what lets a rotated or
    cleared variable stop working without a restart. The value never enters a log or a response.
    """

    def __init__(self, name: str):
        self.name = name

    def __call__(self):
        return os.environ.get(self.name)


def load_config(config_path, client):
    """Read the private configuration and confirm this process may serve that client.

    Only what the transport needs is read: the file must exist and parse as strict JSON, and the
    client must be a registered principal. Whether its credential, projects and permissions
    currently authorize anything is decided per request by the domain, so a client that is
    registered but no longer privileged starts normally and is then refused.
    """
    path = Path(config_path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise Fault("invalid_configuration", 503) from None
    try:
        config = strict_json(raw)
    except ValueError:
        raise Fault("invalid_configuration", 503) from None
    require(isinstance(config, dict), "invalid_configuration", 503)
    knowledge = config.get("knowledge")
    require(isinstance(knowledge, dict), "invalid_configuration", 503)
    principal = knowledge.get("clients", {}).get(client)
    require(isinstance(principal, dict) and bool(principal.get("permissions")), "unauthorized", 401)
    return config


def serve_port(port):
    """The one port this process was told to serve; the Host check is built on it."""
    require(type(port) is int and 1 <= port <= 65535, "invalid_configuration", 400)
    return port


def check_host(host, port):
    """Only the loopback authority of this exact port is accepted.

    A request with any other Host — a public name, a DNS name resolving here, or the same host
    on a different port — is refused before the body is read. Forwarded headers are never
    consulted, because this process must not be reachable through a proxy.
    """
    authority = (host or "").strip().lower()
    name, separator, declared = authority.rpartition(":")
    if not separator:
        name, declared = authority, ""
    require(name in LOOPBACK_HOSTS and declared == str(port), "invalid_host", 400)


def check_origin(request):
    """Refuse any request a browser may have initiated.

    The only intended caller is a server-side connector on this machine. A browser sends
    `Origin` (and may send a `Sec-Fetch-*` header); either one means the request came from a
    page, for which this entry has no authenticated user model.
    """
    require(
        request.headers.get("origin") is None and request.headers.get("sec-fetch-site") is None,
        "browser_origin_refused",
        400,
    )


def bearer(request):
    """The credential exactly as the caller presented it, or a refusal."""
    header = request.headers.get("authorization")
    require(
        isinstance(header, str) and header.startswith("Bearer ") and len(header) > len("Bearer "),
        "unauthorized",
        401,
    )
    return header[len("Bearer ") :]


async def read_body(request, timeout):
    """Read at most `MAX_BODY_BYTES`, counting streaming chunks within a read deadline.

    `Content-Length` is never trusted: the limit is enforced on the bytes that actually arrive,
    and a body that keeps arriving past it is refused with 413.
    """
    body = bytearray()
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise Fault("request_too_large", 413)
    except TimeoutError:
        raise Fault("request_timeout", 408) from None
    except Fault:
        raise
    except (ValueError, RuntimeError):
        # A truncated or abandoned body is an input failure, never a completed request.
        raise Fault("invalid_input", 400) from None
    return bytes(body)


def parse_body(raw):
    """One strict JSON object carrying exactly the three request fields."""
    try:
        payload = strict_json(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Fault("invalid_input", 400) from None
    require(isinstance(payload, dict) and set(payload) == REQUEST_FIELDS, "invalid_input", 400)
    operation, project_id, arguments = (
        payload["operation"],
        payload["project_id"],
        payload["arguments"],
    )
    # A field of the wrong type is malformed input; a well-formed name outside the allowlist is an
    # operation this entry does not carry. The two are reported differently on purpose.
    require(
        isinstance(operation, str) and isinstance(project_id, str) and isinstance(arguments, dict),
        "invalid_input",
        400,
    )
    require(operation in ALLOWED_OPERATIONS, "unsupported", 415)
    require(0 < len(project_id) <= 128, "invalid_input", 400)
    return payload


def create_app(config_path, client, port, *, credential=None, read_timeout=None):
    """Build the entry for one fixed knowledge client on one explicit loopback port.

    The factory refuses to build an application whose private configuration is missing or
    unparseable, whose named client is not registered, or whose port is not a real port: a
    process that cannot serve its client must not start and then look alive.
    """
    load_config(config_path, client)
    require(isinstance(client, str) and 0 < len(client) <= MAX_CLIENT_LENGTH, "invalid_input", 400)
    port = serve_port(port)
    if credential is None:
        credential = Credential("TIANSHU_PROJECT_CREDENTIAL")
    require(callable(credential), "invalid_configuration", 400)
    deadline = READ_TIMEOUT_SECONDS if read_timeout is None else float(read_timeout)
    require(deadline > 0, "invalid_configuration", 400)

    app = FastAPI(
        title="Tianshu project knowledge (restricted loopback entry)",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.knowledge_client = client
    app.state.credential = credential
    # The only mutable transport state: how many executes are running, plus one optional call
    # observer. Neither carries a project, a credential or a request body between requests.
    app.state.active = 0
    app.state.observer = None

    def claim_slot():
        """Take one of the bounded execute slots, or report that none is free.

        This is a plain counter rather than a semaphore with a waiting queue: the policy is that a
        request arriving when every slot is busy is *refused*, never parked, so there is nothing
        to wait on. The counter is only ever touched from the event loop (the worker thread never
        releases a slot itself), so no lock is needed to keep it exact.
        """
        if app.state.active >= MAX_ACTIVE_EXECUTES:
            return False
        app.state.active += 1
        return True

    def release_slot():
        app.state.active -= 1

    def released(task):
        """Free the slot an abandoned call was holding, and consume its outcome.

        The caller is gone by then, so nothing can be reported; the only honest thing left is to
        retrieve the outcome instead of leaving an unretrieved exception behind. The result the
        domain recorded in its own idempotency ledger stays the record a replay reads.
        """
        try:
            task.exception()
        except BaseException:  # noqa: BLE001 - a cancelled worker still needs its slot back
            pass
        release_slot()

    def application():
        """One request's own application, built and thrown away.

        `KnowledgeApplication._context` rewrites the project, client, permission and domain flags
        of the instance it runs on, and the planner updates its domain flags, so an instance kept
        across requests would let one request's authorized project leak into the next. A fresh
        instance per request makes that impossible by construction, and only the public
        `execute` of that instance is ever called.
        """
        return KnowledgeApplication(config_path)

    def call(body, presented):
        """Run one operation through this request's own application."""
        observer = app.state.observer
        if observer is None:
            return application().execute(body, client=client, credential=presented)
        # The observer replaces the call, never a rule of it: it receives the same body and the
        # same presented credential a production request would, and an observation that does not
        # perform the call simply performs nothing.
        return observer(body, presented, application)

    async def run_operation(body, presented):
        """One operation, bounded by an admission slot its call itself releases."""
        if not claim_slot():
            # No queue and no wait: this process is already running as many operations as it is
            # allowed to, and the caller is told to come back rather than being parked.
            raise Fault("overloaded", 503)
        work = asyncio.ensure_future(run_in_threadpool(call, body, presented))
        try:
            # Shielding keeps this frame's cancellation — a disconnect, or the deadline below —
            # from cancelling the worker call, so a response can end while its operation runs on.
            return await asyncio.wait_for(asyncio.shield(work), timeout=deadline)
        except TimeoutError:
            # The operation is still running and may still commit. This entry never claims it was
            # cancelled and never retries it: the caller replays the same idempotent request, or
            # reads its recorded result, once it has returned.
            raise Fault("request_timeout", 408) from None
        finally:
            # The slot is returned when the *call* is over, never when the response is written: a
            # timeout or a disconnect must not free a slot that still has work in it, because
            # that is exactly how a bounded process turns into an unbounded one.
            if work.done():
                release_slot()
            else:
                work.add_done_callback(released)

    @app.get(HEALTH_PATH, status_code=200)
    async def health(request: Request):
        """Whether this entry is listening — and nothing about any project's data.

        No project is opened, migrated or read here, so `listening` must never be read as "the
        projects are migrated and readable". Every actual operation still decides that itself and
        fails closed with the storage error the domain raises.
        """
        check_host(request.headers.get("host"), port)
        check_origin(request)
        return JSONResponse(
            {"state": "listening", "entrypoint": "project_knowledge_http", "projects": None},
            status_code=200,
            headers=NO_STORE,
        )

    @app.post(ACTION_PATH, status_code=200)
    async def action(request: Request):
        """One complete project-knowledge or research-note operation."""
        check_host(request.headers.get("host"), port)
        check_origin(request)
        presented = bearer(request)
        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        require(media_type == "application/json", "unsupported", 415)
        body = parse_body(await read_body(request, deadline))
        return JSONResponse(await run_operation(body, presented), status_code=200, headers=NO_STORE)

    @app.exception_handler(Fault)
    async def fault_handler(request: Request, error: Fault):
        """Transport verdicts keep their own status; refusals become one caller-visible envelope.

        The card fixes a 422 envelope for refusals, so any domain code carrying one of the
        transport's own statuses (400 for a too-small byte budget, 413 for an oversized write) is
        reported as 422 with its code: a caller must never have to guess whether a 400 means "your
        request was malformed" or "your request was understood and refused". A missing credential
        is the one identity failure a caller may distinguish, and it is decided here, before any
        domain call.
        """
        if error.code == "unauthorized":
            return failed(error.code, 401)
        if error.code in DEPENDENCY_CODES:
            return failed(error.code, 503)
        if error.code in TRANSPORT_CODES:
            return failed(error.code, TRANSPORT_STATUS[error.code])
        return failed(error.code, DOMAIN_STATUS)

    @app.exception_handler(StarletteHTTPException)
    async def framework_handler(request: Request, error: StarletteHTTPException):
        """Framework verdicts keep this entry's failure shape instead of a bare status page.

        A wrong method or an unknown path is answered with a code, and a request that never
        reached a route is never reported as a served operation.
        """
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code)
        if code is None:
            return failed("invalid_input", 400)
        return failed(code, error.status_code)

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    @app.exception_handler(ValueError)
    @app.exception_handler(ImportError)
    async def dependency_handler(request: Request, error: Exception):
        """Everything the domain could not complete is one code, never a traceback.

        A missing table, an absent migration, an unreadable file and a locked database are all
        "this process cannot answer now"; the response carries no path, no SQL and no detail. A
        defect of this module's own — a `TypeError`, an `AttributeError` — is deliberately not
        caught here: reporting the process's own bug as a dependency outage would hide it.
        """
        return failed("dependency_unavailable", 503)

    return app
