"""The container recipe and its liveness probe.

There is no container runtime in this environment: `docker` is not installed, so **no build and no
run of this image has happened**, and nothing in this file claims otherwise. What it establishes is
what can be established without a daemon:

- the recipe states, as text, the properties an operator would otherwise have to trust: one image,
  a locked and hash-checked dependency install, a non-root account, no secret and no data in the
  context, and a healthcheck that cannot disable certificate verification;
- the healthcheck script itself runs here for real, against a real TLS server with a locally
  generated certificate, on a real socket — the alive path, the wrong-body path, the untrusted
  certificate path, the unannounced authority path and the size bound.

A green run of this file is not a passing build. `docs/deployment-runtime.md` records the exact
build and run commands, and the handoff records that they are unverified here.
"""

import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "container_healthcheck.py"
DOCKERFILE = ROOT / "infra" / "container" / "Dockerfile"
IGNORE_FILE = ROOT / "infra" / "container" / "Dockerfile.dockerignore"


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ts102-container-certificates")
    result = subprocess.run(
        [
            os.environ.get("TIANSHU_TEST_CERT_PYTHON", sys.executable),
            str(Path(__file__).with_name("tls_certificates.py")),
            str(directory),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, (
        "TLS tests require a test tooling Python with cryptography; set "
        f"TIANSHU_TEST_CERT_PYTHON (see docs/runtime.md). {result.stderr}"
    )
    return directory


class Liveness(http.server.BaseHTTPRequestHandler):
    """A real TLS server answering `/health/live`, so the probe is tested over a socket."""

    def do_GET(self):
        body = json.dumps(self.server.body).encode("utf-8")
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):  # noqa: ARG002 - the probe must not need a log
        return


def serve_tls(certificates, *, body, status=200, certificate="server.pem", key="server.key"):
    """Start a real HTTPS server on a free loopback port and return its port and a stop function."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Liveness)
    server.body = body
    server.status = status
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=str(certificates / certificate), keyfile=str(certificates / key)
    )
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    return server.server_address[1], stop


def run_probe(**environment):
    values = dict(os.environ)
    values.pop("TIANSHU_HEALTHCHECK_CA", None)
    values.update({key: str(value) for key, value in environment.items()})
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env=values,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def test_the_probe_script_is_importable_without_the_application():
    """The probe runs inside the image, where the application environment is what is installed.

    It uses only the standard library, so it cannot fail because of a dependency the service did
    not install, and it cannot import the service either: a probe that imported the application it
    is checking would be checking a second copy of it.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    for module in ("json", "os", "socket", "ssl", "sys"):
        assert f"import {module}" in source
    for forbidden in ("tianshu_memory", "requests", "urllib.request", "http.client", "curl"):
        assert forbidden not in source, forbidden
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
    assert "verify_mode" not in source


def test_the_probe_requires_an_announced_authority():
    result = run_probe(TIANSHU_HEALTHCHECK_HOST="")
    assert result.returncode == 2
    assert result.stderr.strip() == (
        "tianshu-healthcheck: TIANSHU_HEALTHCHECK_HOST must name the announced authority"
    )


@pytest.mark.parametrize(
    "authority",
    ["memory.example.test/path", "memory.example.test:not-a-port", "memory.example.test:0", "  "],
)
def test_the_probe_refuses_an_authority_it_cannot_honour(authority):
    result = run_probe(TIANSHU_HEALTHCHECK_HOST=authority)
    assert result.returncode == 2
    assert result.stdout == ""


def test_the_probe_reports_a_missing_trust_anchor_rather_than_skipping_verification(tmp_path):
    result = run_probe(
        TIANSHU_HEALTHCHECK_HOST="127.0.0.1",
        TIANSHU_HEALTHCHECK_CA=str(tmp_path / "absent-ca.pem"),
    )
    assert result.returncode == 2
    assert "trust anchor" in result.stderr


def test_an_alive_service_passes_the_probe(certificates):
    port, stop = serve_tls(certificates, body={"status": "alive"})
    try:
        result = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "ca.pem"),
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "body,status",
    [
        ({"status": "not_ready"}, 200),
        ({"status": "alive", "checks": {}}, 200),
        ({"status": "alive"}, 503),
        ({"detail": "internal"}, 200),
    ],
)
def test_anything_other_than_the_liveness_document_is_not_alive(certificates, body, status):
    port, stop = serve_tls(certificates, body=body, status=status)
    try:
        result = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "ca.pem"),
        )
    finally:
        stop()
    assert result.returncode == 1, result.stderr


