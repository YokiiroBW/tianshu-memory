"""Container liveness probe: one bounded TLS request, no shell tool and no certificate bypass.

The image has no package manager and no shell HTTP tool, so this is the whole probe: open one
verified TLS connection to this container's own announced authority, read at most four kilobytes of
one response, and decide from the body alone.

It is deliberately strict about the two things a healthcheck most often gets wrong:

- **the certificate is verified.** `ssl.create_default_context()` performs hostname and chain
  verification, and no environment variable can turn that off. A certificate the container cannot
  chain to a trust anchor fails the probe, because a probe that cannot tell this service apart from
  anything else that answers on the port has not checked anything. A private trust anchor is given
  as a file path (`TIANSHU_HEALTHCHECK_CA`), which is loaded in addition to the system roots — never
  as a switch that disables verification.
- **the authority is the one that was announced.** One value carries the whole authority
  (`TIANSHU_HEALTHCHECK_HOST`), and both the `Host` header and the port this probe connects to come
  from it, because the server refuses any other authority and a probe that took its name from one
  value and its port from another could check a different service than the one it announced.

Only the liveness route is asked. `/health/live` answers `{"status":"alive"}` whenever the process
is running; readiness is a separate decision with its own credential, and a container restart must
not be driven by a check that deliberately reports `not_ready` while a dependency is unwell.

Exit status:

- `0` the service is alive;
- `1` the process answered, but not with the liveness document — this is the service saying it is
  unwell, and the only outcome that should ever restart a container;
- `2` the probe could not reach a verdict: an announcement it cannot parse, a trust anchor it cannot
  read, a certificate it cannot verify, a port nothing answers on, or an answer larger than this
  probe will read. A process that may be perfectly healthy must not be restarted for any of those.

Nothing here reads, writes or logs a credential, and a response body is never printed: only a fixed
phrase, and at most an HTTP status.
"""

import json
import os
import socket
import ssl
import sys

LIVE_PATH = "/health/live"
LIVE_BODY = {"status": "alive"}
# Two seconds each for connecting and for reading. A liveness probe that can hang for longer than
# its own interval turns a slow moment into a restart, which is the opposite of what it is for.
CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 2.0
# Four kilobytes is about a hundred times the body this route returns and a hard ceiling on what a
# misbehaving service can make the probe hold in memory.
MAXIMUM_RESPONSE_BYTES = 4096
MAXIMUM_HEADER_LINES = 64
DEFAULT_HTTPS_PORT = 443
EXIT_ALIVE = 0
EXIT_NOT_ALIVE = 1
EXIT_UNREACHABLE = 2


class Overlong(Exception):
    """The service answered with more bytes than this probe will read."""


def fail(code, message):
    """One fixed phrase on standard error; never a body, a header or an exception's text."""
    print(f"tianshu-healthcheck: {message}", file=sys.stderr)
    return code


def split_authority(value):
    """Split `host[:port]`, keeping an IPv6 literal in its bracketed form."""
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("the announced authority is not a bracketed address")
        tail = value[end + 1 :]
        if tail == "":
            return value[: end + 1], ""
        if tail.startswith(":"):
            return value[: end + 1], tail[1:]
        raise ValueError("the announced authority has trailing text after its address")
    name, separator, port = value.rpartition(":")
    if not separator or ":" in name:
        # No port at all, or a bare IPv6 literal that was never bracketed.
        return value, ""
    return name, port


def authority():
    """The announced authority, or the refusal that keeps this probe from guessing one.

    One value carries both parts, because an authority is what the server's own list holds and what
    the `Host` header must match. The port it names is the port this probe connects to: a probe that
    took the name from one value and the port from another could check a different service than the
    one it announced, which is exactly the mistake a healthcheck is trusted not to make.
    """
    host = os.environ.get("TIANSHU_HEALTHCHECK_HOST", "").strip()
    if not host:
        raise ValueError("TIANSHU_HEALTHCHECK_HOST must name the announced authority")
    if any(character in host for character in "/\\ \t\r\n"):
        raise ValueError("the announced authority contains a character an authority cannot hold")
    name, declared = split_authority(host.lower())
    if not name:
        raise ValueError("the announced authority has no host")
    if declared:
        if not declared.isdigit() or not 1 <= int(declared) <= 65535:
            raise ValueError("the announced port is outside the port range")
        port = int(declared)
    else:
        port = DEFAULT_HTTPS_PORT
    return host, port


