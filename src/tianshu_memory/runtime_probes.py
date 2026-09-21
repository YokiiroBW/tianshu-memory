"""Read-only liveness and readiness verdicts for the two Memory HTTP services.

Every check here observes; none of them changes anything. That is a property of the code, not a
promise in a document, and it is what the tests assert byte for byte:

- the database is opened with SQLite's own `mode=ro` URI and only `SELECT` is issued. `Store` is
  never constructed, no `Store.transaction` is started (so no `BEGIN IMMEDIATE`, no WAL switch and
  no checkpoint persist), no migration runs, no directory and no database file is created, and an
  absent database stays absent;
- the source checkpoint is read through the existing read-only `source_recovery.checkpoint`
  capability and compared with the database's own metadata. The guard rules are not restated here
  and the guard file is never written. A mismatch is reported as not-ready and never "repaired":
  an absent or older checkpoint needs a reviewer, not a probe;
- the log latch is read from memory. The probe never writes an event, never advances the sequence
  and never creates or touches the log directory. Recording what a probe observed is the job of
  the external collector.

Readiness says only that this process's *local* prerequisites verified. A remote Platform, Chat
Audit or model service is never called, so every remote dependency is reported `not_verified`:
"this process did not test it", rather than a success that one local `SELECT 1` could never prove.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import time
from pathlib import Path

from . import source_recovery
from .diagnostics import CHAT_SERVICE, CHECK_STATES, CHECKS_BY_SERVICE, KNOWLEDGE_SERVICE
from .domain import canonical

# The frozen diagnostics package as published by the coordinator. This product consumes it and
# keeps it at deployment time; it never copies the schema source into itself.
CONTRACT_MANIFEST = "manifest.json"
CONTRACT_VERSION = "1.0.0"
# One bounded retry for a database that is briefly busy, and a hard ceiling on the whole probe.
RETRY_SECONDS = 0.05
CHECK_DEADLINE_SECONDS = 2.0
RETRYABLE_SQLITE = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_PROTOCOL,
        sqlite3.SQLITE_INTERRUPT,
    }
)


class ProbeConfig:
    """What a probe needs, assembled once by `server_runtime` at startup."""

    __slots__ = (
        "client",
        "config_path",
        "contract_path",
        "diagnostics",
        "handles",
        "runtime",
        "service",
    )

    def __init__(
        self,
        *,
        service,
        diagnostics,
        config_path,
        contract_path,
        runtime=None,
        client=None,
        handles=None,
    ):
        self.service = service
        self.diagnostics = diagnostics
        self.config_path = Path(config_path).resolve()
        # None means this process could not establish where the published package is kept, which the
        # contract check reports as `not_configured` rather than as a verified package.
        self.contract_path = None if contract_path is None else Path(contract_path)
        self.runtime = runtime
        self.client = client
        # A callable the entry point supplies: whether it still holds the objects that own its
        # database handle. The probe calls it and never sets anything through it.
        self.handles = handles


class Outcome:
    """One check's verdict, plus whether asking again could plausibly change it."""

    __slots__ = ("retryable", "state")

    def __init__(self, state, *, retryable=False):
        if state not in CHECK_STATES:
            raise ValueError("unregistered check state")
        self.state = state
        self.retryable = retryable


OK = Outcome("ok")
NOT_CONFIGURED = Outcome("not_configured")
NOT_VERIFIED = Outcome("not_verified")
NON_DURABLE = Outcome("non_durable")


def failed(*, retryable=False):
    return Outcome("failed", retryable=retryable)


def ip_literal(value):
    """The normalized text of an IP literal, or None when the value is not one."""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def probe_configuration(settings):
    """Whether the process can name its own local prerequisites at all.

    The private configuration file must exist and parse, and the two keys a probe needs must be
    the right kind. No value is returned, reported or logged — only this verdict.
    """
    raw = _read(settings.config_path)
    if raw is None:
        return NOT_CONFIGURED if not settings.config_path.is_file() else failed()
    if not isinstance(raw.get("database_path"), str) or not raw["database_path"].strip():
        return failed()
    if settings.service == KNOWLEDGE_SERVICE and not isinstance(raw.get("knowledge"), dict):
        return failed()
    return OK


