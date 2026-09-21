"""Deployment assembly for the two Memory HTTP services.

This module is the only place that decides how a Memory process is bound, protected and stopped.
It adds no domain rule: it reads explicit arguments, calls the factory it is handed, installs the
diagnostic stack and the two read-only probes, and serves exactly one process per container.

The rules it does own are transport rules, and every one of them is explicit:

- `--host` accepts an IP literal only. The default stays `127.0.0.1`, so an operator who says
  nothing keeps the loopback service the product already had;
- a non-loopback address is refused unless a loadable TLS certificate *and* private key *and* an
  explicit list of legal authorities are all given. There is no plain-HTTP path off the loopback
  interface and no "trusted proxy" escape: TLS terminates in this process through uvicorn, and a
  forwarding header is never consulted or trusted;
- every request must name one of the configured authorities on the port this process serves. That
  check runs on the exact `Host` header, before the diagnosis middleware and before any route;
- authorities are normalized: an IPv6 literal is compared in its bracketed, canonical form, so
  `::1`, `0:0:0:0:0:0:0:1` and `[::1]:8130` all name the same service, while another name,
  another port and a wildcard are refused.

Startup and shutdown are recorded through the diagnostic adapter, which is also what keeps
readiness truthful: a runtime that is starting, stopping or already stopped is never ready.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import re
import signal
import ssl
from pathlib import Path

from .diagnostics import NO_STORE as DIAGNOSTIC_NO_STORE
from .diagnostics import Diagnostics
from .domain import Fault
from .runtime_probes import CHECK_DEADLINE_SECONDS, keys_for, present_token, readiness

DEFAULT_HOST = "127.0.0.1"
LOOPBACK_NAMES = frozenset({"localhost"})
# A DNS name an operator may legitimately put in an authority list. Deliberately narrow: this is
# not a general URL parser, and anything it does not understand is refused rather than allowed.
HOSTNAME_PATTERN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\Z")
# Every response this runtime writes is uncacheable and identical in shape to the ones the
# diagnostic adapter writes, so a probe answer cannot be stored by anything between here and the
# operator, and a refusal cannot be told apart from a verdict by its headers.
NO_STORE = dict(DIAGNOSTIC_NO_STORE)


class Binding:
    """Where this process listens and which authorities may address it."""

    __slots__ = ("allowed", "host", "port", "tls")

    def __init__(self, host, port, allowed, tls=None):
        self.host = host
        self.port = port
        self.allowed = frozenset(allowed)
        self.tls = tls

    @property
    def loopback(self):
        return ipaddress.ip_address(self.host).is_loopback

    def authority(self):
        text = f"[{self.host}]" if ":" in self.host else self.host
        return f"{text}:{self.port}"

    def accepts(self, header):
        """Whether a `Host` header names exactly this authority, and nothing else.

        The whole header must name one of the configured authorities on exactly this port. A missing
        header, a different port, an unknown name and an empty value are all refused here.

        The presented name is resolved through the same rule the configured names went through, so an
        authority is judged by what it names rather than by how it was spelled: the long form of an
        IPv6 address is the same interface, and a value this runtime cannot parse is refused instead
        of being compared as text.
        """
        if not isinstance(header, str):
            return False
        name, port = split_authority(header.strip().lower())
        if port != str(self.port):
            return False
        try:
            return normalize_authority(name, self.port) in self.allowed
        except Fault:
            return False


def ip_literal(value):
    """The canonical text of an IP literal, or None when the value is not one.

    Lower-case throughout, because an authority is compared as text: `2001:0DB8::1` and
    `2001:db8::1` name the same interface, and refusing the first because it was written in capitals
    would be an accident of spelling rather than a rule. A zone identifier is refused: `fe80::1%eth0`
    names an interface on one host, not an authority a client anywhere can address.
    """
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        return str(ipaddress.ip_address(value)).lower()
    except ValueError:
        return None


def split_authority(value):
    """Split `host[:port]`, keeping an IPv6 literal in its bracketed form."""
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return value, ""
        tail = value[end + 1 :]
        if tail == "":
            return value[: end + 1], ""
        if tail.startswith(":"):
            return value[: end + 1], tail[1:]
        return value, ""
    name, separator, port = value.rpartition(":")
    if not separator or ":" in name:
        # No port at all, or a bare IPv6 literal that was never bracketed.
        return value, ""
    return name, port


def normalize_authority(value, port):
    """One configured authority as a comparable name string, or a refusal.

    A configured port is accepted only when it is this process's own port: naming a different port
    in the list would describe a service this process is not.
    """
    name, declared = split_authority(str(value).strip().lower())
    if not name or (declared and declared != str(port)):
        raise Fault("invalid_configuration", 503)
    if name.startswith("["):
        if not name.endswith("]"):
            raise Fault("invalid_configuration", 503)
        literal = ip_literal(name[1:-1])
        if literal is None:
            raise Fault("invalid_configuration", 503)
        return f"[{literal}]"
    if name in LOOPBACK_NAMES:
        return name
    literal = ip_literal(name)
    if literal is not None:
        return f"[{literal}]" if ":" in literal else literal
    if not HOSTNAME_PATTERN.fullmatch(name):
        raise Fault("invalid_configuration", 503)
    return name


def resolve_host(value):
    """The bind address: an IP literal, or a refusal. A hostname is never resolved implicitly."""
    literal = ip_literal(value)
    if literal is None:
        raise Fault("invalid_configuration", 503)
    return literal


def default_allowed(host, port):
    """The authorities a loopback bind serves when it is not told: its own loopback names."""
    literal = ipaddress.ip_address(host)
    names = {literal.compressed.lower()}
    if literal.version == 6:
        names.add(f"[{literal.compressed.lower()}]")
    names |= set(LOOPBACK_NAMES)
    return tuple(sorted(names))


def tls_context(certfile, keyfile):
    """Build the server TLS context from explicit files, or refuse to start.

    Both files must be given, must exist and must actually load: a mismatched pair, an unreadable
    key or an encrypted key without its password is a refusal at startup rather than a service that
    looks alive and then fails every single handshake.
    """
    if certfile is None and keyfile is None:
        return None
    if not certfile or not keyfile:
        raise Fault("invalid_configuration", 503)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    except (OSError, ssl.SSLError, ValueError, ImportError):
        raise Fault("invalid_configuration", 503) from None
    return context


def resolve_binding(*, host, port, certfile, keyfile, allowed_hosts):
    """The complete binding decision, or the refusal that stops this process from starting."""
    host = resolve_host(host or DEFAULT_HOST)
    if type(port) is not int or not 1 <= port <= 65535:
        raise Fault("invalid_configuration", 503)
    loopback = ipaddress.ip_address(host).is_loopback
    # A certificate without its key (or the reverse) is a half-configured TLS deployment, and half
    # a TLS deployment is not one. It is refused whatever the bind address; only "neither" is
    # allowed, and only on the loopback interface.
    if not certfile and not keyfile and loopback:
        names = (
            tuple(normalize_authority(name, port) for name in allowed_hosts)
            if allowed_hosts
            else default_allowed(host, port)
        )
        if not names:
            raise Fault("invalid_configuration", 503)
        return Binding(host, port, names)
    # Off the loopback interface: TLS and an explicit authority list are both mandatory. Neither
    # is inferred, and no plain-HTTP deployment is reachable this way. The authority list is checked
    # first because it is the decision that needs no files to read: an operator who has given
    # neither an authority nor a certificate is told about the missing authority, not about a file
    # that was never named, and neither one lets the process start.
    if not allowed_hosts:
        raise Fault("invalid_configuration", 503)
    tls_context(certfile, keyfile)
    names = tuple(normalize_authority(name, port) for name in allowed_hosts)
    if not names:
        raise Fault("invalid_configuration", 503)
    return Binding(host, port, names, tls=(certfile, keyfile))


def install_networking(app, binding):
    """Refuse any request that an off-loopback binding may not serve.

    The check runs on the raw `Host` header, before the diagnostic middleware, and consults no
    forwarding header. It is deliberately **not applied to a loopback binding**: that binding is
    reachable only from this host, and every existing entry point already accepted any authority
    there, so tightening it would change behavior this task was not asked to change. What a
    loopback bind cannot reach is a browser: an `Origin` or `Sec-Fetch-Site` is refused there too.

    An off-loopback binding is the one that needs the allowlist. It accepts only the authorities the
    operator named, and it refuses a browser origin outright, because this service has no cookie or
    CORS surface to serve one.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def authority_middleware(request: Request, call_next):
        browser = (
            request.headers.get("origin") is not None
            or request.headers.get("sec-fetch-site") is not None
        )
        if binding.loopback:
            if browser:
                return JSONResponse(
                    {"status": "failed", "code": "browser_origin_refused"},
                    status_code=400,
                    headers=dict(NO_STORE),
                )
            return await call_next(request)
        if not binding.accepts(request.headers.get("host")):
            return JSONResponse(
                {"status": "failed", "code": "invalid_host"},
                status_code=400,
                headers=dict(NO_STORE),
            )
        if browser:
            return JSONResponse(
                {"status": "failed", "code": "browser_origin_refused"},
                status_code=400,
                headers=dict(NO_STORE),
            )
        return await call_next(request)

    return app


