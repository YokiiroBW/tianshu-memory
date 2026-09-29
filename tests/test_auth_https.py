"""Real loopback TLS issuer + configured_app, with only disposable synthetic identities."""

import copy
import json
import os
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tianshu_memory.app import configured_app


@pytest.fixture(scope="session")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tls-certificates")
    result = subprocess.run(
        [
            os.environ.get("TIANSHU_TEST_CERT_PYTHON", sys.executable),
            str(Path(__file__).with_name("tls_certificates.py")),
            str(directory),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, (
        "TLS tests require a test tooling Python with cryptography; set "
        f"TIANSHU_TEST_CERT_PYTHON (see docs/runtime.md). {result.stderr}"
    )
    return directory


@pytest.fixture
def issuer(h, certificates, request):
    state = SimpleNamespace(
        context=copy.deepcopy(h.config["origins"]["origin-first"]),
        calls=[],
        status=200,
        response_request_id=None,
        location=None,
    )
    state.context.update(issuer="platform", assertion_ref="origin-private")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            h.contracts.validate("common#origin_resolve_request", body)
            state.calls.append((self.path, self.headers.get("Authorization"), body))
            status = state.status
            if self.headers.get("Authorization") != "Bearer test-only-memory-resolver-secret":
                status = 401
            response = (
                {
                    "schema_version": 1,
                    "request_id": state.response_request_id or body["request_id"],
                    "context": state.context,
                }
                if status == 200
                else {"private_upstream_detail": "must-not-escape"}
            )
            payload = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if state.location:
                self.send_header("Location", state.location)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    label = getattr(request, "param", "server")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / f"{label}.pem", certificates / f"{label}.key")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        state.url = f"https://127.0.0.1:{server.server_port}/internal/v1/origins/resolve"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.fixture
def runtime(h, issuer, certificates, monkeypatch):
    # Exercise deployment composition; never substitute Authenticator or SourceAuthority.
    h.config.update(mode="service", database_path=str(h.directory / "https-memory.sqlite"))
    h.config.pop("origins")
    caller = h.config["callers"]["companion"]
    caller.update(
        issuer="platform",
        issuer_url=issuer.url,
        issuer_token="test-only-memory-resolver-secret",
        issuer_ca_file=str(certificates / "ca.pem"),
    )
    caller["operations"].append("select_profiles")
    h.save_config()
    monkeypatch.setenv("TIANSHU_MEMORY_CONFIG", str(h.config_path))
    with TestClient(configured_app()) as client:
        client.headers["Authorization"] = "Bearer test-only-companion-secret"
        yield client


def register(runtime, h):
    return runtime.post(
        "/internal/v1/identity/register", json={"command": h.command(), "account": h.account}
    )


def rejected(response, status, h, runtime):
    assert response.status_code == status, response.text
    h.contracts.validate("common#error", response.json())
    assert response.json()["code"] == ("dependency_unavailable" if status == 503 else "forbidden")
    assert "must-not-escape" not in response.text and "secret" not in response.text
    with runtime.app.state.memory.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_configured_app_trusted_https_register_resolve_and_live_config(
    runtime, h, issuer, certificates, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", str(certificates / "wrong-ca.pem"))
    created = register(runtime, h)
    assert created.status_code == 200 and created.json()["created"] is True
    payload = {"query": h.query(), "account": h.account}
    resolved = runtime.post("/internal/v1/identity/resolve", json=payload)
    assert resolved.status_code == 200
    assert resolved.json()["person_id"] == created.json()["person_id"]
    assert resolved.json()["state"] == "found"
    assert issuer.calls[-1] == (
        "/internal/v1/origins/resolve",
        "Bearer test-only-memory-resolver-secret",
        {
            "schema_version": 1,
            "request_id": payload["query"]["request_id"],
            "assertion_ref": "origin-private",
        },
    )
    assert len(issuer.calls) == 2
    # Existing live caller configuration also rotates trust, with no insecure fallback.
    h.config["callers"]["companion"]["issuer_ca_file"] = str(certificates / "wrong-ca.pem")
    h.save_config()
    denied = runtime.post("/internal/v1/identity/resolve", json=payload)
    assert denied.status_code == 503 and len(issuer.calls) == 2
    h.config["callers"]["companion"]["issuer_ca_file"] = str(certificates / "ca.pem")
    h.save_config()
    assert runtime.post("/internal/v1/identity/resolve", json=payload).status_code == 200


@pytest.mark.parametrize("setting", ["default", "wrong", "missing", "malformed", "relative", False])
def test_ca_failures_do_not_send_credentials_or_write(
    runtime, h, issuer, certificates, monkeypatch, setting
):
    caller = h.config["callers"]["companion"]
    if setting == "default":
        caller.pop("issuer_ca_file")
        # Environment CA trust must not silently authorize this private test CA.
        monkeypatch.setenv("SSL_CERT_FILE", str(certificates / "ca.pem"))
        monkeypatch.setenv("SSL_CERT_DIR", str(certificates))
    else:
        malformed = h.directory / "malformed.pem"
        malformed.write_text("not a CA certificate", encoding="utf-8")
        caller["issuer_ca_file"] = {
            "wrong": str(certificates / "wrong-ca.pem"),
            "missing": str(h.directory / "missing.pem"),
            "malformed": str(malformed),
            "relative": "ca.pem",
            False: False,
        }[setting]
    h.save_config()
    rejected(register(runtime, h), 503, h, runtime)
    assert not issuer.calls


@pytest.mark.parametrize("issuer", ["wrong-host", "client-only", "expired"], indirect=True)
def test_tls_hostname_purpose_and_expiry_required(runtime, h, issuer):
    rejected(register(runtime, h), 503, h, runtime)
    assert not issuer.calls


def test_https_required_even_with_trusted_ca(runtime, h, issuer):
    h.config["callers"]["companion"]["issuer_url"] = issuer.url.replace("https:", "http:")
    h.save_config()
    rejected(register(runtime, h), 503, h, runtime)
    assert not issuer.calls


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_not_followed(runtime, h, issuer, status):
    issuer.status = status
    issuer.location = issuer.url + "/redirect-target"
    rejected(register(runtime, h), 503, h, runtime)
    assert len(issuer.calls) == 1 and issuer.calls[0][0] == "/internal/v1/origins/resolve"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_issuer_denial_and_unavailability(runtime, h, issuer, status):
    issuer.status = status
    rejected(register(runtime, h), 503 if status == 500 else 403, h, runtime)
    assert len(issuer.calls) == 1


def test_wrong_resolver_credential(runtime, h, issuer):
    h.config["callers"]["companion"]["issuer_token"] = "wrong-test-credential"
    h.save_config()
    rejected(register(runtime, h), 403, h, runtime)
    assert len(issuer.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "nonebot"),
        ("authenticated_service", "unapproved-caller"),
        ("audience_service", "companion"),
        ("assertion_ref", "unrelated-ref"),
        ("expires_at", "2000-01-01T00:00:00Z"),
        ("verified_account", {"namespace": "qq", "immutable_account_id": "10004"}),
        ("allowed_scope", {"actor_id": "other-actor"}),
    ],
)
def test_https_does_not_grant_context_authority(runtime, h, issuer, field, value):
    if field == "allowed_scope":
        issuer.context[field].update(value)
    else:
        issuer.context[field] = value
    rejected(register(runtime, h), 403, h, runtime)
    assert len(issuer.calls) == 1