def probe_contract(directory):
    """Verify the published diagnostics package exactly as the deployment kept it.

    Every file the manifest lists must be present and must hash to the digest the manifest
    declares, and the declared version must be the one this adapter writes. A package that drifted
    — a truncated copy, an edited schema, a leftover from another version — is not-ready rather
    than silently tolerated.
    """
    try:
        if directory is None:
            return NOT_CONFIGURED
        directory = Path(directory)
        manifest = json.loads((directory / CONTRACT_MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != CONTRACT_VERSION:
            return failed()
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return failed()
        for name, declared in files.items():
            if not isinstance(name, str) or not isinstance(declared, str):
                return failed()
            actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            if not hmac.compare_digest(actual, declared):
                return failed()
    except FileNotFoundError:
        return NOT_CONFIGURED
    except (OSError, ValueError):
        return failed()
    return OK


def _connect(database):
    """A read-only connection that cannot create anything, not even an empty database."""
    uri = "file:" + Path(database).as_uri()[len("file:") :] + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=CHECK_DEADLINE_SECONDS)
    connection.row_factory = sqlite3.Row
    return connection


def _select(database, statements):
    """Run the fixed read-only statements and return their rows, or a verdict for the failure."""
    try:
        with _connect(database) as connection:
            return [connection.execute(statement).fetchone() for statement in statements], None
    except sqlite3.Error as error:
        return None, failed(retryable=error.sqlite_errorcode in RETRYABLE_SQLITE)
    except (OSError, ValueError):
        return None, failed()


def probe_database(database):
    """The database is present, readable and at a schema this build understands.

    `mode=ro` is what makes "absent" mean absent: opening a path that does not exist fails instead
    of creating an empty database. `immutable=1` is deliberately *not* used, because it would hide
    a WAL that still holds committed data this build must see.
    """
    path = Path(str(database))
    if not path.exists():
        return NOT_CONFIGURED
    if not path.is_file():
        return failed()
    rows, verdict = _select(
        path,
        (
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'",
            "SELECT value FROM metadata WHERE key='schema'",
        ),
    )
    if verdict is not None:
        return verdict
    if rows[0] is None:
        return failed()
    version = rows[1][0] if rows[1] is not None else None
    if version not in {"1", "2", "3"}:
        return failed()
    if version != "3":
        # An older schema is a real, readable database this deployment has not migrated. Reporting
        # it ready would claim an upgrade nobody performed; migrating it here is out of the
        # question, because a probe must not write.
        return failed()
    return OK


def probe_installed(database, key, value="1"):
    """Whether one recorded schema marker is present with its expected value."""
    rows, verdict = _select(database, (f"SELECT value FROM metadata WHERE key='{key}'",))
    if verdict is not None:
        return verdict
    if rows[0] is None or rows[0][0] != value:
        return failed()
    return OK


def probe_present(database, key):
    """Whether a recorded key exists at all, without reading what it holds.

    Used for the one metadata row whose value is itself a secret: the deployment must have a seal
    key, and whether it does is a single boolean. Its bytes are never compared, returned or logged,
    so a readiness verdict cannot become a way to read the value out of a database.
    """
    rows, verdict = _select(database, (f"SELECT 1 FROM metadata WHERE key='{key}'",))
    if verdict is not None:
        return verdict
    if rows[0] is None:
        return failed()
    return OK


def probe_guard(database, guard_path):
    """The independent schema 3 checkpoint agrees with the database it guards.

    A checkpoint is required exactly when the database records one. A missing, unreadable or
    divergent file is not-ready and is never rewritten, deleted or re-initialized here; a database
    whose metadata does not yet record a revision legitimately has no checkpoint to compare.
    """
    try:
        with _connect(database) as connection:
            expected = source_recovery.checkpoint(connection)
    except sqlite3.Error as error:
        return failed(retryable=error.sqlite_errorcode in RETRYABLE_SQLITE)
    except (OSError, ValueError):
        return failed()
    if expected is None:
        return OK
    guard = Path(guard_path)
    if not guard.is_file():
        return NOT_CONFIGURED
    try:
        actual = json.loads(guard.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return failed()
    if canonical(actual) != canonical(expected):
        return failed()
    return OK


def probe_mode(raw):
    """Whether this process runs a mode a production deployment may call ready.

    The production container refuses `local_fixture`: its sources are synthetic, so a fixture
    backed process is deliberately not ready. Any other unrecognized value fails closed rather
    than being guessed as a real mode.
    """
    if raw.get("mode") == "local_fixture":
        return failed()
    if raw.get("mode") != "source_sync":
        return failed()
    return OK


def probe_knowledge_client(settings, raw):
    """The fixed client this process serves is still a registered knowledge principal.

    Only the registration is checked: whether this credential, project list and permission list
    currently authorize anything is still decided per request by the domain. A client that was
    unregistered since startup is not-ready, because this process can no longer serve its identity
    at all.
    """
    knowledge = raw.get("knowledge")
    if not isinstance(knowledge, dict):
        return failed()
    clients = knowledge.get("clients")
    if not isinstance(clients, dict):
        return failed()
    principal = clients.get(settings.client)
    if not isinstance(principal, dict):
        return failed()
    digest = principal.get("credential_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return failed()
    if not isinstance(principal.get("permissions"), list) or not principal["permissions"]:
        return failed()
    if not isinstance(principal.get("projects"), list):
        return failed()
    return OK


# The project-knowledge schema families, each named by what its own migration records. The base
# family is the minimum any project-knowledge deployment has; every other family is installed by its
# own explicit operator step, so a deployment that never ran one must not be reported ready for it —
# and must not be reported broken for it either. The catalogue is required by this build, because
# the permission allowlist this process serves includes its two operations.
#
# `knowledge_seal_key` is not a version marker: its value is the generated key itself, so only its
# presence is read and its bytes are never compared, reported or logged.
KNOWLEDGE_MARKERS = {
    "knowledge_schema": "1",
    "knowledge_seal_key": None,
    "research_notes_schema": "1",
    "knowledge_directories_schema": "1",
    "lessons_schema": "1",
    "knowledge_catalog_schema": "1",
}
KNOWLEDGE_BASE_MARKERS = ("knowledge_schema", "knowledge_seal_key")
KNOWLEDGE_OPTIONAL_MARKERS = (
    "research_notes_schema",
    "knowledge_directories_schema",
    "lessons_schema",
)
KNOWLEDGE_REQUIRED_MARKERS = ("knowledge_catalog_schema",)


def probe_knowledge_extensions(database):
    """The schema family this deployment holds, reported against what this build serves.

    Three answers, and the distinction between them is the point:

    - `ok` — the base family is installed, every family that was migrated really is installed, and
      the catalogue this build serves is installed. Nothing is claimed beyond what is there;
    - `not_configured` — the base family is installed but the catalogue is not, which is a
      deployment whose catalogue step has not run. It is reported rather than claimed, so the
      process never says it is ready for a capability it lacks;
    - `failed` — the base family is missing (this is not a project-knowledge database at all) or the
      option families contradict each other. The reviewed migration installs the note book, the
      directory plans and the lesson book together, so one of them present without the others is not
      a state any migration sequence produces: it is a failure, not a feature that was never
      installed.
    """
    present = {}
    for key, expected in KNOWLEDGE_MARKERS.items():
        outcome = (
            probe_installed(database, key) if expected is not None else probe_present(database, key)
        )
        if outcome.state == "not_configured":
            return failed()
        present[key] = outcome.state == "ok"
    if not all(present[key] for key in KNOWLEDGE_BASE_MARKERS):
        return failed()
    if not all(present[key] for key in KNOWLEDGE_REQUIRED_MARKERS):
        return NOT_CONFIGURED
    installed_optional = [key for key in KNOWLEDGE_OPTIONAL_MARKERS if present[key]]
    if len(installed_optional) not in {0, len(KNOWLEDGE_OPTIONAL_MARKERS)}:
        return failed()
    if not installed_optional:
        return failed()
    return OK


def probe_log(diagnostics):
    """Whether the runtime event log is durable and currently working.

    This reads the latch in memory: a durable log whose writes stopped working is not-ready, and a
    process with no configured log directory is non-durable, which is never ready in production.
    Nothing is created, opened or written here.
    """
    if not diagnostics.durable:
        return NON_DURABLE
    if diagnostics.sink.failure is not None:
        return failed()
    try:
        if not diagnostics.sink.directory.is_dir():
            return failed()
    except OSError:
        return failed()
    return OK


def probe_ownership(runtime, handles):
    """Whether the object that owns this process's database handle is still held.

    A released owner means the process is winding down: the files may still look perfect while the
    service can no longer answer. This is the one readiness condition no file can express, so it is
    read from the live assembly instead, through a callable the entry point supplied.
    """
    if runtime is None:
        return NOT_CONFIGURED
    if getattr(runtime, "released", False):
        return failed()
    if handles is None:
        # This entry owns no separately releasable handle; its assembled state is the whole answer,
        # which the `assembled` check already reports.
        return OK
    try:
        return OK if handles() else failed()
    except Exception:  # noqa: BLE001 - a probe never raises at an operator
        return failed()


def assess(settings, diagnostics, runtime=None):
    """Every check for one service, keyed by the closed set readiness reports."""
    checks = {
        "configuration": probe_configuration(settings),
        "contract": probe_contract(settings.contract_path),
        "log": probe_log(diagnostics),
        # A remote Platform, Chat Audit or model service is never called from here, so this is
        # always "not tested" — never a success a local read could not establish.
        "remote": NOT_VERIFIED,
        "assembled": OK if runtime is not None else NOT_CONFIGURED,
        "owner": probe_ownership(runtime, settings.handles),
    }
    raw = _read(settings.config_path)
    if raw is None:
        for key in keys_for(settings.service):
            checks.setdefault(key, failed())
        return checks
    database = raw.get("database_path") or ""
    checks["database"] = probe_database(database)
    if settings.service == CHAT_SERVICE:
        guard = (raw.get("source_sync") or {}).get("recovery_path")
        checks["guard"] = probe_guard(database, guard or str(database) + ".source-guard.json")
        checks["mode"] = probe_mode(raw)
    else:
        checks["client"] = probe_knowledge_client(settings, raw)
        checks["extensions"] = probe_knowledge_extensions(database)
    return checks


def _read(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def keys_for(service):
    return CHECKS_BY_SERVICE[service]


def readiness(settings):
    """The closed readiness document: a status, the service and one map of checks.

    Exactly three fields, each with a fixed shape, so nothing about the configuration, the data, a
    project, a count or a path can leak out of a probe.
    """
    keys = keys_for(settings.service)
    if settings.runtime is not None and not settings.runtime.active:
        # A runtime that has begun shutting down, or whose owner released it, is not a runtime that
        # may report ready, whatever the files still say.
        return _document(settings, {key: failed() for key in keys}, keys)
    checks = assess(settings, settings.diagnostics, settings.runtime)
    # One bounded retry, and only for a check whose own failure said asking again could help. It
    # never recurses, so a permanently busy database costs one extra attempt and then settles.
    if any(outcome.retryable for outcome in checks.values()):
        time.sleep(RETRY_SECONDS)
        again = assess(settings, settings.diagnostics, settings.runtime)
        checks = {key: again[key] if value.retryable else value for key, value in checks.items()}
    return _document(settings, checks, keys)


# How each check decides the verdict, stated per key rather than inferred from one value, because
# `not_configured` legitimately means two different things. For a core prerequisite it means this
# process cannot establish the condition at all, which is exactly the contract's "a core
# prerequisite left unconfigured is never ready". For an optional capability it means the operator
# never installed it, and the contract's rule there is narrower: the deployment must not *claim the
# extension*, which reporting the state already satisfies.
BLOCKING_STATES = {
    "configuration": frozenset({"failed", "not_configured", "non_durable"}),
    "contract": frozenset({"failed", "not_configured", "non_durable"}),
    "database": frozenset({"failed", "not_configured", "non_durable"}),
    # A database that records a recovery revision requires its checkpoint, so an absent one is a
    # missing core prerequisite. A database with no revision yet legitimately has no checkpoint to
    # compare, and `probe_guard` reports that case as `ok` rather than as absent.
    "guard": frozenset({"failed", "not_configured", "non_durable"}),
    "mode": frozenset({"failed", "not_configured", "non_durable"}),
    "client": frozenset({"failed", "not_configured", "non_durable"}),
    "extensions": frozenset({"failed", "non_durable"}),
    "log": frozenset({"failed", "not_configured", "non_durable"}),
    "assembled": frozenset({"failed", "not_configured", "non_durable"}),
    "owner": frozenset({"failed", "not_configured", "non_durable"}),
    # A dependency this process never called is reported `not_verified`. The contract's whole point
    # is that such a state must be visible rather than turned into a claimed success, and it is not
    # one of the three local conditions readiness is decided by.
    "remote": frozenset({"failed", "non_durable"}),
}
# A check key no rule mentions fails closed: an unreviewed readiness condition never passes.
DEFAULT_BLOCKING = frozenset(CHECK_STATES - {"ok", "not_verified"})


def _document(settings, checks, keys):
    ordered = {key: checks[key].state for key in keys}
    ready = all(
        state not in BLOCKING_STATES.get(key, DEFAULT_BLOCKING) for key, state in ordered.items()
    )
    return {
        "status": "ready" if ready else "not_ready",
        "service": settings.service,
        "checks": ordered,
    }


def present_token(request, diagnostics):
    """The independent diagnostics token, or the refusal that goes with its absence.

    A missing or malformed `Authorization: Bearer` is 401; a process that was never given a token
    is 503, because it cannot authenticate anyone at all. The comparison is constant time, the
    token is never logged and it grants no business permission of any kind.
    """
    token = os.environ.get(diagnostics.token_env) if diagnostics.token_env else None
    if not token:
        return "unconfigured"
    header = request.headers.get("authorization")
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return "unauthorized"
    presented = header[len("Bearer ") :]
    if not presented or not hmac.compare_digest(presented, token):
        return "unauthorized"
    return "ok"
