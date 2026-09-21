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

import asyncio
import json
import os
import queue
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

# The two environment variables the deployment convention fixes. Both are read here, by the
# adapter that owns them, so a container that sets them really is wired up rather than merely
# configured to look that way. An explicit configuration key always wins over its environment
# variable: the file is the deployment's own statement, and an environment variable inherited from
# a unit must never silently redirect a process that was configured in writing.
LOG_DIRECTORY_ENV = "TIANSHU_LOG_DIR"
DEFAULT_TOKEN_ENV = "TIANSHU_DIAGNOSTICS_TOKEN"
# How much persistence work may be outstanding at once, across both producers. The queue is the
# only buffer between a request and the one writer, so its size is the bound on how many requests
# can be waiting on the log: a burst beyond it is refused rather than parked, exactly as an
# overloaded service refuses. The same number is the admission the writer grants, which is what
# keeps a synchronous caller and an asynchronous one from being two separate budgets.
WRITE_QUEUE_LIMIT = 64
# How long the one writer sleeps when there is nothing queued, which is also how promptly it notices
# a stop request. It is not a caller's wait: no producer ever blocks on the queue, and the longest a
# caller waits for its own line is `WRITER_DEADLINE_SECONDS`.
WRITER_QUEUE_TIMEOUT_SECONDS = 0.25
WRITER_DEADLINE_SECONDS = 2.0
# The longest shutdown waits for already-queued lines. Past it the process stops anyway and every
# line it could not persist is latched as a failure by the writer that owns it.
WRITER_SHUTDOWN_SECONDS = 5.0

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


def _directory_setting(value):
    """One explicitly configured absolute log directory, or None when the key is absent."""
    if value is None or value is False:
        return None
    require(isinstance(value, str) and bool(value.strip()), "invalid_configuration", 503)
    resolved = Path(value).expanduser()
    require(resolved.is_absolute(), "invalid_configuration", 503)
    return resolved


