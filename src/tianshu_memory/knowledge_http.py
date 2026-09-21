"""Restricted loopback HTTP entry for project knowledge and research notes.

A thin transport for one already-integrated application. It adds no rule of its own:

- every request builds a fresh `KnowledgeApplication` and calls only its public `execute`;
- the identity is the fixed knowledge client the operator named when the process started, so no
  field of a request body, query string or header can choose who the caller is;
- the credential is whatever `Authorization: Bearer` carried, exactly as the CLI and MCP
  entrypoints pass their credential, so `knowledge.clients` stays the only authority for a
  digest, a project list and a permission list. No server-side secret is configured here, and no
  environment variable is read;
- no database, source context, project adapter or other product client is held here, and the
  domain never imports this module.

The route surface is fixed: one health route and one action route, with a twelve-operation
allowlist. Everything else — authorization, idempotency, version conflicts, byte budgets,
pagination cursors, evidence freshness and the transaction boundary — is decided inside the
domain that owns it.

The boundaries enforced here are transport concerns only:

- admission before everything: one of four slots is taken *before* a single byte of the body is
  read, so the bound covers the whole admitted request — reading its body *and* the synchronous
  execute it starts. A request that arrives with every slot busy is refused immediately with 503
  and never reads its body, so there is no waiting queue and no second, unbounded queue of slow
  bodies. A slot is given up when the read failed or no synchronous call was ever started, and
  otherwise only when that call has really returned: a read timeout, a disconnect or an execute
  wait timeout ends the *response*, never the work, and never frees a slot that still has work in
  it;
- two independent 10 second phases: reading the request body, and waiting for the synchronous
  execute. Either one that expires is reported as 408 `request_timeout` and never as a claim that
  the operation was cancelled;
- strictly bounded request reading: at most 262144 bytes counted while streaming, never from
  `Content-Length`;
- loopback only: the Host must name 127.0.0.1 or localhost on the port this process was told to
  serve, a browser `Origin` request is refused, there is no CORS or cookie surface, and no proxy
  forwarding header is trusted;
- no OpenAPI/docs/redoc route, no response without `no-store`, and failure bodies that expose
  only a stable code — never a path, a credential, SQL or source text.

Failures are reported in one envelope, `{"status": "failed", "code": "<stable code>"}`, and the
status says which *layer* refused the request, never which permission class was missing:

- this module's own verdicts on the request itself keep their transport status: 400 for a
  malformed body or a bad field, 413 for a body over the limit, 415 for a media type or operation
  this entry does not carry, 408 for either phase's deadline, 401 for a missing or malformed
  `Authorization: Bearer`, 400 for a Host or `Origin` this entry does not serve, 503 for
  saturation and for a dependency that is down;
- *everything* the domain's own `execute` refuses is 422 with the domain's own code, whatever the
  code happens to be called. A code name is never used to guess which layer raised it: a domain
  refusal named `invalid_input` is a 422, and this module's own malformed-body verdict with the
  same name is a 400, because they come from different places.
"""

import asyncio
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

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
        # The paginated directory and the complete-block reader are two more reads of the same
        # already-integrated application. They change nothing the transport decides: admission,
        # identity, media type and both deadlines are the same rules, and each of them is still
        # authorized per request by `knowledge.clients`.
        "document_list",
        "document_read",
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
# Two independent phases, each bounded on its own: reading the request body, and waiting for the
# synchronous execute the request started. Together with the four slots this is what keeps one
# request from occupying the process indefinitely.
READ_TIMEOUT_SECONDS = 10.0
EXECUTE_TIMEOUT_SECONDS = 10.0
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
# Every refusal the domain's execute raises is reported as 422 with its own code, so a caller
# never has to guess a permission class — or a layer — from a status code.
DOMAIN_STATUS = 422
# The statuses this module's own verdicts are reported with. The failure of a dependency (a
# missing table, an unreadable file) is `dependency_unavailable`, not one of these.
STATUS_DEPENDENCY = 503
STATUS_SATURATED = 503
STATUS_DEADLINE = 408
STATUS_BEARER = 401
STATUS_BODY = 400
STATUS_AUTHORITY = 400
STATUS_TOO_LARGE = 413
STATUS_MEDIA = 415