def test_a_certificate_the_probe_cannot_verify_fails_the_probe(certificates):
    """The whole point of the probe's TLS: an unverifiable certificate is not liveness."""
    port, stop = serve_tls(certificates, body={"status": "alive"})
    try:
        untrusted = run_probe(TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}")
        wrong_anchor = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "wrong-ca.pem"),
        )
        wrong_name = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "ca.pem"),
        )
    finally:
        stop()
    assert untrusted.returncode == 2
    assert "verified TLS" in untrusted.stderr
    assert wrong_anchor.returncode == 2
    # The name is part of verification: the certificate names 127.0.0.1, and a probe to another
    # name must not accept it on the strength of the chain alone.
    assert wrong_name.returncode == 0


def test_a_server_naming_another_address_is_refused_by_name(certificates):
    port, stop = serve_tls(
        certificates,
        body={"status": "alive"},
        certificate="wrong-host.pem",
        key="wrong-host.key",
    )
    try:
        result = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "ca.pem"),
        )
    finally:
        stop()
    assert result.returncode == 2
    assert "verified TLS" in result.stderr


def test_a_closed_port_is_not_alive_and_not_a_reason_to_restart_a_container():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    result = run_probe(TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{closed_port}")
    assert result.returncode == 2
    assert result.stdout == ""


def test_the_probe_reads_a_bounded_response(certificates):
    """A service that answers with more than the probe will read cannot make it buffer forever."""
    port, stop = serve_tls(certificates, body={"status": "alive", "padding": "x" * 20000})
    try:
        result = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "ca.pem"),
        )
    finally:
        stop()
    assert result.returncode == 2
    assert "size" in result.stderr


def test_a_body_just_inside_the_bound_is_still_read_and_judged(certificates):
    """The bound is on bytes read, not on the shape of a legitimate answer."""
    port, stop = serve_tls(certificates, body={"status": "alive", "padding": "x" * 100})
    try:
        result = run_probe(
            TIANSHU_HEALTHCHECK_HOST=f"127.0.0.1:{port}",
            TIANSHU_HEALTHCHECK_CA=str(certificates / "ca.pem"),
        )
    finally:
        stop()
    assert result.returncode == 1
    assert "not the expected document" in result.stderr


def recipe():
    return DOCKERFILE.read_text(encoding="utf-8")


