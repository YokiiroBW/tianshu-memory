"""Safe runtime event log for the two Memory HTTP services.

This module is the product's *own* diagnostic adapter. It is deliberately independent of the
domain: it holds no Store, no source authority, no knowledge application, and no domain module
imports it except through an explicit assembly call. It therefore never becomes a second
business database and can never be consulted to decide an operation.

What it guarantees, and how:

- **One closed record shape.** Every line is UTF-8 JSON with exactly the twelve fields the
  coordinator's `contracts/diagnostics/v1` fixes. The event name and the error code must come
  from the enums registered *here*, in product source, so a caller-provided string is rejected
  even when it happens to match the schema's own pattern. There is no field that could carry a
  message, an extra, a stack, a body, a header, a path, a project, an account or a SQL string —
  a secret has nowhere to go, which is a stronger statement than a redaction filter.
- **One full-coverage stream, never sampled.** `emit` is called explicitly at each registered
  seam. There is no level filter, no success filter and no sampling.
- **Ordered, unique sequence numbers.** The number is allocated inside the same lock that writes
  the line, so the file's sequence column is strictly increasing and never repeats, whatever the
  thread that produced the event.
- **A durable, bounded local file.** Main output is one JSONL file per process inside the
  explicitly configured `TIANSHU_LOG_DIR`; deployment mounts it at `/var/log/tianshu`. Lines are
  written under a lock and flushed, and each event is `fsync`ed, so an acceptance or completion
  event is on disk before the business work it describes returns.
- **Fail closed, and never twice.** When a write cannot be persisted the adapter sets a latch:
  readiness becomes false, new business requests are refused with 503, one fixed safe warning goes
  to stderr, and nothing is retried, re-executed or rolled back to make logging succeed again.
  Existing committed results keep the receipt they already returned.

`runtime_probes` reads the latch; the probe itself never writes here.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from .domain import Fault, require

SCHEMA_VERSION = "1.0.0"
# The two registered Memory identities. The chat/identity entry is `memory` and the
# project-knowledge entry is its own registered identity, so an operator can tell the two
# processes apart in one collected stream.
CHAT_SERVICE = "memory"
KNOWLEDGE_SERVICE = "memory-knowledge"
SERVICES = frozenset({CHAT_SERVICE, KNOWLEDGE_SERVICE})

MAX_LINE_BYTES = 4096
DEFAULT_SEGMENT_BYTES = 64 * 1024 * 1024
MIN_DIRECTORY_BYTES = 32 * 1024 * 1024
DEFAULT_DIRECTORY_BYTES = 1024 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024 * 1024

CORRELATION_HEADER = "x-tianshu-correlation-id"
CORRELATION_PATTERN = re.compile(r"[a-f0-9]{32}\Z")
EVENT_PATTERN = re.compile(r"[a-z][a-z0-9_.]{0,63}\Z")
ERROR_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# The registered event names. Anything not in this table cannot be logged: a URL, a request path
# or an exception class name is not an event.
EVENTS = frozenset(
    {
        "runtime.starting",
        "runtime.started",
        "runtime.ready",
        "runtime.start_failed",
        "runtime.stopping",
        "runtime.stopped",
        "request.started",
        "request.authenticated",
        "sync.execute.started",
        "sync.execute.completed",
        "request.completed",
        "log.sink_failed",
    }
)
# The registered error codes. Every one of them is a fixed product verdict; an unknown defect is
# reported as `internal_error` and its detail never leaves the process.
ERROR_CODES = frozenset(
    {
        "unauthorized",
        "forbidden",
        "invalid_input",
        "unsupported_version",
        "not_found",
        "conflict",
        "scope_changed",
        "project_uninitialized",
        "project_conflict",
        "registration_changed",
        "idempotency_conflict",
        "request_too_large",
        "request_timeout",
        "client_disconnected",
        "overloaded",
        "dependency_unavailable",
        "invalid_configuration",
        "invalid_host",
        "browser_origin_refused",
        "internal_error",
        "log_unavailable",
        "log_capacity",
        "log_unwritable",
        "execute_not_started",
    }
)
LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
OUTCOMES = frozenset(
    {"started", "succeeded", "failed", "cancelled", "unknown", "rejected", "degraded"}
)
# The verdict codes this product already uses on the wire, mapped onto the registered enum. A
# code this table does not know is deliberately *not* passed through: an unknown defect is
# `internal_error`, so no product string can become a log value.
FAULT_CODES = {
    "unauthorized": "unauthorized",
    "forbidden": "forbidden",
    "invalid_input": "invalid_input",
    "unsupported_version": "unsupported_version",
    "not_found": "not_found",
    "conflict": "conflict",
    "scope_changed": "scope_changed",
    "project_uninitialized": "project_uninitialized",
    "project_conflict": "project_conflict",
    "registration_changed": "registration_changed",
    "idempotency_conflict": "idempotency_conflict",
    "request_too_large": "request_too_large",
    "request_timeout": "request_timeout",
    "overloaded": "overloaded",
    "dependency_unavailable": "dependency_unavailable",
    "invalid_configuration": "invalid_configuration",
    "invalid_host": "invalid_host",
    "browser_origin_refused": "browser_origin_refused",
    "unregistered_client": "invalid_configuration",
    "unsupported": "invalid_input",
}
# The two deployment probe paths, plus the product's own pre-existing readiness route. A liveness
# poll is not business traffic: it is excluded by exact path so that neither the deployment probes
# nor the product's own health route can ever write a line or advance the sequence, no matter how
# often a supervisor asks.
LEGACY_HEALTH_PATH = "/health"
PROBE_PATHS = frozenset({"/health/live", "/health/ready", LEGACY_HEALTH_PATH})
LIVE_BODY = {"status": "alive"}
# The ASGI scope key carrying the request's validated correlation identifier, so an entry point can
# echo the value this process really used without reading the header or importing this module.
CORRELATION_SCOPE_KEY = "tianshu_correlation_id"
CHECK_STATES = frozenset({"ok", "failed", "not_configured", "not_verified", "non_durable"})
# The checks each service reports. The key set is closed, so a caller of the readiness route can
# rely on exactly these names and on no field beyond them. `assembled` and `owner` are the two
# conditions that cannot be read from a file: this process really is the assembled runtime, and
# the object that owns the database handle has not been released.
CHAT_CHECKS = (
    "configuration",
    "contract",
    "database",
    "guard",
    "mode",
    "log",
    "assembled",
    "owner",
    "remote",
)
KNOWLEDGE_CHECKS = (
    "configuration",
    "contract",
    "database",
    "client",
    "extensions",
    "log",
    "assembled",
    "owner",
    "remote",
)
CHECKS_BY_SERVICE = {CHAT_SERVICE: CHAT_CHECKS, KNOWLEDGE_SERVICE: KNOWLEDGE_CHECKS}
NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# The one fixed warning printed when persistence stops working. It carries no path, no value and
# no exception text, and it is printed exactly once per process.
SINK_WARNING = "tianshu-memory: runtime event log unavailable; readiness fails closed\n"

_scope = ContextVar("tianshu_diagnostics_scope", default=None)


class Hooks:
    """What the installed adapter publishes to the application it belongs to.

    One object per installed application, registered in `app.state` under a fixed name. An entry
    point that must not import this module — the restricted knowledge transport is deliberately the
    thinnest module in the product, and its own suite refuses a new import there — resolves the
    adapter through this object instead, which is why installing the stack is enough for that entry
    to be instrumented without naming any of it.
    """

    __slots__ = ("adapter",)

    def __init__(self, adapter):
        self.adapter = adapter

    def execution(self, name):
        """The diagnostic seam around one synchronous business call of this application."""
        return Execution(self.adapter, name)

    def correlation(self, request):
        """The request's own verified correlation identifier, or None when it presented none."""
        scope = _scope.get()
        if scope is not None:
            return scope.correlation_id
        if request is None:
            return None
        return read_correlation(request.headers.get(CORRELATION_HEADER))