class Refusal(Fault):
    """A `Fault` that came out of the domain's `execute` and is reported as a 422.

    The domain's own refusals carry their natural status (a missing credential is a 401 there, a
    malformed argument a 400). On this wire every one of them is a 422 with its code preserved,
    and the only reliable way to know that a refusal came from the domain is to mark it where it
    crosses the `execute` boundary: a code name is not a layer. `domain.Fault` itself is the
    product's shared vocabulary and is not modified.
    """

    def __init__(self, code):
        super().__init__(code, DOMAIN_STATUS)
        self.code = code
        # Set explicitly rather than inherited: this type *is* the statement "the domain refused
        # it", and the status is the same for every one of them whatever the domain said.
        self.status = DOMAIN_STATUS


def transport(code, status):
    """This module's own verdict on a request, carrying the status it is reported with."""
    return Fault(code, status)


def failed(code, status) -> JSONResponse:
    """The only failure shape this entry returns: a status and a stable code."""
    return JSONResponse({"status": "failed", "code": code}, status_code=status, headers=NO_STORE)


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
        raise transport("invalid_configuration", STATUS_DEPENDENCY) from None
    try:
        config = strict_json(raw)
    except ValueError:
        raise transport("invalid_configuration", STATUS_DEPENDENCY) from None
    require(isinstance(config, dict), "invalid_configuration", STATUS_DEPENDENCY)
    knowledge = config.get("knowledge")
    require(isinstance(knowledge, dict), "invalid_configuration", STATUS_DEPENDENCY)
    principal = knowledge.get("clients", {}).get(client)
    # A client that is not a registered principal cannot be served at all: the process refuses to
    # start rather than answer every request with a credential refusal while looking healthy.
    require(
        isinstance(principal, dict) and bool(principal.get("permissions")),
        "unregistered_client",
        STATUS_BEARER,
    )
    return config


def serve_port(port):
    """The one port this process was told to serve; the Host check is built on it."""
    require(type(port) is int and 1 <= port <= 65535, "invalid_configuration", STATUS_AUTHORITY)
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
    require(name in LOOPBACK_HOSTS and declared == str(port), "invalid_host", STATUS_AUTHORITY)


def check_origin(request):
    """Refuse any request a browser may have initiated.

    The only intended caller is a server-side connector on this machine. A browser sends
    `Origin` (and may send a `Sec-Fetch-*` header); either one means the request came from a
    page, for which this entry has no authenticated user model.
    """
    require(
        request.headers.get("origin") is None and request.headers.get("sec-fetch-site") is None,
        "browser_origin_refused",
        STATUS_AUTHORITY,
    )


def bearer(request):
    """The credential exactly as the caller presented it, or a refusal.

    The presented value *is* the credential: it is handed to the existing `execute`, which
    compares it against `knowledge.clients`. This entry holds no secret of its own and reads no
    environment variable, so nothing here can authorize a request that the domain would not.
    """
    header = request.headers.get("authorization")
    require(
        isinstance(header, str) and header.startswith("Bearer ") and len(header) > len("Bearer "),
        "unauthorized",
        STATUS_BEARER,
    )
    return header[len("Bearer ") :]


def check_media_type(request):
    """Only a JSON body is carried here."""
    media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    require(media_type == "application/json", "unsupported", STATUS_MEDIA)


async def read_body(request, deadline):
    """Read at most `MAX_BODY_BYTES`, counting streaming chunks within a read deadline.

    `Content-Length` is never trusted: the limit is enforced on the bytes that actually arrive,
    and a body that keeps arriving past it is refused with 413. A body that stops arriving is a
    408, and a body the peer abandoned is a 400 — all three are verdicts about the request, and
    the caller of this function still holds its admission slot while they are reached.
    """
    body = bytearray()
    try:
        async with asyncio.timeout(deadline):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise transport("request_too_large", STATUS_TOO_LARGE)
    except TimeoutError:
        raise transport("request_timeout", STATUS_DEADLINE) from None
    except Fault:
        raise
    except ClientDisconnect:
        # The peer went away mid-body. This is not a completed request, and it is reported as the
        # malformed input it is rather than as a defect of this process.
        raise transport("invalid_input", STATUS_BODY) from None
    except (ValueError, RuntimeError):
        # A truncated or otherwise unusable body is never a completed request either.
        raise transport("invalid_input", STATUS_BODY) from None
    return bytes(body)