def config_path_from_environment(explicit=None):
    """The one explicit configuration path; no production default is invented."""
    path = explicit or os.environ.get("TIANSHU_MEMORY_CONFIG")
    if not path:
        raise Fault("invalid_configuration", 503)
    return path


def diagnostics_contract_path(raw, config_path, explicit=None):
    """Where the frozen diagnostics package is kept, or None when this process cannot say.

    Three sources, in order, and no guessed production path:

    1. `serve --diagnostics-contract`, when the operator names the package outright;
    2. the `diagnostics/v1` package beside the product contracts the configuration already points
       at, because a deployment keeps the published contract tree together;
    3. for an isolated development tree, the workspace named by this checkout's
       `.runtime/workspace-context.json`, which is the same pointer the test fixtures already use
       to find the published contracts.

    None means the location could not be established. That is not fatal here: the process starts
    with a log it can write, and its own readiness check reports the contract as not configured, so
    an operator sees a precise not-ready rather than a service that pretends to be complete.
    """
    if explicit:
        return Path(explicit)
    directory = raw.get("contract_directory")
    if isinstance(directory, str) and directory.strip():
        return Path(directory).expanduser().resolve().parent.parent / "diagnostics" / "v1"
    return workspace_diagnostics_path(config_path)


def workspace_diagnostics_path(config_path):
    """The published diagnostics package of the workspace this checkout points at, or None."""
    current = Path(config_path).resolve().parent
    while True:
        if (current / ".git").exists():
            context = current / ".runtime" / "workspace-context.json"
            try:
                workspace = json.loads(context.read_text(encoding="utf-8")).get("workspace")
            except (OSError, ValueError):
                return None
            if not isinstance(workspace, str) or not workspace:
                return None
            return Path(workspace) / "contracts" / "diagnostics" / "v1"
        if current.parent == current:
            return None
        current = current.parent