HOOK_NAME = "tianshu_diagnostics"


class SinkUnavailable(RuntimeError):
    """A line could not be persisted. Never raised to a business caller."""


class SinkFull(SinkUnavailable):
    """No free segment is left inside the configured directory limit."""


class Scope:
    """Per-request state shared by the middleware and the seams it instruments.

    Held in a context variable set for the request, so a request's own record can never be read by
    another request. The parallel worker that runs the business call can read it too, which is how
    the *real* end of a call is recorded even when the response has already ended.
    """

    __slots__ = ("authenticated", "correlation_id", "diagnostics", "fault", "started")

    def __init__(self, diagnostics, correlation_id):
        self.diagnostics = diagnostics
        self.correlation_id = correlation_id
        self.started = time.monotonic()
        self.authenticated = None
        self.fault = None


def new_correlation_id():
    """A random 32 character lower-case hex identifier, never derived from any identity."""
    return uuid.uuid4().hex


def read_correlation(header):
    """The presented correlation identifier when it is the exact documented shape, else None.

    An illegal value is never reused, never trimmed into shape and never echoed back: the caller
    gets a freshly generated identifier in the log and nothing about its own string.
    """
    if not isinstance(header, str):
        return None
    return header if CORRELATION_PATTERN.fullmatch(header) else None