def test_response_correlation_and_live_revocation(runtime, h, issuer):
    issuer.response_request_id = "different-request"
    rejected(register(runtime, h), 403, h, runtime)
    issuer.response_request_id = None
    assert register(runtime, h).status_code == 200
    # A real issuer rejects revoked references rather than emitting revoked=true (invalid v1 wire).
    issuer.status = 403
    denied = runtime.post(
        "/internal/v1/identity/resolve", json={"query": h.query(), "account": h.account}
    )
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    assert len(issuer.calls) == 3


def test_https_identity_success_does_not_enable_sources(runtime, h, issuer):
    assert register(runtime, h).status_code == 200
    assert runtime.app.state.memory.source_authority is None
    assert runtime.get("/health").status_code == 503
    revision = copy.deepcopy(h.examples["revise"])
    revision["command"] = h.command()
    profiles = {
        "query": h.query(),
        "requester_scope": h.private,
        "target": {"kind": "person", "person_id": h.person},
        "query_text": "coffee",
        "selection": ["interest"],
        "known_scope_version": None,
        "budget": {"tokens": 0, "bytes": 0},
    }
    for path, body in [
        ("memory/select", h.selection()),
        ("memory/select", h.selection(budget=0)),
        ("memory/profiles/select", profiles),
        ("memory/revise", revision),
        ("memory/turn-commits", h.event([h.source()])),
    ]:
        response = runtime.post("/internal/v1/" + path, json=body)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "dependency_unavailable"
    # Authenticated query/revision requests still re-resolve; turn events use service auth only.
    assert len(issuer.calls) == 5
    with runtime.app.state.memory.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