def hostname(host):
    """The name to connect to and verify: the authority's name without its port or brackets."""
    name, _ = split_authority(host)
    if name.startswith("["):
        return name[1:-1]
    return name


def context():
    """A verifying TLS context, optionally with one operator-supplied trust anchor."""
    anchor = os.environ.get("TIANSHU_HEALTHCHECK_CA", "").strip()
    if anchor:
        if not os.path.isfile(anchor):
            raise ValueError("the given trust anchor is not a readable file")
        return ssl.create_default_context(cafile=anchor)
    return ssl.create_default_context()


def send_request(tls, host, port):
    """Write the request line and headers, and read the bounded response back."""
    request = (
        f"GET {LIVE_PATH} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "User-Agent: tianshu-healthcheck/1\r\n"
        "\r\n"
    ).encode("ascii")
    name = hostname(host)
    with socket.create_connection((name, port), timeout=CONNECT_TIMEOUT_SECONDS) as raw:
        with tls.wrap_socket(raw, server_hostname=name) as stream:
            stream.settimeout(READ_TIMEOUT_SECONDS)
            stream.sendall(request)
            # The size bound stops a misbehaving service from making this probe buffer without
            # limit. It is enforced while reading rather than after, so one oversized response is
            # the most this probe ever holds, and it is reported as its own failure rather than as
            # an unreachable port: "the service answered too much" and "nothing answered" send an
            # operator to two different places.
            received = b""
            while len(received) <= MAXIMUM_RESPONSE_BYTES:
                chunk = stream.recv(1024)
                if not chunk:
                    break
                received += chunk
    if len(received) > MAXIMUM_RESPONSE_BYTES:
        raise Overlong("the liveness answer exceeded the size this probe will read")
    return received


def status_and_body(received):
    """The status code and body of one HTTP/1.1 response, or a refusal."""
    head, separator, body = received.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("the response ended before its headers did")
    lines = head.split(b"\r\n")
    if len(lines) > MAXIMUM_HEADER_LINES:
        raise ValueError("the response had more header lines than this probe will read")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/1.") or not parts[1].isdigit():
        raise ValueError("the response did not begin with an HTTP status line")
    return int(parts[1]), body


def main():
    # The three failure classes are separated because they ask an operator for three different
    # things, and a message that names the wrong one sends them to the wrong place: a malformed
    # announcement is a configuration mistake, an unverifiable certificate is a trust mistake, and
    # no answer at all is a process that is not listening. All three are status 2 — "this probe
    # could not establish liveness" — and none of them is the service saying it is unwell, which is
    # the only thing that should ever restart a container.
    try:
        host, port = authority()
    except ValueError as error:
        return fail(EXIT_UNREACHABLE, str(error))
    try:
        tls = context()
    except ValueError as error:
        return fail(EXIT_UNREACHABLE, str(error))
    try:
        received = send_request(tls, host, port)
    except Overlong as error:
        return fail(EXIT_UNREACHABLE, str(error))
    except (OSError, ssl.SSLError, ValueError):
        return fail(EXIT_UNREACHABLE, "could not establish liveness over verified TLS")
    try:
        status, body = status_and_body(received)
    except ValueError:
        return fail(EXIT_NOT_ALIVE, "the liveness answer was not a readable HTTP response")
    if status != 200:
        return fail(EXIT_NOT_ALIVE, f"the liveness answer was HTTP {status}")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return fail(EXIT_NOT_ALIVE, "the liveness answer was not a JSON document")
    if document != LIVE_BODY:
        return fail(EXIT_NOT_ALIVE, "the liveness answer was not the expected document")
    return EXIT_ALIVE


if __name__ == "__main__":
    sys.exit(main())