def correlation_of(request):
    """The resolved correlation identifier for a request, from the installed scope if present."""
    scope = _scope.get()
    if scope is not None:
        return scope.correlation_id
    return read_correlation(request.headers.get(CORRELATION_HEADER))


def map_fault(code):
    """The registered error code for a product verdict, or the fixed internal one."""
    if not isinstance(code, str):
        return "internal_error"
    return FAULT_CODES.get(code, "internal_error")


def read_config(raw):
    """The two diagnostics keys, validated strictly; every other key is left to its owner.

    `log_directory` is the explicit local log directory (deployment mounts it at
    `/var/log/tianshu`). `diagnostics.token_env` names the environment variable that holds the
    independent readiness token; the token itself is never stored or logged here. A non-durable
    adapter is only ever chosen explicitly: either the key is absent, or it is the literal false.
    Any other value fails closed rather than silently falling back to a stdio stream.
    """
    require(isinstance(raw, dict), "invalid_configuration", 503)
    directory = raw.get("log_directory")
    if directory is None or directory is False:
        directory = None
    else:
        require(
            isinstance(directory, str) and bool(directory.strip()), "invalid_configuration", 503
        )
        resolved = Path(directory).expanduser()
        require(resolved.is_absolute(), "invalid_configuration", 503)
        directory = resolved
    section = raw.get("diagnostics", {})
    require(isinstance(section, dict), "invalid_configuration", 503)
    token_env = section.get("token_env")
    if token_env is not None:
        require(
            isinstance(token_env, str) and bool(token_env.strip()), "invalid_configuration", 503
        )
    return {"log_directory": directory, "token_env": token_env}