class Assembly:
    """One assembled process: its app, its adapter, its binding and its lifecycle state."""

    __slots__ = (
        "app",
        "binding",
        "diagnostics",
        "probe_factory",
        "ready_announced",
        "state",
    )

    def __init__(self, app, diagnostics, binding, probe_factory=None):
        self.app = app
        self.diagnostics = diagnostics
        self.binding = binding
        self.probe_factory = probe_factory
        self.state = "starting"
        self.ready_announced = False

    @property
    def active(self):
        """Whether this assembly may still report ready: only while it is actually serving."""
        return self.state == "active"

    def started(self):
        self.diagnostics.emit("runtime.started", level="INFO", outcome="succeeded")
        self.state = "active"

    def announce_ready(self, document):
        """Record the first successful readiness verdict, once, and only while active."""
        if document["status"] != "ready" or self.ready_announced or not self.active:
            return
        self.ready_announced = True
        self.diagnostics.emit("runtime.ready", level="INFO", outcome="succeeded")

    def stopping(self):
        if self.state == "stopped":
            return
        self.state = "stopping"
        self.diagnostics.emit("runtime.stopping", level="INFO", outcome="started")

    def stopped(self):
        if self.state == "stopped":
            return
        self.state = "stopped"
        self.diagnostics.emit("runtime.stopped", level="INFO", outcome="succeeded")
        self.diagnostics.sink.close()

    def probe_settings(self):
        """The read-only probe settings for this exact assembly, built by the entry point.

        The entry point knows which live objects prove it is assembled; this class only carries the
        answer. Assembling without that factory is refused rather than reported as ready on a
        technicality.
        """
        if self.probe_factory is None:
            raise Fault("invalid_configuration", 503)
        return self.probe_factory(self)


