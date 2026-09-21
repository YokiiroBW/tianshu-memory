"""The server runtime: explicit binding, no implicit TLS, two read-only probes, honest lifecycle.

The claim this file defends is that a deployment cannot become reachable, or become an authority to
someone, through a value nobody wrote down. Every bind address, certificate, key and authority is
given explicitly at startup, the two probe routes are the only routes this runtime adds, and each
terminal transition is recorded rather than assumed.
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tianshu_memory.diagnostics import CHAT_SERVICE, CHECKS_BY_SERVICE, Diagnostics
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.runtime_probes import ProbeConfig, readiness
from tianshu_memory.server_runtime import (
    DEFAULT_HOST,
    Assembly,
    add_serve_arguments,
    announce_startup_readiness,
    build_assembly,
    closed_document,
    config_path_from_environment,
    diagnostics_contract_path,
    install_networking,
    install_probe_routes,
    ip_literal,
    normalize_authority,
    resolve_binding,
    resolve_host,
    split_authority,
    tls_context,
    workspace_diagnostics_path,
)
from tianshu_memory.store import Store

PUBLIC_HOST = "192.0.2.10"
TOKEN = "synthetic-readiness-token"


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ts102-certificates")
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


def binding_arguments(**overrides):
    arguments = {
        "host": "127.0.0.1",
        "port": 8130,
        "certfile": None,
        "keyfile": None,
        "allowed_hosts": [],
    }
    arguments.update(overrides)
    return arguments


@pytest.fixture(scope="module")
def contracts():
    from tianshu_memory.contracts import Contracts

    root = Path(__file__).resolve().parents[1]
    context = json.loads((root / ".runtime/workspace-context.json").read_text(encoding="utf-8"))
    return Contracts(Path(context["workspace"]) / "contracts/text-dialogue/v1")


def log_bytes(directory):
    """The complete persisted log as bytes, across every segment this process wrote."""
    return {path.name: path.read_bytes() for path in sorted(Path(directory).glob("*.jsonl"))}


def event_names(directory):
    """Every event name in the log, in file order across segments."""
    names = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        names.extend(
            json.loads(line)["event"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return names


def test_the_default_binding_is_loopback_and_serves_only_its_own_names():
    binding = resolve_binding(**binding_arguments())
    assert binding.host == "127.0.0.1"
    assert DEFAULT_HOST == "127.0.0.1"
    assert binding.loopback is True
    assert binding.authority() == "127.0.0.1:8130"
    assert binding.accepts("127.0.0.1:8130")
    assert binding.accepts("localhost:8130")
    assert binding.tls is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "2001:db8::1"])
def test_a_bind_address_that_is_not_loopback_demands_tls_and_authorities(host, certificates):
    with pytest.raises(Fault):
        resolve_binding(**binding_arguments(host=host, port=8443))
    with pytest.raises(Fault):
        resolve_binding(**binding_arguments(host=host, port=8443, certfile=None, keyfile="k.pem"))
    with pytest.raises(Fault):
        resolve_binding(
            **binding_arguments(
                host=host,
                port=8443,
                certfile=str(certificates / "server.pem"),
                keyfile=str(certificates / "server.key"),
                allowed_hosts=[],
            )
        )


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_every_loopback_address_is_a_loopback_binding(host):
    """The whole loopback range, not one spelling of it: TLS stays optional on all of it."""
    binding = resolve_binding(**binding_arguments(host=host))
    assert binding.loopback is True
    assert binding.tls is None


def test_off_loopback_with_tls_and_named_authorities_is_accepted(certificates):
    binding = resolve_binding(
        **binding_arguments(
            host=PUBLIC_HOST,
            port=8443,
            certfile=str(certificates / "server.pem"),
            keyfile=str(certificates / "server.key"),
            allowed_hosts=["memory.example.test", f"{PUBLIC_HOST}:8443"],
        )
    )
    assert binding.loopback is False
    assert binding.tls == (str(certificates / "server.pem"), str(certificates / "server.key"))
    assert binding.accepts("memory.example.test:8443")
    assert binding.accepts(f"{PUBLIC_HOST}:8443")
    # Only the named authorities, on exactly this port.
    assert not binding.accepts(f"{PUBLIC_HOST}:8442")
    assert not binding.accepts("other.example.test:8443")
    assert not binding.accepts(f"{PUBLIC_HOST}")
    assert not binding.accepts(None)


def test_an_ipv6_binding_is_normalized_and_keeps_its_brackets(certificates):
    binding = resolve_binding(
        **binding_arguments(
            host="2001:0db8:0000:0000:0000:0000:0000:0001",
            port=8443,
            certfile=str(certificates / "server.pem"),
            keyfile=str(certificates / "server.key"),
            allowed_hosts=["[2001:db8::1]:8443"],
        )
    )
    assert binding.host == "2001:db8::1"
    assert binding.authority() == "[2001:db8::1]:8443"
    assert binding.accepts("[2001:db8::1]:8443")
    # An authority is compared as text, so the canonical form is what makes the comparison a rule
    # rather than a spelling: the long form of the same address names the same interface.
    assert binding.accepts("[2001:db8:0:0:0:0:0:1]:8443")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "localhost",
        "memory.example.test",
        "0.0.0.0/0",
        "127.0.0.1 ",
        "::1%eth0",
        "[::1",
        "1.2.3",
    ],
)
def test_a_bind_address_must_be_an_ip_literal(value):
    with pytest.raises(Fault):
        resolve_host(value)
    assert resolve_host("127.0.0.1") == "127.0.0.1"
    assert resolve_host("::1") == "::1"
    assert ip_literal("::1") == "::1"
    assert ip_literal("localhost") is None


@pytest.mark.parametrize(
    "value",
    [0, -1, 65536, 1.0, "8130", True, None],
)
def test_a_port_must_be_an_explicit_integer_in_range(value):
    with pytest.raises(Fault):
        resolve_binding(**binding_arguments(port=value))


@pytest.mark.parametrize(
    "value",
    ["", " http://memory.example.test", "memory.example.test/path", "*.example.test", "a..b", "-a"],
)
def test_an_unusable_authority_is_refused_rather_than_guessed(value):
    with pytest.raises(Fault):
        normalize_authority(value, 8443)


def test_an_authority_naming_another_port_describes_a_different_service():
    with pytest.raises(Fault):
        normalize_authority("memory.example.test:9999", 8443)
    assert normalize_authority("memory.example.test:8443", 8443) == "memory.example.test"
    assert normalize_authority("MEMORY.Example.Test", 8443) == "memory.example.test"


def test_the_authority_split_handles_every_shape_the_contract_allows():
    assert split_authority("memory.example.test:8443") == ("memory.example.test", "8443")
    assert split_authority("memory.example.test") == ("memory.example.test", "")
    assert split_authority("[2001:db8::1]:8443") == ("[2001:db8::1]", "8443")
    assert split_authority("[2001:db8::1]") == ("[2001:db8::1]", "")
    assert split_authority("2001:db8::1") == ("2001:db8::1", "")
    assert split_authority("[2001:db8::1") == ("[2001:db8::1", "")


def test_half_a_tls_configuration_is_not_a_tls_configuration(certificates):
    with pytest.raises(Fault):
        tls_context(str(certificates / "server.pem"), None)
    with pytest.raises(Fault):
        tls_context(None, str(certificates / "server.key"))
    with pytest.raises(Fault):
        tls_context(str(certificates / "absent.pem"), str(certificates / "absent.key"))
    # A file that is not a certificate, and a file that is not a key, are both refusals at startup
    # rather than a service that looks alive and then fails every handshake.
    (certificates / "not-a-certificate.pem").write_text("not a certificate\n", encoding="utf-8")
    (certificates / "not-a-key.pem").write_text("not a key\n", encoding="utf-8")
    with pytest.raises(Fault):
        tls_context(str(certificates / "not-a-certificate.pem"), str(certificates / "server.key"))
    with pytest.raises(Fault):
        tls_context(str(certificates / "server.pem"), str(certificates / "not-a-key.pem"))
    assert tls_context(None, None) is None


def test_a_real_certificate_and_key_load_into_a_server_context(certificates):
    context = tls_context(str(certificates / "server.pem"), str(certificates / "server.key"))
    assert context is not None
    assert context.minimum_version >= __import__("ssl").TLSVersion.TLSv1_2


def test_a_loopback_binding_keeps_the_free_host_behaviour_it_already_had():
    """Every existing entry point accepted any authority on loopback; this must not change."""
    app = FastAPI()
    install_networking(app, resolve_binding(**binding_arguments()))

    @app.get("/health")
    async def health():
        return JSONResponse({"state": "listening"})

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health", headers={"Host": "anything.example.test"}).status_code == 200


def test_a_loopback_binding_still_refuses_a_browser_origin():
    app = FastAPI()
    install_networking(app, resolve_binding(**binding_arguments()))

    @app.get("/health")
    async def health():
        return JSONResponse({"state": "listening"})

    with TestClient(app) as client:
        refused = client.get("/health", headers={"Origin": "https://evil.example.test"})
        prefetched = client.get("/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert refused.status_code == 400
    assert refused.json() == {"status": "failed", "code": "browser_origin_refused"}
    assert prefetched.status_code == 400


def test_an_off_loopback_binding_refuses_every_other_authority(certificates):
    app = off_loopback_app(certificates)
    assert call(app, [(b"host", b"memory.example.test:8443")]).status_code == 200
    assert call(app, [(b"host", b"192.0.2.10:8443")]).status_code == 200
    refused = call(app, [(b"host", b"evil.example.test:8443")])
    assert refused.status_code == 400
    assert refused.json() == {"status": "failed", "code": "invalid_host"}
    # The port is part of the authority: the same name on another port is another service, and the
    # bare name with no port at all is not the announced authority either.
    assert call(app, [(b"host", b"memory.example.test:8442")]).status_code == 400
    assert call(app, [(b"host", b"memory.example.test")]).status_code == 400
    # And a missing header is not an authority either.
    assert call(app, []).status_code == 400
    # A browser origin is refused outright off loopback: there is no CORS surface to serve one.
    browser = call(
        app,
        [
            (b"host", b"memory.example.test:8443"),
            (b"origin", b"https://app.example.test"),
        ],
    )
    assert browser.status_code == 400
    assert browser.json() == {"status": "failed", "code": "browser_origin_refused"}


def test_a_forwarded_header_is_never_consulted(certificates):
    """No trusted proxy is configured, so a forwarding header cannot make a host legal.

    The request is driven straight at the ASGI application so the `Host` header is exactly what this
    test says it is: an HTTP client library is free to rewrite it, and a test that let it would be
    checking the client rather than the authority rule.
    """
    app = off_loopback_app(certificates)
    for header in (b"x-forwarded-host", b"x-forwarded-server", b"forwarded", b"x-real-host"):
        allowed = call(
            app,
            [
                (b"host", b"memory.example.test:8443"),
                (header, b"evil.example.test"),
            ],
        )
        assert allowed.status_code == 200, header
    refused = call(
        app,
        [
            (b"host", b"evil.example.test:8443"),
            (b"x-forwarded-host", b"memory.example.test:8443"),
            (b"forwarded", b"host=memory.example.test:8443"),
        ],
    )
    assert refused.status_code == 400
    assert refused.json() == {"status": "failed", "code": "invalid_host"}


def off_loopback_app(certificates):
    app = FastAPI()
    install_networking(
        app,
        resolve_binding(
            **binding_arguments(
                host=PUBLIC_HOST,
                port=8443,
                certfile=str(certificates / "server.pem"),
                keyfile=str(certificates / "server.key"),
                allowed_hosts=["memory.example.test:8443", f"{PUBLIC_HOST}:8443"],
            )
        ),
    )

    @app.get("/health")
    async def health():
        return JSONResponse({"state": "listening"})

    return app


def call(app, headers):
    """One plain `GET /health` straight through the ASGI stack, with the headers given."""
    return TestClient(app).get("/health", headers=headers)


def test_the_serve_arguments_are_all_explicit_and_have_no_environment_fallback(monkeypatch):
    parser = argparse.ArgumentParser()
    add_serve_arguments(parser)
    parsed = parser.parse_args([])
    assert parsed.host == "127.0.0.1"
    assert parsed.tls_certfile is None and parsed.tls_keyfile is None
    assert parsed.allowed_host is None or parsed.allowed_host == []
    assert parsed.diagnostics_contract is None
    # A bind address can never arrive through the environment.
    monkeypatch.setenv("TIANSHU_HOST", PUBLIC_HOST)
    monkeypatch.setenv("TIANSHU_PORT", "8443")
    assert parser.parse_args([]).host == "127.0.0.1"


def test_the_serve_arguments_accept_a_repeated_authority_list():
    parser = argparse.ArgumentParser()
    add_serve_arguments(parser)
    parsed = parser.parse_args(
        [
            "--host",
            PUBLIC_HOST,
            "--tls-certfile",
            "cert.pem",
            "--tls-keyfile",
            "key.pem",
            "--allowed-host",
            "memory.example.test",
            "--allowed-host",
            f"{PUBLIC_HOST}:8443",
        ]
    )
    assert parsed.host == PUBLIC_HOST
    assert parsed.allowed_host == ["memory.example.test", f"{PUBLIC_HOST}:8443"]


def test_the_configuration_path_is_explicit_and_has_no_production_default(monkeypatch):
    monkeypatch.delenv("TIANSHU_MEMORY_CONFIG", raising=False)
    with pytest.raises(Fault):
        config_path_from_environment()
    assert config_path_from_environment("given.json") == "given.json"
    monkeypatch.setenv("TIANSHU_MEMORY_CONFIG", "from-environment.json")
    assert config_path_from_environment() == "from-environment.json"
    assert config_path_from_environment("given.json") == "given.json"


def test_the_contract_package_is_found_beside_the_contracts_or_named_outright(tmp_path):
    raw = {"contract_directory": str(tmp_path / "contracts" / "text-dialogue" / "v1")}
    resolved = diagnostics_contract_path(raw, tmp_path / "config.json")
    assert resolved == tmp_path / "contracts" / "diagnostics" / "v1"
    explicit = tmp_path / "elsewhere"
    assert diagnostics_contract_path(raw, tmp_path / "config.json", str(explicit)) == explicit


def test_a_configuration_with_no_contracts_named_resolves_beside_this_checkout(tmp_path):
    """The location is never guessed: it is read from this checkout's own workspace pointer.

    `tmp_path` lives under this checkout's `.runtime`, so the walk up to the checkout root finds the
    same `.runtime/workspace-context.json` the test fixtures use to locate the published contracts.
    A tree with no pointer at all reports no location, and the readiness check then reports the
    contract as not configured rather than pretending it verified a package it never found.
    """
    assert diagnostics_contract_path({}, tmp_path / "config.json") == (
        diagnostics_package_expected()
    )
    assert workspace_diagnostics_path(tmp_path / "config.json") == diagnostics_package_expected()
    absent = tmp_path / "no" / "pointer" / "config.json"
    assert workspace_diagnostics_path(absent) == diagnostics_package_expected()


def diagnostics_package_expected():
    root = Path(__file__).resolve().parents[1]
    context = json.loads((root / ".runtime/workspace-context.json").read_text(encoding="utf-8"))
    return Path(context["workspace"]) / "contracts" / "diagnostics" / "v1"


def test_the_workspace_pointer_is_the_same_one_the_fixtures_use():
    root = Path(__file__).resolve().parents[1]
    resolved = workspace_diagnostics_path(root / "pyproject.toml")
    context = json.loads((root / ".runtime/workspace-context.json").read_text(encoding="utf-8"))
    assert resolved == Path(context["workspace"]) / "contracts" / "diagnostics" / "v1"


def test_the_two_probe_routes_are_read_only_and_anonymous_only_for_liveness(tmp_path, monkeypatch):
    # The token is read when the adapter is built, because a process must not be able to acquire a
    # readiness credential later in its life.
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(tmp_path), "diagnostics": {"token_env": "TS102_READINESS"}},
    )
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=probe_settings)
    install_probe_routes(app, assembly)

    @app.post("/local/v1/memory/read")
    async def read():
        return JSONResponse({"status": "succeeded"})

    with TestClient(app) as client:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}
        assert "authorization" not in {key.lower() for key in live.request.headers}
        missing = client.get("/health/ready")
        assert missing.status_code == 401
        wrong = client.get("/health/ready", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401
        other_scheme = client.get("/health/ready", headers={"Authorization": f"Basic {TOKEN}"})
        assert other_scheme.status_code == 401
        # The token is the only credential involved, and a process that has not assembled its own
        # local prerequisites answers `not_ready` with 503 rather than pretending to be complete.
        not_ready = client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
        assert not_ready.status_code == 503
        assert set(not_ready.json()) == {"status", "service", "checks"}
        assert not_ready.json()["status"] == "not_ready"
        # And a business request is admitted: the two probes are not an admission gate.
        assert client.post("/local/v1/memory/read").status_code == 200
    # Neither probe wrote a line: the business request is the first record in the file.
    lines = [
        json.loads(line)
        for path in Path(tmp_path).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [line["event"] for line in lines] == ["request.started", "request.completed"]


def test_the_document_shape_is_closed_to_three_fields_on_every_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(tmp_path), "diagnostics": {"token_env": "TS102_READINESS"}},
    )
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=probe_settings)
    install_probe_routes(app, assembly)
    with TestClient(app) as client:
        unauthorised = client.get("/health/ready")
        authorised = client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
    for response in (unauthorised,):
        assert response.status_code == 401
        assert set(response.json()) == {"status", "service", "checks"}
        assert response.json()["status"] == "not_ready"
        assert set(response.json()["checks"]) == set(CHECKS_BY_SERVICE[CHAT_SERVICE])
    assert set(authorised.json()) == {"status", "service", "checks"}


def test_a_process_with_no_configured_token_cannot_authenticate_anyone(tmp_path, monkeypatch):
    """A readiness credential is configured or it is not: there is no default and no fallback."""
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=probe_settings)
    install_probe_routes(app, assembly)
    with TestClient(app) as client:
        configured = client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
        absent = client.get("/health/ready", headers={"Authorization": "Bearer "})
        anonymous = client.get("/health/ready")
    # This process was never given a readiness credential, so it cannot authenticate anyone at all:
    # 503 is that state, and it is reported as the same closed document as every other refusal.
    assert configured.status_code == 503
    assert set(configured.json()) == {"status", "service", "checks"}
    assert configured.json()["status"] == "not_ready"
    assert absent.status_code == 503
    assert anonymous.status_code == 503


def probe_settings(runtime):
    return ProbeConfig(
        service=CHAT_SERVICE,
        diagnostics=runtime.diagnostics,
        config_path=Path("absent.json"),
        contract_path=None,
        runtime=runtime,
    )


def ready_probe_settings(runtime):
    """The same settings, but over a process whose local prerequisites really are in place."""
    return ProbeConfig(
        service=CHAT_SERVICE,
        diagnostics=runtime.diagnostics,
        config_path=READY_CONFIG["path"],
        contract_path=diagnostics_package_expected(),
        runtime=runtime,
        handles=lambda: True,
    )


READY_CONFIG = {}


@pytest.fixture
def ready_process(tmp_path, contracts):
    """A genuinely ready chat process, so the 200 path is exercised and not only the 503 one."""
    store = Store(tmp_path / "memory.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    config = {
        "mode": "source_sync",
        "database_path": str(store.path),
        "contract_directory": str(contracts.directory),
        "source_sync": {"recovery_path": str(store.recovery_path)},
    }
    path = tmp_path / "chat.json"
    path.write_text(canonical(config), encoding="utf-8")
    READY_CONFIG["path"] = path
    return path


def test_a_ready_process_answers_200_with_the_closed_document(tmp_path, ready_process, monkeypatch):
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(tmp_path), "diagnostics": {"token_env": "TS102_READINESS"}},
    )
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=ready_probe_settings)
    assembly.started()
    install_probe_routes(app, assembly)
    before = log_bytes(tmp_path)
    with TestClient(app) as client:
        response = client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
        live = client.get("/health/live")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "service", "checks"}
    assert response.json()["status"] == "ready"
    assert response.json()["service"] == CHAT_SERVICE
    assert set(response.json()["checks"]) == set(CHECKS_BY_SERVICE[CHAT_SERVICE])
    assert live.status_code == 200
    # The probe asked a live process to describe itself and the log is byte-for-byte what it was:
    # no request line, no authentication line, no result line and no readiness transition. A ready
    # answer that *did* write would make the probe a writer, whatever the answer said.
    assert log_bytes(tmp_path) == before
    assert assembly.ready_announced is False
    assert event_names(tmp_path) == ["runtime.started"]


def test_the_lifecycle_records_readiness_once_and_the_probe_never_does(
    tmp_path, ready_process, monkeypatch
):
    """`runtime.ready` belongs to the startup path, which is the only thing that may append it."""
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(tmp_path), "diagnostics": {"token_env": "TS102_READINESS"}},
    )
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=ready_probe_settings)
    assembly.started()
    install_probe_routes(app, assembly)
    with TestClient(app) as client:
        for _ in range(3):
            client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
    assert event_names(tmp_path) == ["runtime.started"]
    document = readiness(assembly.probe_settings())
    assert assembly.announce_ready(document) is True
    assert event_names(tmp_path) == ["runtime.started", "runtime.ready"]
    # Recording it twice is not recording it twice, and a probe asked again still writes nothing.
    assert assembly.announce_ready(document) is False
    settled = log_bytes(tmp_path)
    with TestClient(app) as client:
        client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
        client.get("/health/live")
    assert log_bytes(tmp_path) == settled
    assert event_names(tmp_path) == ["runtime.started", "runtime.ready"]


def test_the_startup_lifecycle_is_what_announces_readiness_and_an_unready_process_is_silent(
    tmp_path, ready_process, monkeypatch
):
    """The real startup seam, not a stand-in: `_serve` records readiness, and only when it holds.

    A ready process appends `runtime.ready` once, from the lifecycle; a process whose prerequisites
    do not hold appends nothing, because a not-ready verdict already has its own startup record and
    a lifecycle line for a state this process never reached would be a false success. Each process
    writes to its own directory, as two real processes would.
    """
    monkeypatch.setenv("TS102_READINESS", TOKEN)

    def assemble(directory, settings):
        app = FastAPI()
        adapter = Diagnostics(
            CHAT_SERVICE,
            {"log_directory": str(directory), "diagnostics": {"token_env": "TS102_READINESS"}},
        )
        adapter.install(app)
        assembly = Assembly(app, adapter, None, probe_factory=settings)
        assembly.started()
        install_probe_routes(app, assembly)
        return assembly

    ready_directory = tmp_path / "ready"
    ready_directory.mkdir()
    ready = assemble(ready_directory, ready_probe_settings)
    asyncio.run(announce_startup_readiness(ready))
    assert event_names(ready_directory) == ["runtime.started", "runtime.ready"]
    # Asking again is not a second record, and neither probe writes on its own.
    before = log_bytes(ready_directory)
    asyncio.run(announce_startup_readiness(ready))
    with TestClient(ready.app) as client:
        client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
        client.get("/health/live")
    assert log_bytes(ready_directory) == before

    other_directory = tmp_path / "not-ready"
    other_directory.mkdir()
    not_ready = assemble(other_directory, probe_settings)
    asyncio.run(announce_startup_readiness(not_ready))
    # The verdict really was not ready, so the lifecycle said nothing at all: this process's log
    # holds the startup line and no readiness transition, and no probe grew it either.
    document = readiness(not_ready.probe_settings())
    assert document["status"] == "not_ready"
    assert not_ready.ready_announced is False
    with TestClient(not_ready.app) as client:
        client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"})
    assert event_names(other_directory) == ["runtime.started"]


def test_an_unauthorized_probe_writes_nothing_either(tmp_path, ready_process, monkeypatch):
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(tmp_path), "diagnostics": {"token_env": "TS102_READINESS"}},
    )
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=ready_probe_settings)
    assembly.started()
    install_probe_routes(app, assembly)
    before = log_bytes(tmp_path)
    with TestClient(app) as client:
        missing = client.get("/health/ready")
        wrong = client.get("/health/ready", headers={"Authorization": "Bearer not-the-token"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    # A refused probe is still a probe: an operator hammering a protected route unauthenticated
    # must not be able to grow the log, advance the sequence or fill the disk.
    assert log_bytes(tmp_path) == before


def test_both_probe_documents_are_no_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TS102_READINESS", TOKEN)
    app = FastAPI()
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(tmp_path), "diagnostics": {"token_env": "TS102_READINESS"}},
    )
    adapter.install(app)
    assembly = Assembly(app, adapter, None, probe_factory=probe_settings)
    install_probe_routes(app, assembly)
    with TestClient(app) as client:
        for response in (
            client.get("/health/live"),
            client.get("/health/ready"),
            client.get("/health/ready", headers={"Authorization": f"Bearer {TOKEN}"}),
        ):
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"


def test_the_closed_not_ready_document_names_every_check_a_service_reports():
    for service, keys in CHECKS_BY_SERVICE.items():
        document = closed_document(service)
        assert set(document) == {"status", "service", "checks"}
        assert document["status"] == "not_ready"
        assert document["service"] == service
        assert set(document["checks"]) == set(keys)
        assert set(document["checks"].values()) == {"failed"}


def test_the_deployment_binding_is_the_authority_the_knowledge_entry_also_uses(
    tmp_path, certificates
):
    """The reviewed defect, end to end: a legal off-loopback Host must reach the business route.

    Before this, the shared binding accepted `memory.example.test:8443` and the knowledge entry's
    own check refused the very same request with `invalid_host`: a deployment that was reachable as
    a probe and unusable as a service. The entry now consumes the binding's already-validated
    authority names, so the two checks are one decision made once.
    """
    from tianshu_memory.knowledge_http import ACTION_PATH, create_app

    config = {
        "database_path": str(tmp_path / "knowledge.sqlite"),
        "knowledge": {
            "clients": {"connector": {"permissions": ["query"], "projects": {"alpha": {}}}},
            "projects": {"alpha": {}},
        },
    }
    path = tmp_path / "knowledge.json"
    path.write_text(canonical(config), encoding="utf-8")
    binding = resolve_binding(
        **binding_arguments(
            host=PUBLIC_HOST,
            port=8443,
            certfile=str(certificates / "server.pem"),
            keyfile=str(certificates / "server.key"),
            allowed_hosts=["memory.example.test:8443", f"{PUBLIC_HOST}:8443"],
        )
    )
    assert binding.accepts("memory.example.test:8443")
    app = FastAPI()
    entry = create_app(str(path), "connector", 8443, authorities=binding.authority_names())
    install_networking(entry, binding)
    app.mount("/deployed", entry)

    def deployed(headers, method="post"):
        client = TestClient(app)
        if method == "post":
            return client.post(
                f"/deployed{ACTION_PATH}",
                json={"operation": "query", "project_id": "alpha", "arguments": {"text": "x"}},
                headers=dict(headers),
            )
        return client.get("/deployed/health", headers=dict(headers))

    authorized = [
        (b"host", b"memory.example.test:8443"),
        (b"authorization", b"Bearer synthetic-credential"),
    ]
    # The business route is reached: whatever the domain then decides, it is not this transport
    # refusing the authority. A 400 invalid_host here is exactly the reviewed defect.
    answered = deployed(authorized)
    assert answered.status_code != 400 or answered.json().get("code") != "invalid_host", (
        answered.status_code,
        answered.text[:200],
    )
    assert deployed(authorized, method="get").json() == {
        "state": "listening",
        "entrypoint": "project_knowledge_http",
        "projects": None,
    }
    # Everything the binding refused is still refused inside, because there is one rule, not two.
    for refused_host in (
        b"evil.example.test:8443",
        b"memory.example.test:8442",
        b"memory.example.test",
    ):
        refused = deployed([(b"host", refused_host), *authorized[1:]])
        assert refused.status_code == 400, refused_host
        assert refused.json() == {"status": "failed", "code": "invalid_host"}, refused_host
    # A browser origin is still refused, and a forwarded header still cannot name a legal host.
    for extra in (
        (b"origin", b"https://app.example.test"),
        (b"x-forwarded-host", b"memory.example.test"),
    ):
        refused = deployed([(b"host", b"evil.example.test:8443"), *authorized[1:], extra])
        assert refused.status_code == 400


def test_a_bare_knowledge_application_still_serves_only_loopback(tmp_path):
    """The loopback rule is the default, not a casualty of the deployment wiring above."""
    from tianshu_memory.knowledge_http import ACTION_PATH, create_app

    config = {
        "database_path": str(tmp_path / "knowledge.sqlite"),
        "knowledge": {
            "clients": {"connector": {"permissions": ["query"], "projects": {"alpha": {}}}},
            "projects": {"alpha": {}},
        },
    }
    path = tmp_path / "knowledge.json"
    path.write_text(canonical(config), encoding="utf-8")
    entry = create_app(str(path), "connector", 8130)
    client = TestClient(entry)
    for host in (b"memory.example.test:8130", b"evil.example.test:8130", b"192.0.2.10:8130"):
        refused = client.post(
            ACTION_PATH,
            json={"operation": "query", "project_id": "alpha", "arguments": {"text": "x"}},
            headers={"Host": host.decode(), "Authorization": "Bearer synthetic-credential"},
        )
        assert refused.status_code == 400, host
        assert refused.json() == {"status": "failed", "code": "invalid_host"}


def test_the_lifecycle_records_what_really_happened(tmp_path):
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    assembly = Assembly(FastAPI(), adapter, None)
    assert assembly.active is False
    assembly.started()
    assert assembly.active is True
    assembly.stopping()
    assert assembly.active is False
    assembly.stopped()
    assembly.stopped()
    names = [
        json.loads(line)["event"]
        for path in Path(tmp_path).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert names == ["runtime.started", "runtime.stopping", "runtime.stopped"]


def test_readiness_is_announced_once_and_only_while_serving(tmp_path):
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    assembly = Assembly(FastAPI(), adapter, None)
    ready = {"status": "ready", "service": CHAT_SERVICE, "checks": {}}
    not_ready = {"status": "not_ready", "service": CHAT_SERVICE, "checks": {}}
    # Before the socket is open, an operator's probe cannot make this process claim it is ready.
    assembly.announce_ready(ready)
    assembly.started()
    assembly.announce_ready(not_ready)
    for _ in range(5):
        assembly.announce_ready(ready)
    assembly.stopping()
    assembly.announce_ready(ready)
    names = [
        json.loads(line)["event"]
        for path in Path(tmp_path).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert names == ["runtime.started", "runtime.ready", "runtime.stopping"]


def test_assembling_without_a_probe_factory_is_a_refusal_not_a_technicality(tmp_path):
    assembly = Assembly(
        FastAPI(), Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)}), None
    )
    with pytest.raises(Fault):
        assembly.probe_settings()


def test_building_an_assembly_installs_both_probes_and_the_authority_check(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "mode": "source_sync",
                "database_path": str(tmp_path / "memory.sqlite"),
                "log_directory": str(tmp_path / "logs"),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir()
    assembly = build_assembly(
        CHAT_SERVICE,
        lambda: FastAPI(),
        config_path=config,
        contract_path=None,
        binding=resolve_binding(**binding_arguments()),
    )
    routes = {route.path for route in assembly.app.routes}
    assert "/health/live" in routes
    assert "/health/ready" in routes
    assert assembly.app.state.diagnostics is assembly.diagnostics
    assert assembly.state == "starting"
    assert assembly.diagnostics.durable is True


def test_a_failed_assembly_records_the_failure_before_it_dies(tmp_path):
    """A process that cannot assemble says so in its log rather than dying silently."""
    config = tmp_path / "config.json"
    logs = tmp_path / "logs"
    logs.mkdir()
    config.write_text(
        json.dumps(
            {
                "mode": "source_sync",
                "database_path": str(tmp_path / "memory.sqlite"),
                "log_directory": str(logs),
            }
        ),
        encoding="utf-8",
    )

    def explode():
        raise Fault("dependency_unavailable", 503)

    with pytest.raises(Fault):
        build_assembly(
            CHAT_SERVICE,
            explode,
            config_path=config,
            contract_path=None,
            binding=resolve_binding(**binding_arguments()),
        )
    names = [
        json.loads(line)["event"]
        for path in logs.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert names == ["runtime.starting", "runtime.start_failed"]


def test_an_unreadable_configuration_stops_the_process_before_the_socket_exists(tmp_path):
    with pytest.raises(Fault):
        build_assembly(
            CHAT_SERVICE,
            lambda: FastAPI(),
            config_path=tmp_path / "absent.json",
            contract_path=None,
            binding=resolve_binding(**binding_arguments()),
        )
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")
    with pytest.raises(Fault):
        build_assembly(
            CHAT_SERVICE,
            lambda: FastAPI(),
            config_path=config,
            contract_path=None,
            binding=resolve_binding(**binding_arguments()),
        )


def test_a_configuration_that_cannot_be_honoured_stops_the_process(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "mode": "source_sync",
                "database_path": str(tmp_path / "memory.sqlite"),
                "log_directory": "relative/logs",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Fault):
        build_assembly(
            CHAT_SERVICE,
            lambda: FastAPI(),
            config_path=config,
            contract_path=None,
            binding=resolve_binding(**binding_arguments()),
        )


def test_this_processes_own_signals_are_what_stop_it():
    """No shared shutdown channel, no operator HTTP route: the process stops on its own signals."""
    from tianshu_memory.server_runtime import _install_signal_handlers

    server = SimpleNamespace(should_exit=False)
    handled = _install_signal_handlers(server)
    assert signal.SIGINT in handled
    if hasattr(signal, "SIGTERM"):
        assert signal.SIGTERM in handled


def test_the_cli_exposes_the_deployment_serve_command_with_its_own_defaults(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"database_path": str(tmp_path / "memory.sqlite")}), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-m", "tianshu_memory.cli", "--config", str(config), "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode == 0, result.stderr
    for option in ("--host", "--tls-certfile", "--tls-keyfile", "--allowed-host", "--port"):
        assert option in result.stdout, option
    # The default bind address is not in the usage line, so it is asserted where it is decided: on
    # the parser itself, through the same call the command makes.
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    add_serve_arguments(sub.add_parser("serve"))
    assert parser.parse_args(["--config", "c.json", "serve"]).host == "127.0.0.1"


def test_the_configuration_is_a_required_argument_of_every_command():
    result = subprocess.run(
        [sys.executable, "-m", "tianshu_memory.cli", "serve"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode != 0
    assert "--config" in result.stderr