class Sink:
    """The bounded, durable JSONL writer: one file per process, one line at a time.

    A single lock covers both the sequence allocation and the write, so sequence order in the file
    is exactly record order. Segments never exceed the segment limit, the directory never exceeds
    the directory limit, and no existing file is ever deleted here: collecting and retaining files
    belongs to the next round, which needs central collection confirmation first.
    """

    def __init__(
        self,
        directory,
        instance_id,
        *,
        segment_bytes=DEFAULT_SEGMENT_BYTES,
        directory_bytes=DEFAULT_DIRECTORY_BYTES,
    ):
        self.directory = Path(directory) if directory is not None else None
        self.instance_id = instance_id
        self.segment_bytes = int(segment_bytes)
        self.directory_bytes = int(directory_bytes)
        self.lock = threading.Lock()
        self.sequence = 0
        self.failure = None
        self.warned = False
        self._handle = None
        self._segment_size = 0

    @property
    def durable(self):
        return self.directory is not None

    def _warn_once(self):
        if not self.warned:
            self.warned = True
            try:
                sys.stderr.write(SINK_WARNING)
                sys.stderr.flush()
            except OSError:  # pragma: no cover - a closed stderr cannot be reported either
                pass

    def fail(self, code):
        """Latch a persistence failure and emit the one fixed warning. Never recovers by itself."""
        if self.failure is None:
            self.failure = code
        self._close()
        self._warn_once()

    def _close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _directory_usage(self):
        """The bytes already present in the directory, including other instances' segments."""
        total = 0
        try:
            for entry in os.scandir(self.directory):
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            raise SinkUnavailable("log directory is not readable") from None
        return total

    def _free_segment(self):
        """The next free segment path, or a refusal when the directory limit is reached.

        A segment this instance already filled is never reused, and no existing file is removed to
        make room. Once this raises, the latch stays set until an explicit maintenance action:
        readiness does not recover by deleting history, and never by deleting it silently.
        """
        used = self._directory_usage()
        position = 0
        for entry in os.scandir(self.directory):
            name = entry.name
            if not name.startswith(self.instance_id + ".") or not name.endswith(".jsonl"):
                continue
            middle = name[len(self.instance_id) + 1 : -len(".jsonl")]
            if middle.isdigit():
                position = max(position, int(middle) + 1)
        if self.segment_bytes > self.directory_bytes - used:
            raise SinkFull("log directory capacity reached")
        return self.directory / f"{self.instance_id}.{position:06d}.jsonl"

    def _ensure_handle(self):
        if self._handle is not None:
            return
        try:
            if not self.directory.is_dir():
                raise SinkUnavailable("log directory is not a directory")
            if not os.access(self.directory, os.W_OK | os.X_OK):
                raise SinkUnavailable("log directory is not writable")
            while True:
                target = self._free_segment()
                if target.exists() and target.stat().st_size >= self.segment_bytes:
                    # A segment this instance already filled, left behind by an earlier process
                    # with the same instance id. Advancing is what keeps lines out of a sealed
                    # file; nothing is overwritten and nothing is removed.
                    raise SinkUnavailable("segment position is already sealed")
                handle = target.open("a", encoding="utf-8", newline="\n")
                self._handle = handle
                self._segment_size = handle.tell()
                return
        except SinkUnavailable:
            raise
        except OSError as error:
            raise SinkUnavailable(f"log segment cannot be opened: {error.errno}") from None

    def write(self, line):
        """Persist one already-validated line. Raises only to the caller's own failure latch.

        With no configured log directory there is no second, quieter sink to fall back to: a
        standard stream is not a durable record and must never be mistaken for one, so the line is
        dropped here and the readiness check reports the sink `non_durable`. That mode exists for a
        development process only — a deployment that claims to be ready on it cannot exist — and
        dropping is also the only behavior that cannot block a serving process on a full pipe it
        does not own.
        """
        encoded = line.encode("utf-8") + b"\n"
        if len(encoded) > MAX_LINE_BYTES:
            raise SinkUnavailable("event line exceeds the contract byte limit")
        if self.directory is None:
            return
        self._ensure_handle()
        if self._segment_size > 0 and self._segment_size + len(encoded) > self.segment_bytes:
            self._close()
            self._ensure_handle()
        handle = self._handle
        handle.write(line)
        handle.write("\n")
        self._segment_size += len(encoded)
        handle.flush()
        os.fsync(handle.fileno())

    def close(self):
        self._close()