def build_assembly(service, build_app, *, config_path, contract_path, binding, probe_settings=None):
    """Assemble one Memory process: read the private configuration, install, and return.

    A configuration that cannot be read, or whose diagnostics section is malformed, stops the
    process here — before the socket exists — instead of producing a service that starts and then
    cannot record what it does. Once the adapter exists, a failure anywhere after it is recorded as
    `runtime.start_failed`, and the refusal itself is passed through unchanged so an operator still
    sees the real code rather than a generic startup error.
    """
    raw = read_configuration(config_path)
    resolved = diagnostics_contract_path(raw, config_path, contract_path)
    # `raw` goes in whole: the adapter reads its own `diagnostics` section, and a section that is
    # malformed or a log directory that is missing is refused here, before the socket exists, rather
    # than becoming a process that serves and cannot record what it did.
    diagnostics = Diagnostics(service, raw, config_path=config_path, contract_path=resolved)
    diagnostics.emit("runtime.starting", level="INFO", outcome="started")
    try:
        assembly = Assembly(build_app(), diagnostics, binding, probe_factory=probe_settings)
        diagnostics.install(assembly.app)
        install_networking(assembly.app, binding)
        install_probe_routes(assembly.app, assembly)
    except Fault:
        diagnostics.emit("runtime.start_failed", level="ERROR", outcome="failed")
        diagnostics.sink.close()
        raise
    except BaseException:
        diagnostics.emit("runtime.start_failed", level="ERROR", outcome="failed")
        diagnostics.sink.close()
        raise Fault("invalid_configuration", 503) from None
    return assembly


def read_configuration(config_path):
    """The private configuration, or the refusal that keeps this process from starting."""
    try:
        with open(config_path, encoding="utf-8") as stream:
            raw = json.loads(stream.read())
    except (OSError, ValueError):
        raise Fault("invalid_configuration", 503) from None
    if not isinstance(raw, dict):
        raise Fault("invalid_configuration", 503)
    if not isinstance(raw.get("database_path"), str) or not raw["database_path"].strip():
        raise Fault("invalid_configuration", 503)
    return raw