def instructions():
    """The Dockerfile's instructions, with comments and blank lines removed."""
    return [
        line.strip()
        for line in recipe().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_recipe_is_one_image_with_a_discarded_build_stage():
    stages = [line for line in instructions() if line.upper().startswith("FROM ")]
    assert len(stages) == 2
    assert stages[0].endswith("AS builder")
    assert stages[1].endswith("AS runtime")


def test_both_base_images_are_pinned_to_a_patch_release():
    for line in instructions():
        if line.upper().startswith("FROM "):
            reference = line.split()[1]
            assert ":" in reference, reference
            tag = reference.split(":", 1)[1]
            assert tag.count(".") >= 2, reference
            assert tag != "latest", reference
            assert "@sha256:" in reference
            assert len(reference.rsplit("sha256:", 1)[1]) == 64


def test_the_dependency_install_is_locked_and_hash_checked():
    # The actual installation lives in the same script exercised by local fresh-venv verification.
    assert (
        "RUN python infra/container/build_install.py --source /build --output /opt/tianshu"
        in instructions()
    )
    assert "COPY pyproject.toml uv.lock ./" in instructions()
    assert (
        "COPY infra/container/build-requirements.txt infra/container/build_install.py "
        "./infra/container/" in instructions()
    )


def test_the_runtime_image_installs_nothing_and_builds_nothing():
    runtime = instructions()
    stages = [index for index, line in enumerate(runtime) if line.upper().startswith("FROM ")]
    after = runtime[stages[1] :]
    body = " ".join(after)
    for forbidden in ("pip install", "apt-get", "apk add", "uv export", "uv sync", "make ", "gcc"):
        assert forbidden not in body, forbidden
    # Only the built environment and the probe script cross the stage boundary.
    copied = [line for line in after if line.upper().startswith("COPY ")]
    assert copied == [
        "COPY --from=builder /opt/tianshu/venv /opt/tianshu/venv",
        "COPY scripts/container_healthcheck.py /opt/tianshu/container_healthcheck.py",
    ]


def test_the_process_runs_as_a_non_root_account_without_a_home():
    body = " ".join(instructions())
    assert "useradd" in body
    assert "--no-create-home" in body
    assert "--home-dir /nonexistent" in body
    users = [line for line in instructions() if line.upper().startswith("USER ")]
    assert users == ["USER 10001:10001"]


def test_every_writable_path_is_created_and_owned_by_that_account():
    body = " ".join(instructions())
    assert "/var/log/tianshu" in body
    assert "/srv/tianshu" in body
    assert "--owner 10001 --group 10001" in body
    assert "TIANSHU_LOG_DIR=/var/log/tianshu" in body
    assert "WORKDIR /srv/tianshu" in instructions()


def test_the_healthcheck_is_the_bounded_verified_probe_and_not_a_shell_command():
    # The instruction continues onto a second line, so it is read as one joined body.
    healthchecks = [
        line.rstrip("\\").strip()
        for line in instructions()
        if line.upper().startswith("HEALTHCHECK")
    ]
    assert len(healthchecks) == 1
    healthcheck = healthchecks[0]
    assert "container_healthcheck.py" in healthcheck
    assert "CMD [" in healthcheck
    assert "curl" not in healthcheck
    for bound in ("--interval=", "--timeout=", "--start-period=", "--retries="):
        assert bound in healthcheck, bound
    # A read-only root filesystem is what makes the image's own paths immutable, so the probe and
    # the service must both be able to run without writing anywhere else.
    assert "--read-only" in recipe()


def test_the_image_bakes_in_no_secret_and_no_configuration_value():
    raw = recipe()
    for forbidden in ("TIANSHU_DIAGNOSTICS_TOKEN", "ARG TOKEN", "ARG SECRET", "ARG PASSWORD"):
        assert forbidden not in raw, forbidden
    # The environment values set here are paths and interpreter flags. A credential is always an
    # operator-supplied environment variable at run time, which is what the adapter reads, and the
    # one value that expands a shell variable is the activated interpreter path.
    assignments = []
    for line in instructions():
        if not line.upper().startswith("ENV "):
            continue
        for value in line[4:].rstrip("\\").split():
            name, separator, assigned = value.partition("=")
            assert separator, value
            assert name.isupper(), name
            assert assigned.strip(), name
            assignments.append((name, assigned))
    assert assignments
    for name, assigned in assignments:
        assert "$" not in assigned or (name, assigned) == (
            "PATH",
            '"/opt/tianshu/venv/bin:${PATH}"',
        ), (name, assigned)
    body = " ".join(instructions())
    assert "TIANSHU_LOG_DIR=/var/log/tianshu" in body
    assert "TIANSHU_MEMORY_CONFIG=/etc/tianshu/memory.json" in body


def test_the_entry_point_names_the_configuration_it_needs():
    commands = [line for line in instructions() if line.upper().startswith("CMD ")]
    # One command: the healthcheck's own `CMD` form belongs to that instruction, not to the image.
    assert len(commands) == 1
    assert commands[0].startswith('CMD ["tianshu-memory"')
    assert "--config" in commands[0]
    # `--config` belongs to the command and the runtime options to its `serve` subcommand, in that
    # order, which is the order the parser accepts.
    assert commands[0].index("--config") < commands[0].index("serve")


def test_the_ignore_file_keeps_the_build_inputs_and_excludes_state():
    body = IGNORE_FILE.read_text(encoding="utf-8")
    lines = [
        line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "**"
    for kept in ("!pyproject.toml", "!uv.lock", "!src/**"):
        assert kept in lines, kept
    for name in ("build_install.py", "build-requirements.txt"):
        assert lines.index(f"!infra/container/{name}") > lines.index("infra/**")
    for excluded in (
        ".git/**",
        ".runtime/**",
        ".venv/**",
        "tests/**",
        "docs/**",
        "*.sqlite",
        "*.pem",
        "*.key",
        ".env",
        "*credential*",
        "*secret*",
        "*token*",
        "source-guard.json",
    ):
        assert excluded in lines, excluded
    # A credential or a database anywhere in the context is excluded by name, and the file says so.
    assert "never contain a checkout, a runtime directory, a credential or a" in body


def test_the_build_command_uses_dockerfile_specific_ignore_discovery():
    """BuildKit discovers the adjacent <Dockerfile>.dockerignore automatically."""
    body = recipe()
    assert "--ignorefile" not in body
    assert "--file infra/container/Dockerfile" in body
    assert IGNORE_FILE == DOCKERFILE.with_name(DOCKERFILE.name + ".dockerignore")