def read_config(raw):
    """The two diagnostics settings, validated strictly; every other key is left to its owner.

    `log_directory` is the explicit local log directory (deployment mounts it at
    `/var/log/tianshu`). `diagnostics.token_env` names the environment variable holding the
    independent readiness token; the token itself is never stored or logged here.

    Both deployment variables are read *here*, so setting them really assembles a durable sink
    rather than only appearing to. The precedence is fixed and one-directional:

    - `log_directory` in the configuration wins; with no key, `TIANSHU_LOG_DIR` is used; with
      neither, the sink is explicitly non-durable. The variable is only ever a directory: an
      absolute path, read exactly once while the process is being assembled.
    - `diagnostics.token_env` in the configuration wins; with no key, the contract's own
      `TIANSHU_DIAGNOSTICS_TOKEN` is consumed. The variable names which environment variable holds
      the token — it never *is* the token, so a business credential cannot be picked up by
      accident here, and the value is only ever read when a readiness request presents one.

    A non-durable adapter is only ever chosen explicitly: either the key is absent and no variable
    is set, or the key is the literal false. Any other value fails closed rather than silently
    falling back to a stdio stream.
    """
    require(isinstance(raw, dict), "invalid_configuration", 503)
    directory = _directory_setting(raw.get("log_directory"))
    if directory is None and raw.get("log_directory") is not False:
        directory = _directory_setting(os.environ.get(LOG_DIRECTORY_ENV))
    section = raw.get("diagnostics", {})
    require(isinstance(section, dict), "invalid_configuration", 503)
    token_env = section.get("token_env")
    if token_env is not None:
        require(
            isinstance(token_env, str) and bool(token_env.strip()), "invalid_configuration", 503
        )
    else:
        token_env = DEFAULT_TOKEN_ENV
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
        self._last_reserved = 0
        self.failure = None
        self.warned = False
        self._handle = None
        self._segment_size = 0

    @property
    def durable(self):
        return self.directory is not None

    def reserve(self):
        """Claim the next sequence number for the one thread that is about to write it.

        The read and the increment happen together under the sink's lock, which is what makes the
        claim exclusive: without that, two writers would read the same value and two lines would
        carry the same number. The lock is released before the write, so the `fsync` happens with
        nothing held — that separation is the whole reason the writer thread exists.

        Only the single writer thread calls this. Callers read `sequence`, which is advanced only
        after a line is really on disk, so no reader ever sees a number that was not written.
        """
        with self.lock:
            self._last_reserved = self.sequence + 1
            return self._last_reserved

    def last_reserved(self):
        """The highest number handed out by `reserve`, whether or not it is on disk yet."""
        return self._last_reserved

    def last_persisted_sequence(self):
        """The highest sequence already on disk for this instance, or 0.

        Read once while the process is being assembled and before anything is written, so a restart
        that reuses an instance id continues the sequence instead of repeating numbers inside the
        same file. A segment that cannot be read is treated as if it were absent: the number this
        returns is added to, never trusted as the only guard against a repeat.
        """
        if self.directory is None:
            return 0
        highest = 0
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return 0
        for entry in entries:
            name = entry.name
            if not name.startswith(self.instance_id + ".") or not name.endswith(".jsonl"):
                continue
            try:
                with open(entry.path, encoding="utf-8") as stream:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            number = json.loads(line).get("sequence")
                        except (ValueError, AttributeError):
                            continue
                        if isinstance(number, int) and number > highest:
                            highest = number
            except OSError:
                continue
        return highest

    def _warn_once(self):
        if not self.warned:
            self.warned = True
            try:
                sys.stderr.write(SINK_WARNING)
                sys.stderr.flush()
            except OSError:  # pragma: no cover - a closed stderr cannot be reported either
                pass

    def fail(self, code):
        """Latch a persistence failure and emit the one fixed warning. Never recovers by itself.

        The open handle is deliberately left alone: it is owned by the single writer thread, which
        is the only caller of `write` and therefore the only place that may close it. Latching is
        safe from any thread.
        """
        if self.failure is None:
            self.failure = code
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

    def write(self, line, sequence):
        """Persist one already-validated line, and publish the number it was written under.

        With no configured log directory there is no second, quieter sink to fall back to: a
        standard stream is not a durable record and must never be mistaken for one, so the line is
        dropped here and the readiness check reports the sink `non_durable`. That mode exists for a
        development process only — a deployment that claims to be ready on it cannot exist — and
        dropping is also the only behavior that cannot block a serving process on a full pipe it
        does not own.

        Only the single writer thread calls this, with a number it claimed from `reserve`. The
        shared counter is moved at the very end: a reader must never see a number whose bytes are
        not on disk yet, so its value is always a sequence the file really contains.
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
        self.sequence = sequence

    def close(self):
        """Release the open segment. Only the thread that owns the handle may call this."""
        self._close()


class _Pending:
    """One record handed to the writer, and the verdict the caller is waiting for.

    A record that has been admitted is *taken*: from that moment exactly one of three things must
    settle it — the writer persists it, the writer fails it, or the writer abandons it on the way
    out — and settling it is also what returns its admission slot. Nothing on the caller's side can
    take that slot back early, because the caller stopping its wait does not stop the work: the
    record is still in the queue, or still in the writer's hands, and the capacity it occupies is
    still spent. `taken` is set only by the code that really spends the slot, never by a caller.
    """

    __slots__ = ("done", "failed", "persisted", "record", "taken")

    def __init__(self, record):
        self.record = record
        self.done = threading.Event()
        self.persisted = False
        self.failed = False
        self.taken = False


class Writer:
    """The one thread allowed to touch the log file, and the bounded queue in front of it.

    `flush` and `fsync` are blocking calls on a real disk, and running them on the event loop would
    make the log the slowest thing in the process: a 250 ms `fsync` would stall every other
    request, including the liveness probe that exists to answer while everything else is busy. So
    the blocking work belongs to a thread this process owns, and the event loop never waits on it
    at all: it hands a line over through a bounded admission and is told the verdict by a callback
    on that same loop.

    What is bounded, exactly:

    - **The queue.** `WRITE_QUEUE_LIMIT` lines may be outstanding. A submission beyond it is
      refused immediately and latches the sink, because parking an unbounded number of requests on
      the log is the failure mode this bound exists to prevent — and it is never a silent drop.
    - **The threads.** `WRITE_QUEUE_LIMIT` blocking waits may exist at once. Moving a wait to a
      worker is not by itself a bound: the default executor's queue is unbounded, so a flood that
      outran the writer would accumulate there instead of in the writer's own queue. The same
      ceiling covers both, and it is held until the *writer* is finished with the record rather
      than until the caller stops waiting — see `_Pending`.
    - **Every wait.** A submission is either accepted now or refused now (`admit` never blocks, not
      even on the loop); a caller waits at most `WRITER_DEADLINE_SECONDS` for its own line;
      shutdown waits `WRITER_SHUTDOWN_SECONDS` for what is already queued. Each of those is a
      deadline on a refusal, never an assertion that the line was written.
    - **The writers.** Exactly one thread owns the file — including the closing of it — so there is
      never an old writer and a new one racing over the same segment, and a line that timed out is
      *not* cancelled: the writer this process owns either persists it or latches it. Nothing is
      submitted twice, and a stopped writer is never rebuilt.

    A deadline that expires therefore means "not established", and the caller refuses the business
    work it was about to admit. It never means "probably fine": a line whose persistence could not
    be confirmed is latched, not assumed.

    The writer's own lifecycle is one state machine, and it is the reason no caller can ever see half
    of it:

    - `NEW` — no writer thread has been asked to run. It owns nothing, so a shutdown here has
      nothing to join and may release the sink itself.
    - `STARTING` — `Thread.start()` is on the stack. The thread object exists and is published for
      waiters, but it has not been started yet. A shutdown that arrives now waits for this window to
      close instead of calling `join` on an unstarted thread, which is the `RuntimeError` the state
      exists to prevent; the window is bounded by asking `Thread.start` to be quick.
    - `RUNNING` — the thread has been started. A shutdown sets `STOPPING` and joins it.
    - `STOPPING` — the stop was requested; the writer is persisting what is already queued, and
      nothing new is accepted.
    - `STOPPED` — the writer thread has been started and has ended, so the file has been released by
      its owner and no second writer is ever built; or the thread was never started at all, in which
      case this class still owns the file and releases it.

    The transitions are made under `self.lock`, which is a plain mutex and is never held across any
    system IO.
    """

    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.sink = diagnostics.sink
        self.queue = queue.Queue(maxsize=WRITE_QUEUE_LIMIT)
        self.attempted = 0
        self.stopping = threading.Event()
        # The one mutex for this class: it guards the state transitions, the queue insertion that
        # consumes an admission slot, and the drain-and-settle of the abandoned records. It is never
        # held across a write, a flush, or a join.
        self.lock = threading.Lock()
        self.state = self.NEW
        # How many records have been admitted and not yet settled by the writer. It is the true
        # measure of outstanding log work, so both producers share it: a synchronous caller and an
        # asynchronous caller each consume one slot when their record is really queued, and the
        # slot comes back when the writer is done with that record, never when a caller gives up.
        self.admitted = 0
        self.thread = None
        self.settled = 0

    # -- admission ---------------------------------------------------------------------------

    def _take(self, pending):
        """Consume one admission slot for a record that is about to be queued."""
        pending.taken = True
        self.admitted += 1

    def _settle(self, pending):
        """Return the slot of one record the writer is finished with, exactly once.

        Called only by the writer thread, and only for a record that really was admitted. A record
        the writer never took back — one that was refused, or one whose producer was cancelled
        before insertion — is settled with `taken` False and changes nothing, which is what keeps
        this idempotent.
        """
        if not pending.taken:
            return
        pending.taken = False
        self.admitted -= 1
        self.settled += 1

    def slots(self):
        """How many admission slots are free right now: the writer's own outstanding-work figure."""
        with self.lock:
            return WRITE_QUEUE_LIMIT - self.admitted

    def _acquire(self, pending, *, blocking):
        """Whether one free slot could be taken for `pending`, and what the caller should do next.

        Three verdicts, and the third is the one that matters for cancellation: when the record was
        not taken, `_settle` must be called on it precisely once by whoever learns that it was not
        taken. The caller that is already here does that, because it is the only one that will.

        The stop is consulted *here*, under the same lock the shutdown takes, and not only by the
        producer before it built the record: a stop that arrives while a producer is on its way in
        must refuse that record rather than accept one that only the shutdown drain could ever
        write. The queue insertion below is what makes the decision final, because the shutdown sets
        the stop under this same lock and drains whatever is already in the queue.
        """
        with self.lock:
            if self.stopping.is_set() or self.state == self.STOPPED:
                return None
            if self.admitted >= WRITE_QUEUE_LIMIT:
                self.sink.fail("log_capacity")
                return None
            if not blocking and not self._admitted_room_for_a_non_blocking_put():
                self.sink.fail("log_capacity")
                return None
            self._take(pending)
            try:
                self.queue.put_nowait(pending)
            except queue.Full:
                self.sink.fail("log_capacity")
                return None
            self._start()
            return pending

    def _admitted_room_for_a_non_blocking_put(self):
        """Whether the physical queue also has room, so `put_nowait` cannot be the refusal.

        The caller's own slot count already covers this, and the queue is sized by the same number,
        so this is a guard rather than a second policy: it keeps the non-blocking producer honest
        about the queue's real ceiling even if a record is ever abandoned without being dequeued.
        """
        return not self.queue.full()

    def admit(self, pending):
        """Queue one record without ever blocking, or refuse it and latch the sink.

        `None` means the bounded work set is full, the process is stopping, or the sink was already
        refuted — three forms of the same answer: this process will not take on that line, so the
        business work it describes must not be admitted either. It is never a silent drop and never
        a wait: a producer on the event loop may not park, and a producer that must wait is exactly
        the case this ceiling exists to refuse.
        """
        pending = self._acquire(pending, blocking=True)
        if pending is None:
            return None
        return pending

    def try_admit(self, pending):
        """`admit` for a caller that must give up its claim before it could ever block.

        The only difference from `admit` is that a free admission slot is not enough: the physical
        queue must have room too. Both producers share the same slot count, so the two cannot
        disagree about how much outstanding work this process is paying for.
        """
        return self._acquire(pending, blocking=False)

    # -- lifecycle ---------------------------------------------------------------------------

    def _start(self):
        """Start the single writer thread, at most once in this process, whatever calls in when.

        Called with `self.lock` held. The whole decision — has a writer ever been started, is this
        sink durable, construct it, publish it, start it — is one atomic step, so two threads
        emitting the first event at the same moment produce one writer and one thread rather than
        two writers over one file handle. The thread object is published before `start()` returns,
        but the state itself is `STARTING` until it has really been started, so a concurrent
        shutdown cannot mistake a published object for a running one.
        """
        if self.state != self.NEW or not self.sink.durable:
            return
        thread = threading.Thread(target=self._run, name="tianshu-memory-log-writer", daemon=True)
        self.thread = thread
        self.state = self.STARTING
        try:
            thread.start()
        except BaseException:
            # The thread never ran, so it owns nothing and there is nothing to join. The sink stays
            # with this class, which is also what a shutdown in this state expects to find.
            self.state = self.STOPPED
            raise
        self.state = self.RUNNING

    def start(self):
        """Start the single writer, at most once in this process, whatever calls in when."""
        with self.lock:
            self._start()

    def shutdown(self):
        """Stop accepting, drain what is queued, and wait a bounded time for the writer to end.

        Returns True only when the one writer thread has really finished — which is also the moment
        the file is released, because that thread owns the handle. A False means the writer did not
        end inside the deadline: it may still be inside a `write` or an `fsync` the kernel has not
        returned from. That is reported as unconfirmed rather than papered over: the caller must not
        treat it as a closed file, nothing here closes the handle out from under a live writer, and
        no second writer is ever built to replace it.

        A writer whose thread has not been started yet is never joined: the state is consulted under
        the same lock the start uses, so this can only ever see `NEW` (nothing to join), `RUNNING`
        (join it), or `STOPPED` (nothing left to join). The `STARTING` window is waited out, and that
        wait is bounded, so a shutdown cannot be parked forever by a start that never returns.
        """
        deadline = time.monotonic() + WRITER_SHUTDOWN_SECONDS
        with self.lock:
            self.stopping.set()
            while self.state == self.STARTING and time.monotonic() < deadline:
                # `Thread.start` is on someone else's stack. Releasing the lock is what lets it
                # finish; taking it again is what makes the state below a decision rather than a
                # guess.
                self.lock.release()
                try:
                    time.sleep(0.001)
                finally:
                    self.lock.acquire()
            if self.state == self.NEW:
                # No writer was ever started, so nothing else can be holding the file: the process
                # that assembled this adapter is the only thing that could have, and it did not.
                # Any record that is somehow queued here is settled rather than left taken.
                self._abandon_queued()
                self.state = self.STOPPED
                self.sink.close()
                return True
            if self.state == self.STARTING:
                # The start did not complete inside the deadline. This is the same "unconfirmed"
                # verdict as a join that ran out of patience, and it is deliberately not a join:
                # joining an unstarted thread is the error this whole state machine removes.
                return False
            if self.state == self.STOPPED:
                return not (self.thread is not None and self.thread.is_alive())
            thread = self.thread
        thread.join(max(0.0, deadline - time.monotonic()))
        ended = not thread.is_alive()
        if ended:
            with self.lock:
                self.state = self.STOPPED
        return ended

    def _abandon_queued(self):
        """Settle every queued record for a writer that never ran. Called with the lock held."""
        while True:
            try:
                pending = self.queue.get_nowait()
            except queue.Empty:
                return
            try:
                pending.failed = True
            finally:
                pending.done.set()
                self._settle(pending)

    # -- the writer thread -------------------------------------------------------------------

    def wait(self, pending):
        """Whether this exact line reached durable storage. A timeout is a refusal, not a maybe."""
        if not pending.done.wait(WRITER_DEADLINE_SECONDS):
            # The writer is still working on it — or stuck in the kernel. Either way this process
            # cannot claim the line was persisted, so it refuses; the writer still owns the item and
            # decides that item's verdict itself, and it is never cancelled or re-sent.
            #
            # A write that did not confirm inside the deadline also latches the sink. Latency this
            # far past the deadline is a log whose durability is unknown, and refusing only this one
            # request would keep admitting the next, so the process stops admitting business work
            # until an operator looks at it. The record may still land — an unconfirmed write is not
            # a lost one — but readiness stays false, because "the bytes are probably there" is not
            # a durability claim this process is allowed to make.
            self.sink.fail("log_unavailable")
            pending.failed = True
            return False
        return pending.persisted and not pending.failed

    async def await_verdict(self, pending):
        """Wait for one already-admitted record without ever holding the loop.

        The blocking `wait` runs on a worker, and the loop is handed back a future rather than being
        held inside the writer: no coroutine, no task and no probe response is ever parked waiting
        for an `fsync`. Cancelling this await cancels only the caller's interest — the record is
        already admitted, the writer still owns it, and its admission slot is returned by the writer
        when it is really done. That is what stops a cancelled waiter from manufacturing capacity.
        """
        return await asyncio.get_running_loop().run_in_executor(None, self.wait, pending)

    def _run(self):
        """Own the file until told to stop: persist, or latch, every record in arrival order.

        The sequence number is allocated *here*, on the one thread that writes, so the order of the
        numbers is the order of the bytes by construction. Allocating it in the calling thread would
        need the allocation and the write to happen under one lock, and holding that lock across an
        `fsync` is exactly the design this class exists to remove.
        """
        try:
            while True:
                try:
                    pending = self.queue.get(timeout=WRITER_QUEUE_TIMEOUT_SECONDS)
                except queue.Empty:
                    if self.stopping.is_set():
                        return
                    continue
                self.attempted += 1
                try:
                    record = pending.record
                    sequence = self.sink.reserve()
                    record["sequence"] = sequence
                    line = json.dumps(
                        record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    )
                    if len(line.encode("utf-8")) + 1 > MAX_LINE_BYTES:
                        # A record that cannot fit the contract's byte limit is refused rather than
                        # truncated into something that parses as a different event.
                        self.sink.fail("log_unavailable")
                    else:
                        self.sink.write(line, sequence)
                except SinkUnavailable as error:
                    self.sink.fail(
                        "log_capacity" if isinstance(error, SinkFull) else "log_unavailable"
                    )
                except Exception:
                    # Anything else raised while making this record durable leaves it unwritten, so
                    # it is the same refusal as a full disk. Latching here keeps the rule true even
                    # for a failure this code did not anticipate: an unaccountable side effect is
                    # never admitted, and the caller's `wait` sees an unlatched-but-unwritten
                    # record as a refusal either way.
                    self.sink.fail("log_unavailable")
                    pending.failed = True
                else:
                    if self.sink.failure is None and not pending.failed:
                        pending.persisted = True
                        # The number becomes visible to the rest of the process only now, after the
                        # bytes really are on disk.
                        self.sink.sequence = sequence
                finally:
                    pending.done.set()
                    # The record is finished with — persisted, or latched as failed — so this is
                    # exactly when its admission slot comes back. Not when the caller stopped
                    # waiting: a cancelled caller leaves the record in the writer's hands, and the
                    # capacity it occupies has to stay spent until the writer really is done.
                    self._settle(pending)
        finally:
            # Every line still queued when this thread ends is latched rather than retried: the
            # process is stopping, and pretending these were written is exactly what the latch is
            # for. Nothing here is re-submitted or re-executed, and each of these records returns
            # its admission slot here too, because the writer is what really gives it back.
            while True:
                try:
                    pending = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    pending.failed = True
                finally:
                    pending.done.set()
                    self._settle(pending)
            # The file is released *here*, by the one thread that ever owned it, and only after
            # this thread has stopped writing to it. Closing the handle somewhere else would mean a
            # caller taking a file the writer may still be flushing — a second owner of a descriptor
            # this class promises has exactly one — and it would put an unbounded `close` on whoever
            # happened to call shutdown. A shutdown that ran out of patience therefore leaves the
            # handle exactly where it belongs: it is released when this thread really exits.
            self.sink.close()