def install_probe_routes(app, assembly):
    """The two read-only probes, bound to this assembly's own settings.

    They stay out of the request middleware by exact path, so asking a process whether it is alive
    cannot change what it is alive *about*.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from .diagnostics import LIVE_BODY

    @app.get("/health/live", status_code=200)
    async def live(request: Request):
        return JSONResponse(LIVE_BODY, status_code=200, headers=dict(NO_STORE))

    @app.get("/health/ready")
    async def ready(request: Request):
        # The body of this route is the closed document and nothing else, on every path out of it:
        # `status`, `service`, `checks`. A refusal is expressed by the HTTP status, never by adding
        # a code, a message or an operator hint to the document — a not-ready check document with
        # `checks` all `failed` already says which process could not establish itself.
        token = present_token(request, assembly.diagnostics)
        if token == "unconfigured":
            return JSONResponse(
                closed_document(assembly.diagnostics.service),
                status_code=503,
                headers=dict(NO_STORE),
            )
        if token == "unauthorized":
            return JSONResponse(
                closed_document(assembly.diagnostics.service),
                status_code=401,
                headers=dict(NO_STORE),
            )
        try:
            document = await asyncio.wait_for(
                asyncio.to_thread(readiness, assembly.probe_settings()),
                timeout=CHECK_DEADLINE_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            # The bounded probe deadline is a real verdict: this process could not establish its
            # own local prerequisites in time, so it is not ready. Liveness is unaffected, because
            # it does no work that could block.
            document = closed_document(assembly.diagnostics.service)
        assembly.announce_ready(document)
        return JSONResponse(
            document,
            status_code=200 if document["status"] == "ready" else 503,
            headers=dict(NO_STORE),
        )

    return app


def closed_document(service):
    """The closed not-ready document, with every check reported as failed."""
    return {
        "status": "not_ready",
        "service": service,
        "checks": {key: "failed" for key in keys_for(service)},
    }


def add_serve_arguments(parser: argparse.ArgumentParser):
    """The deployment options `serve` accepts: binding, TLS and legal authorities.

    Every one of them is explicit. There is no environment-variable fallback for a bind address, a
    certificate, a key or an authority list, so a deployment cannot become reachable through a
    value nobody wrote down in the unit that starts it.
    """
    parser.add_argument("--host", default=DEFAULT_HOST, help="IP literal to bind; default loopback")
    parser.add_argument(
        "--tls-certfile", default=None, help="PEM certificate chain; required off loopback"
    )
    parser.add_argument(
        "--tls-keyfile", default=None, help="PEM private key; required off loopback"
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Legal Host authority (repeatable); required off loopback",
    )
    parser.add_argument(
        "--diagnostics-contract",
        default=None,
        help="Directory holding the frozen diagnostics/v1 package; default beside the contracts",
    )
    return parser


def serve(service, build_app, *, args, binding=None, probe_settings=None):
    """Run one Memory process: assemble, listen, and stop on this process's own signals."""
    if binding is None:
        binding = resolve_binding(
            host=args.host,
            port=args.port,
            certfile=args.tls_certfile,
            keyfile=args.tls_keyfile,
            allowed_hosts=args.allowed_host or [],
        )
    config_path = config_path_from_environment(args.config)
    assembly = build_assembly(
        service,
        build_app,
        config_path=config_path,
        contract_path=args.diagnostics_contract,
        binding=binding,
        probe_settings=probe_settings,
    )

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            assembly.app,
            host=binding.host,
            port=binding.port,
            ssl_certfile=args.tls_certfile,
            ssl_keyfile=args.tls_keyfile,
            access_log=False,
            log_level="warning",
        )
    )
    return run_until_stopped(server, assembly)


def run_until_stopped(server, assembly, *, install_signals=True):
    """Serve until this process is asked to stop, recording both ends of that lifecycle.

    The server object is driven directly rather than through `Server.run`, for one reason: what
    "this process is now serving" and "this process is now stopping" mean has to be stated at the
    exact moment it becomes true, instead of being inferred from a function that might return.
    Readiness depends on that, so it is not left to inference.

    Signal handling stays this process's own, and only for the duration of the call. On Windows
    `SIGTERM` cannot be registered and `SIGBREAK` is used for a console close; either way the
    listener is restored before returning, so this never becomes a second, global handler.
    """
    if install_signals:
        _install_signal_handlers(server)
    try:
        asyncio.run(_serve(server, assembly))
    except BaseException:
        # An interrupt, a failure to bind or a lifespan error: the process is stopping either way,
        # and both ends of that are recorded before it goes.
        assembly.stopping()
        assembly.stopped()
        raise
    assembly.stopping()
    assembly.stopped()
    return 0


def _install_signal_handlers(server):
    """Ask the server to exit on the process signals this platform actually supports."""
    handled = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, lambda *_: setattr(server, "should_exit", True))
        except (OSError, ValueError):
            continue
        handled.append(number)
    return handled


async def _serve(server, assembly):
    """Start, serve, and shut down — recording the transitions rather than assuming them.

    Driving the server object directly means loading its own configuration and building its own
    lifespan first, exactly as `Server.run` does before it calls `startup`. Skipping either would
    make a perfectly healthy configuration look like a failed start, so both are done here
    explicitly and a startup that really did fail is recorded as such rather than guessed.
    """
    if not server.config.loaded:
        server.config.load()
    server.lifespan = server.config.lifespan_class(server.config)
    await server.startup()
    if not getattr(server, "started", False):
        # The socket never opened: this process is not serving, and saying `runtime.started` here
        # would be exactly the false success the log exists to prevent.
        assembly.diagnostics.emit("runtime.start_failed", level="ERROR", outcome="failed")
        return
    assembly.started()
    try:
        await server.main_loop()
    finally:
        assembly.stopping()
        await server.shutdown(sockets=getattr(server, "servers", None))
        assembly.stopped()