class Diagnostics:
    """The adapter installed on one application: one sink, one event vocabulary, one latch."""

    __slots__ = (
        "clock",
        "config_path",
        "contract_path",
        "instance_id",
        "refused",
        "service",
        "sink",
        "token_env",
    )

    def __init__(
        self, service, config, *, sink=None, clock=None, config_path=None, contract_path=None
    ):
        require(service in SERVICES, "invalid_configuration", 503)
        settings = read_config(config)
        self.service = service
        self.token_env = settings["token_env"]
        self.instance_id = str(uuid.uuid4())
        self.clock = clock or _timestamp
        self.sink = sink if sink is not None else Sink(settings["log_directory"], self.instance_id)
        # Kept so the read-only probes can find the same configuration and contract package this
        # process was started with. They are paths, never values.
        self.config_path = config_path
        self.contract_path = contract_path
        self.refused = 0

    @property
    def durable(self):
        return self.sink.durable

    def log_state(self):
        """The readiness verdict for the log itself, without touching the filesystem."""
        if not self.sink.durable:
            return "non_durable"
        if self.sink.failure == "log_capacity":
            return "log_capacity"
        if self.sink.failure is not None:
            return "log_unavailable"
        return "ok"

    def available(self):
        return self.sink.failure is None

    def emit(self, name, *, level="INFO", outcome="succeeded", error_code=None, duration_ms=None):
        """Record one registered event. Returns whether it was persisted.

        A persistence failure never propagates: the caller's business result has already been
        decided and keeps its own receipt. The failure is latched instead, which is what makes
        readiness false and new business work refuse.
        """
        if name not in EVENTS:
            raise ValueError("unregistered diagnostic event")
        if level not in LEVELS:
            raise ValueError("unregistered level")
        if outcome not in OUTCOMES:
            raise ValueError("unregistered outcome")
        if error_code is not None and error_code not in ERROR_CODES:
            raise ValueError("unregistered error code")
        if name == "log.sink_failed":
            # The one event this adapter cannot persist by definition: it is raised by a sink that
            # just failed. It goes to stderr exactly once instead of recursing into the very file
            # that could not be written.
            self.sink._warn_once()
            return False
        scope = _scope.get()
        with self.sink.lock:
            sequence = self.sink.sequence + 1
            record = {
                "schema_version": SCHEMA_VERSION,
                "timestamp": self.clock(),
                "service": self.service,
                "instance_id": self.instance_id,
                "sequence": sequence,
                "event_id": str(uuid.uuid4()),
                "level": level,
                "event": name,
                "outcome": outcome,
                "correlation_id": scope.correlation_id if scope is not None else None,
                "duration_ms": _duration(duration_ms),
                "error_code": error_code,
            }
            line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            try:
                self.sink.write(line)
            except SinkUnavailable as error:
                self.sink.fail("log_capacity" if isinstance(error, SinkFull) else "log_unavailable")
                return False
            self.sink.sequence = sequence
        return True

    def install(self, app: FastAPI):
        """Attach the request middleware and the published hooks to one application.

        The two probe routes are excluded by exact path inside the middleware, so a probe request
        can never reach the writer even indirectly, and the probes never advance the sequence. The
        middleware also publishes the request's validated correlation identifier on the ASGI scope,
        so an entry point can echo exactly that value without validating or reading the header
        itself, and without importing anything from here.
        """
        app.state.diagnostics = self
        app.state.__dict__[HOOK_NAME] = Hooks(self)
        middleware = self._middleware()

        @app.middleware("http")
        async def diagnostics_middleware(request: Request, call_next):
            return await middleware(request, call_next)

        return app

    def _middleware(self):
        async def dispatch(request: Request, call_next):
            if request.url.path in PROBE_PATHS:
                return await call_next(request)
            return await self._record(request, call_next)

        return dispatch

    async def _record(self, request, call_next):
        correlation = (
            read_correlation(request.headers.get(CORRELATION_HEADER)) or new_correlation_id()
        )
        scope = Scope(self, correlation)
        request.scope[CORRELATION_SCOPE_KEY] = correlation
        token = _scope.set(scope)
        try:
            if not self.available():
                # The log is already unusable: refuse before any business work is admitted, and do
                # not try to write about it — that write is exactly what cannot succeed.
                self.refused += 1
                return _refuse()
            self.emit("request.started", level="INFO", outcome="started")
            try:
                response = await call_next(request)
            except ClientDisconnect:
                self._complete(scope, "unknown", "client_disconnected", level="WARNING")
                raise
            except Fault as error:
                if scope.fault is None:
                    scope.fault = error.code
                self._complete(scope, "failed", map_fault(error.code), level="ERROR")
                raise
            self._complete_response(scope, response)
            return response
        finally:
            _scope.reset(token)

    def _complete_response(self, scope, response):
        status = response.status_code
        if status < 400:
            self._complete(scope, "succeeded", None, level="INFO")
        elif status == 408:
            # The response deadline expired. The work it started is *not* a cancellation, so the
            # request's own record says the outcome is unknown rather than claiming one.
            self._complete(scope, "unknown", "request_timeout", level="WARNING")
        elif status == 429:
            self._complete(scope, "rejected", "overloaded", level="WARNING")
        elif status == 503:
            self._complete(scope, "rejected", "dependency_unavailable", level="ERROR")
        else:
            self._complete(
                scope,
                "rejected",
                map_fault(scope.fault) if scope.fault else "invalid_input",
                level="WARNING",
            )

    def _complete(self, scope, outcome, error_code, *, level):
        if not self.available():
            return
        self.emit(
            "request.completed",
            level=level,
            outcome=outcome,
            error_code=error_code,
            duration_ms=(time.monotonic() - scope.started) * 1000.0,
        )

    def note_authenticated(self, code=None):
        """Record the authentication verdict of the request being served, exactly once."""
        scope = _scope.get()
        if scope is None or scope.authenticated is not None or not self.available():
            return
        scope.authenticated = code
        if code is None:
            self.emit("request.authenticated", level="INFO", outcome="succeeded")
        else:
            if scope.fault is None:
                scope.fault = code
            self.emit(
                "request.authenticated",
                level="WARNING",
                outcome="rejected",
                error_code=map_fault(code),
            )

    def note_fault(self, code):
        """Record the product verdict a service layer reached, for the request's final record."""
        scope = _scope.get()
        if scope is not None and scope.fault is None:
            scope.fault = code