class Diagnostics:
    """The adapter installed on one application: one sink, one event vocabulary, one latch."""

    __slots__ = (
        "clock",
        "config_path",
        "contract_path",
        "instance_id",
        "log_configured",
        "refused",
        "service",
        "sink",
        "stopping",
        "token_env",
        "writer",
    )

    def __init__(
        self, service, config, *, sink=None, clock=None, config_path=None, contract_path=None
    ):
        require(service in SERVICES, "invalid_configuration", 503)
        settings = read_config(config)
        self.service = service
        self.token_env = settings["token_env"]
        # Whether this process was configured with a durable log at all, as distinct from whether
        # that log is currently working. The two are different answers to different questions.
        self.log_configured = settings["log_directory"] is not None
        self.instance_id = str(uuid.uuid4())
        self.clock = clock or _timestamp
        self.sink = sink if sink is not None else Sink(settings["log_directory"], self.instance_id)
        self.stopping = threading.Event()
        self.writer = Writer(self)
        if self.sink.durable and self.sink.sequence == 0:
            # A process that restarts with the same instance id must not repeat numbers inside the
            # file it is appending to. This is read once, before anything is written, and only ever
            # raises the starting point.
            self.sink.sequence = self.sink.last_persisted_sequence()
        # Kept so the read-only probes can find the same configuration and contract package this
        # process was started with. They are paths, never values.
        self.config_path = config_path
        self.contract_path = contract_path
        self.refused = 0

    @property
    def durable(self):
        return self.sink.durable

    def start(self):
        """Start the single log writer, if it is not already running.

        The writer starts itself on the process's first event, so this exists for the deployment
        path to state the intent explicitly rather than for correctness.
        """
        self.writer.start()

    def shutdown(self):
        """Drain the already-queued lines within a deadline and release the file.

        After this the process is stopping: the writer has stopped accepting, so every later event
        is refused rather than queued for a thread that will never read it. A refused event is a
        refusal of the work it would have described, which is the same rule as everywhere else.

        Returns True only when the writer really ended — and with it released the file — inside the
        shutdown deadline. False means that could not be confirmed in time; see `Writer.shutdown`.
        """
        self.stopping.set()
        return self.writer.shutdown()

    async def ashutdown(self):
        """`shutdown` for a serving loop: the join happens on a worker, and it is still bounded.

        The wait is bounded twice over — `WRITER_SHUTDOWN_SECONDS` on the join itself and the same
        figure on the executor call — so a writer stuck in the kernel delays the shutdown of this
        process by a known amount rather than indefinitely. The verdict is the writer's: True means
        it really ended and released the file, False means that is unconfirmed.
        """
        self.stopping.set()
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self.writer.shutdown), timeout=WRITER_SHUTDOWN_SECONDS
            )
        except (TimeoutError, asyncio.TimeoutError):
            return False

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

    def _build(self, name, *, level, outcome, error_code, duration_ms):
        """Build one contract-shaped record, with every field except its number.

        The record is handed to the writer unnumbered on purpose: the number is allocated on the
        thread that writes, so the file's order and the numbers' order cannot disagree however many
        threads call this at once.

        Nothing is started here. The writer is started by the admission of the record that needs it,
        after that record is really in the queue, so a shutdown can never run between "the writer
        exists" and "the writer has something to write" — in that order the stop would always win
        and a record the caller had already been promised would be lost.
        """
        scope = _scope.get()
        return _Pending(
            {
                "schema_version": SCHEMA_VERSION,
                "timestamp": self.clock(),
                "service": self.service,
                "instance_id": self.instance_id,
                "sequence": None,
                "event_id": str(uuid.uuid4()),
                "level": level,
                "event": name,
                "outcome": outcome,
                "correlation_id": scope.correlation_id if scope is not None else None,
                "duration_ms": _duration(duration_ms),
                "error_code": error_code,
            }
        )

    def emit(self, name, *, level="INFO", outcome="succeeded", error_code=None, duration_ms=None):
        """Queue one registered event, and report whether it reached the log.

        In an explicitly non-durable process there is nothing to persist, so this reports True and
        readiness reports `non_durable` — that is where the mode is refused, not here. Every other
        True means the line is on durable storage before this returns.

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
        if not self.sink.durable:
            # Nothing is written and nothing can fail. Readiness reports `non_durable`, which is
            # where an undeployable mode is refused rather than here.
            return True
        if self.stopping.is_set():
            # This process is stopping: the writer has stopped accepting, so a record handed over
            # now would sit in a queue nothing will read. It is refused instead of being accepted
            # and quietly forgotten.
            return False
        pending = self.writer.admit(
            self._build(
                name, level=level, outcome=outcome, error_code=error_code, duration_ms=duration_ms
            )
        )
        if pending is None:
            return False
        return self.writer.wait(pending)

    async def aemit(
        self, name, *, level="INFO", outcome="succeeded", error_code=None, duration_ms=None
    ):
        """`emit` for the event loop: the waiting happens on a bounded worker, never on the loop.

        A blocking `fsync` is allowed to take as long as the disk takes; what it is not allowed to
        do is stop this loop from answering a liveness probe while it does. Handing the wait to
        `asyncio.to_thread` alone would move that problem rather than solve it, because the default
        executor's queue is unbounded: a burst that outran the writer would pile up there instead,
        and the writer's own `WRITE_QUEUE_LIMIT` would describe nothing.

        So the admission is the writer's own bounded work set, and it is taken *only* by a record
        that really reaches the shared queue (`Writer.try_admit`). There is no separate async gate
        in front of it: a second gate would be a second opinion about capacity, and it would be one
        the writer cannot see, which is how a cancelled waiter could hand back a slot whose work had
        not finished. Here the slot belongs to the record, so cancelling this coroutine cannot free
        anything, and the record the writer is still holding is still counted.

        Nothing in this method ever waits on the queue, so a full log can never park the loop: a
        record that cannot get in is refused now and latches the sink.

        Every refusal here is a `False`, never an exception: the caller decides whether the work it
        was about to admit may happen, which is the same contract `emit` has.
        """
        if name not in EVENTS:
            raise ValueError("unregistered diagnostic event")
        if level not in LEVELS:
            raise ValueError("unregistered level")
        if not self.sink.durable:
            # Nothing to write and nothing that can block: this is an explicitly non-durable
            # process, and `non_durable` is refused by readiness rather than here.
            return True
        if not self.available():
            self.sink._warn_once()
            return False
        if self.stopping.is_set():
            return False
        record = self._build(
            name, level=level, outcome=outcome, error_code=error_code, duration_ms=duration_ms
        )
        return await self._deliver(record)

    async def _deliver(self, pending):
        """Admit one built record into the writer's bounded work set, then wait off the loop.

        Cancellation reaches exactly one place: the `await` below. The record stays admitted and the
        writer stays responsible for it, so this coroutine disappearing changes nothing about the
        work, the capacity it occupies, or the bytes it produces. Its verdict is simply no longer
        collected by anyone — which is the honest outcome, because the caller is gone.
        """
        admitted = self.writer.try_admit(pending)
        if admitted is None:
            return False
        return await self.writer.await_verdict(admitted)

    def _refuse_unaccountable(self):
        """Whether business work must be refused because its record could not be made durable.

        Two cases refuse, and they are the same case seen twice: this process **was configured** to
        keep a durable log and the record for this work cannot be persisted — because the sink is
        already latched, because the bounded buffer is full, or because the write did not confirm
        within its deadline. In every one of them a side effect would exist that this process cannot
        account for, so the work it would have described must not happen.

        A process with no log directory was never configured with a durable log at all. That mode is
        an explicit development choice, it is reported as `non_durable` and a deployment can never
        be ready on it, and the product's own existing suites run in exactly that mode. Refusing
        every request there would be a new rule about the domain rather than a rule about the log,
        and this task adds no rule of its own to the domain.
        """
        if not self.log_configured:
            return False
        return not self.available()

    async def accept(self, name):
        """The durable acceptance record, confirmed before any business work is admitted.

        Returns True only when this exact line reached durable storage. Every other answer — a full
        buffer, an unusable disk, a writer that did not confirm within the deadline, an explicitly
        non-durable process with nothing to confirm — is a refusal, and the caller must not run the
        business work it was about to run. That is the whole point: a side effect this process
        cannot account for must not happen.
        """
        if self._refuse_unaccountable():
            return False
        return await self.aemit(name, level="INFO", outcome="started")

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
            if not await self.accept("request.started"):
                # The acceptance record could not be confirmed as durable, so the work it would
                # have described must not happen at all. This is the one place the decision is
                # still free: nothing has run yet, so refusing here leaves no side effect to
                # reconcile, and there is nothing to retry, re-execute or roll back.
                self.refused += 1
                return _refuse()
            try:
                response = await call_next(request)
            except ClientDisconnect:
                await self._complete(scope, "unknown", "client_disconnected", level="WARNING")
                raise
            except Fault as error:
                if scope.fault is None:
                    scope.fault = error.code
                await self._complete(scope, "failed", map_fault(error.code), level="ERROR")
                raise
            await self._complete_response(scope, response)
            return response
        finally:
            _scope.reset(token)

    async def _complete_response(self, scope, response):
        status = response.status_code
        if status < 400:
            await self._complete(scope, "succeeded", None, level="INFO")
        elif status == 408:
            # The response deadline expired. The work it started is *not* a cancellation, so the
            # request's own record says the outcome is unknown rather than claiming one.
            await self._complete(scope, "unknown", "request_timeout", level="WARNING")
        elif status == 429:
            await self._complete(scope, "rejected", "overloaded", level="WARNING")
        elif status == 503:
            await self._complete(scope, "rejected", "dependency_unavailable", level="ERROR")
        else:
            await self._complete(
                scope,
                "rejected",
                map_fault(scope.fault) if scope.fault else "invalid_input",
                level="WARNING",
            )

    async def _complete(self, scope, outcome, error_code, *, level):
        """The request's own closing record, written off the event loop like every other line.

        This one lands after the response exists, which is exactly the case where a process must
        not stop answering: the work is already done and its receipt already decided, so the
        record of it is worth waiting for but never worth blocking the loop over.
        """
        if not self.available():
            return
        await self.aemit(
            "request.completed",
            level=level,
            outcome=outcome,
            error_code=error_code,
            duration_ms=(time.monotonic() - scope.started) * 1000.0,
        )

    def note_authenticated(self, code=None):
        """Record the request's authentication verdict from a thread that may synchronously wait.

        This is the form for a business thread — one already inside a worker, where blocking is what
        the thread is for. An asynchronous entry point must use `anote_authenticated` instead: the
        two write the same record, and only the second one keeps the event loop free while the disk
        decides.
        """
        scope = _scope.get()
        if scope is None or scope.authenticated is not None or not self.available():
            return True
        scope.authenticated = code
        if code is None:
            return self.emit("request.authenticated", level="INFO", outcome="succeeded")
        if scope.fault is None:
            scope.fault = code
        return self.emit(
            "request.authenticated",
            level="WARNING",
            outcome="rejected",
            error_code=map_fault(code),
        )

    async def anote_authenticated(self, code=None):
        """Record the authentication verdict without parking the event loop on the disk.

        The verdict is written here, straight after the same two checks that always decided it, so
        nothing about who may call what has changed. What has changed is where the wait happens: on
        a bounded worker, so a 250 ms disk stalls neither this request nor the liveness probe
        running beside it.

        A record that cannot be confirmed is reported to the caller, which refuses the request. The
        check did happen — this is a statement about the log, not about the credential — and a
        process that cannot record which credential it accepted must not go on to act on it.
        """
        scope = _scope.get()
        if scope is None or scope.authenticated is not None or not self.available():
            return True
        scope.authenticated = code
        if code is None:
            return await self.aemit("request.authenticated", level="INFO", outcome="succeeded")
        if scope.fault is None:
            scope.fault = code
        return await self.aemit(
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

    Entering this context is the last moment at which the call can still be refused for free, so it
    is where the durable start record is confirmed. A start record that cannot be persisted means
    the call is refused with the same 503 the middleware uses, and the body never runs: a side
    effect this process cannot account for is not allowed to happen. Already-committed work is
    untouched — nothing here is re-executed, and no completed result is rolled back.
    """

    def __init__(self, diagnostics, name):
        self.diagnostics = diagnostics
        self.name = name
        self.started = time.monotonic()
        self.finished = False

    def __enter__(self):
        if not self.diagnostics.emit("sync.execute.started", level="INFO", outcome="started"):
            raise Fault("log_unavailable", 503)
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


async def anote_authenticated(code=None):
    """Record the authentication verdict from an asynchronous entry point.

    The event loop's form of `note_authenticated`: the same verdict, the same record, written
    without stopping the loop. Returns whether that record reached durable storage.
    """
    scope = _scope.get()
    if scope is None:
        return True
    return await scope.diagnostics.anote_authenticated(code)


def note_authenticated(code=None):
    """Record the authentication verdict from a synchronous business thread."""
    scope = _scope.get()
    if scope is not None:
        return scope.diagnostics.note_authenticated(code)
    return True


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