def parse_body(raw):
    """One strict JSON object carrying exactly the three request fields.

    These are this module's own verdicts on the request text, so they keep the transport statuses
    the card fixes for input: a non-JSON body is a 415 at the media-type check, and a JSON body
    whose shape or fields this entry will not act on is a 400. The domain's identically named
    `invalid_input` refusal stays a 422 with its own code — see `Refusal`.
    """
    try:
        payload = strict_json(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise transport("invalid_input", STATUS_BODY) from None
    require(
        isinstance(payload, dict) and set(payload) == REQUEST_FIELDS, "invalid_input", STATUS_BODY
    )
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
        STATUS_BODY,
    )
    require(operation in ALLOWED_OPERATIONS, "unsupported", STATUS_MEDIA)
    require(0 < len(project_id) <= MAX_CLIENT_LENGTH, "invalid_input", STATUS_BODY)
    return payload


def create_app(config_path, client, port, *, body_timeout=None, execute_timeout=None):
    """Build the entry for one fixed knowledge client on one explicit loopback port.

    The factory refuses to build an application whose private configuration is missing or
    unparseable, whose named client is not registered, or whose port is not a real port: a
    process that cannot serve its client must not start and then look alive. It holds no
    credential: every request presents its own, and the domain decides whether it authorizes
    anything.

    The two phase limits exist so a test can exercise a deadline without waiting ten seconds. The
    wire contract is the defaults; production never passes them.
    """
    load_config(config_path, client)
    require(
        isinstance(client, str) and 0 < len(client) <= MAX_CLIENT_LENGTH,
        "invalid_input",
        STATUS_BODY,
    )
    port = serve_port(port)
    read_deadline = READ_TIMEOUT_SECONDS if body_timeout is None else float(body_timeout)
    execute_deadline = (
        EXECUTE_TIMEOUT_SECONDS if execute_timeout is None else float(execute_timeout)
    )
    require(read_deadline > 0, "invalid_configuration", STATUS_AUTHORITY)
    require(execute_deadline > 0, "invalid_configuration", STATUS_AUTHORITY)

    app = FastAPI(
        title="Tianshu project knowledge (restricted loopback entry)",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.knowledge_client = client
    # The only mutable transport state: how many admitted requests are in flight, plus two
    # optional observation points. None of them carries a project, a credential or a request body
    # between requests.
    app.state.active = 0
    app.state.observer = None
    app.state.settle = None

    def claim_slot():
        """Take one of the four admission slots, or report that none is free.

        This is a plain counter rather than a semaphore with a waiting queue: the policy is that a
        request arriving when every slot is busy is *refused*, never parked, so there is nothing
        to wait on. The claim happens before the body is read and is never deferred to after it:
        a check that ran early and claimed late would let a burst of requests slip through the gap
        between the two and put an unbounded number of slow bodies in flight. The counter is only
        ever touched from the event loop (the worker thread never releases a slot itself), so no
        lock is needed to keep it exact and no request can be counted twice.
        """
        if app.state.active >= MAX_ACTIVE_EXECUTES:
            return False
        app.state.active += 1
        return True

    class Lease:
        """One request's ownership of one admission slot, released exactly once.

        The slot has three possible owners in sequence — the request frame while it reads the body
        and waits, the running call itself if the response ends first, and nobody once either has
        finished — and a request can end in ways its own code never sees (a cancellation while the
        body is still arriving). Keeping the ownership in one object with one `release` makes every
        path land on the same single decrement: a request can neither leak its slot nor give it
        back twice, which would let the process run more work than the bound allows.
        """

        def __init__(self):
            self.owner = "request"

        def hand_off(self):
            """Give the slot to the running call, which will release it when it really returns."""
            self.owner = "call"

        def release(self):
            if self.owner == "request":
                self.owner = "nobody"
                app.state.active -= 1

    def released(task, lease):
        """Free the slot an abandoned call was holding, and consume its outcome.

        The caller is gone by then, so nothing can be reported; the only honest thing left is to
        retrieve the outcome instead of leaving an unretrieved exception behind. The result the
        domain recorded in its own idempotency ledger stays the record a replay reads.
        """
        try:
            task.exception()
        except BaseException:  # noqa: BLE001 - a cancelled worker still needs its slot back
            pass
        lease.owner = "request"
        lease.release()

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
        """Run one operation through this request's own application, marking a domain refusal.

        This is the boundary the card asks for: whatever the domain's `execute` raises is marked
        *here*, where it crosses out of the domain, and everything else this module raises later —
        a saturated entry, an expired phase, an unreadable body — is its own verdict and keeps its
        own status. No code name is ever consulted to decide which layer spoke.
        """
        observer = app.state.observer
        try:
            if observer is None:
                result = application().execute(body, client=client, credential=presented)
                settle = app.state.settle
                if settle is not None:
                    settle()
                return result
            # The observer replaces the call, never a rule of it: it receives the same body and
            # the same presented credential a production request would, and an observation that
            # does not perform the call simply performs nothing.
            return observer(body, presented, application)
        except Fault as error:
            raise Refusal(error.code) from None

    async def run_operation(body, presented, lease):
        """Run the synchronous call while this request's slot is held until it really returns.

        The waiting is shielded, so ending the *response* — the deadline below, or a disconnect —
        never cancels the worker that is already writing. The slot therefore has exactly three
        exits: the call finished, the call was cancelled before it could start, or the response
        ended first and the call's own completion releases it later. Every path releases once,
        through the one lease.
        """
        work = asyncio.ensure_future(run_in_threadpool(call, body, presented))
        lease.hand_off()
        try:
            # Shielding keeps this frame's cancellation from cancelling the worker call, so the
            # response can end while the operation runs on and still commits.
            return await asyncio.wait_for(asyncio.shield(work), timeout=execute_deadline)
        except TimeoutError:
            # The operation is still running and may still commit. This entry never claims it was
            # cancelled and never retries it: the caller replays the same idempotent request, or
            # reads its recorded result, once it has returned.
            raise transport("request_timeout", STATUS_DEADLINE) from None
        finally:
            if work.done():
                work.exception()
                lease.owner = "request"
                lease.release()
            else:
                work.add_done_callback(lambda task: released(task, lease))

    @app.get(HEALTH_PATH, status_code=200)
    async def health(request: Request):
        """Whether this entry is listening — and nothing about any project's data.

        No project is opened, migrated or read here, so `listening` must never be read as "the
        projects are migrated and readable". Every actual operation still decides that itself and
        fails closed with the storage error the domain raises. This route neither reads a body nor
        takes one of the four slots: it does no work that a slot bounds.
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
        check_media_type(request)
        if not claim_slot():
            # No queue and no wait, and no body is read: this request is refused with the bound
            # still intact, whatever the sender was still going to write.
            raise transport("overloaded", STATUS_SATURATED)
        lease = Lease()
        try:
            try:
                body = parse_body(await read_body(request, read_deadline))
            except Fault:
                # The read failed, so no call of this request is running and the slot goes back.
                raise
            return JSONResponse(
                await run_operation(body, presented, lease), status_code=200, headers=NO_STORE
            )
        except Fault:
            raise
        except (sqlite3.Error, OSError, ValueError, ImportError):
            raise transport("dependency_unavailable", STATUS_DEPENDENCY) from None
        finally:
            # The one place a request that never handed its slot to a call gives it back: a read
            # that failed, a body that never parsed, a cancellation while the body was arriving, or
            # the response being written. Once a call owns the slot this is a no-op, so the slot
            # still lives until that call really returns.
            lease.release()

    @app.exception_handler(Fault)
    async def fault_handler(request: Request, error: Fault):
        """Report each refusal with the status of the layer that produced it.

        A `Refusal` is a domain refusal and is always 422 with the domain's code. Anything else
        raised here is this module's own verdict and keeps the status it was declared with, so the
        code name is never consulted to decide the layer.
        """
        if isinstance(error, Refusal):
            return failed(error.code, DOMAIN_STATUS)
        return failed(error.code, error.status)

    @app.exception_handler(StarletteHTTPException)
    async def framework_handler(request: Request, error: StarletteHTTPException):
        """Framework verdicts keep this entry's failure shape instead of a bare status page.

        A wrong method or an unknown path is answered with a code, and a request that never
        reached a route is never reported as a served operation.
        """
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code)
        if code is None:
            return failed("invalid_input", error.status_code)
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
        return failed("dependency_unavailable", STATUS_DEPENDENCY)

    return app