class Execution:
    """One synchronous business call, recorded from its real start to its real end.

    The HTTP response may end before this does — a deadline, a disconnect — and the record here
    still describes what actually happened to the work. That distinction is the whole point: a 408
    is a statement about the response, never a claim that the operation was cancelled.
    """

    def __init__(self, diagnostics, name):
        self.diagnostics = diagnostics
        self.name = name
        self.started = time.monotonic()
        self.finished = False

    def __enter__(self):
        self.diagnostics.emit("sync.execute.started", level="INFO", outcome="started")
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.finish("succeeded", None)
        elif isinstance(value, Fault):
            self.finish("failed", map_fault(value.code))
        else:
            self.finish("failed", "internal_error", level="ERROR")
        return False

    def cancelled(self):
        """The call was abandoned before it ever started; it performed no work at all."""
        self.finish("cancelled", "execute_not_started", level="WARNING")

    def finish(self, outcome, error_code, *, level="ERROR"):
        if self.finished:
            return
        self.finished = True
        if not self.diagnostics.available():
            return
        self.diagnostics.emit(
            "sync.execute.completed",
            level="INFO" if outcome == "succeeded" else level,
            outcome=outcome,
            error_code=error_code,
            duration_ms=(time.monotonic() - self.started) * 1000.0,
        )


class _NullExecution:
    """The no-op stand-in used when no adapter is installed on the serving application."""

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return False

    def cancelled(self):
        return None


def record_execution(name):
    """Instrument one synchronous business call through the installed adapter.

    With no adapter installed — a bare `create_app` in a test — this is a no-op, so the existing
    entry points and their suites are untouched by the arrival of diagnostics.
    """
    scope = _scope.get()
    if scope is None:
        return _NullExecution()
    return Execution(scope.diagnostics, name)


def note_authenticated(code=None):
    scope = _scope.get()
    if scope is not None:
        scope.diagnostics.note_authenticated(code)


def note_fault(code):
    scope = _scope.get()
    if scope is not None:
        scope.diagnostics.note_fault(code)


def _duration(value):
    if value is None:
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(max(0.0, number), 3)


def _timestamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _refuse():
    return JSONResponse(
        {"status": "failed", "code": "log_unavailable"}, status_code=503, headers=dict(NO_STORE)
    )
