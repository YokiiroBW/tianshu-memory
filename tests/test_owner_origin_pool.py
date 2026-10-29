"""Real TLS sockets across owner endpoints and passive proof requests."""

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_auth_https import certificates as certificates
from test_source_http_pool import fake_clients as fake_clients

from tianshu_memory.app import create_app
from tianshu_memory.domain import Fault
from tianshu_memory.observations import PlatformVerifier
from tianshu_memory.source_http_pool import OwnerHttpPool


@pytest.fixture
def tls_owner(certificates):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.calls.append(
                (self.path, self.headers.get("Authorization"), self.headers.get("Cookie"), body)
            )
            payload = self.server.payload
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", "owner_cookie=must-not-survive; Path=/; Secure")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        accepted = 0

        def get_request(self):
            accepted = super().get_request()
            self.accepted += 1
            return accepted

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "server.pem", certificates / "server.key")
    with Server(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.calls, server.status, server.payload = [], 200, b'{"valid":true}'
        server.url = f"https://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_actual_tls_facts_window_facts_share_one_socket_with_fresh_headers(tls_owner, certificates):
    pool = OwnerHttpPool()
    paths = [
        "/internal/v1/source-facts/read",
        "/internal/v1/memory/consolidation/window/check",
        "/internal/v1/source-facts/read",
    ]
    try:
        for index, path in enumerate(paths):
            url = tls_owner.url + path
            with pool.client("core", url, str(certificates / "ca.pem")) as client:
                response = client.post(
                    url, json={"index": index}, headers={"Authorization": f"Bearer fresh-{index}"}
                )
                assert response.status_code == 200 and response.json() == {"valid": True}
        assert tls_owner.calls == [
            (path, f"Bearer fresh-{index}", None, {"index": index})
            for index, path in enumerate(paths)
        ]
        assert tls_owner.accepted == 1
    finally:
        pool.close()


@pytest.mark.parametrize("relative_ca", [False, True])
def test_actual_verifier_reuses_proof_socket_and_reloads_token_and_ca(
    tls_owner, certificates, tmp_path, monkeypatch, relative_ca
):
    ca = tmp_path / "live-owner-ca.pem"
    ca.write_bytes((certificates / "ca.pem").read_bytes())
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "verifier.json"
    config = dict(
        observation_source=dict(
            verify_url=tls_owner.url + "/internal/v2/observation-source/verify",
            verify_token="synthetic-first-token-long-enough",
            ca_file=ca.name if relative_ca else str(ca),
        )
    )
    path.write_text(json.dumps(config), encoding="utf-8")
    verifier = PlatformVerifier(path)
    try:
        assert verifier({"operation": "segment"}) == {"valid": True}
        config["observation_source"]["verify_token"] = "synthetic-rotated-token-long-enough"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert verifier({"operation": "segment_resolve"}) == {"valid": True}
        assert [(call[1], call[2], call[3]) for call in tls_owner.calls] == [
            ("Bearer synthetic-first-token-long-enough", None, {"operation": "segment"}),
            ("Bearer synthetic-rotated-token-long-enough", None, {"operation": "segment_resolve"}),
        ]
        assert tls_owner.accepted == 1
        ca.write_bytes((certificates / "wrong-ca.pem").read_bytes())
        with pytest.raises(Fault) as failed:
            verifier({"operation": "segment"})
        assert failed.value.code == "dependency_unavailable" and len(tls_owner.calls) == 2
        ca.write_bytes((certificates / "ca.pem").read_bytes())
        assert verifier({"operation": "segment"}) == {"valid": True}
        assert tls_owner.accepted == 2
    finally:
        verifier.close()


@pytest.mark.parametrize(
    "next_owner,next_url,same",
    [
        ("core", "https://OWNER:443/window?version=2", True),
        ("core", "https://owner:444/facts", False),
        ("core", "https://other/facts", False),
        ("core", "http://owner/facts", False),
        ("platform", "https://owner/facts", False),
    ],
)
def test_origin_and_owner_boundaries_retain_inflight_generation(
    tmp_path, fake_clients, next_owner, next_url, same
):
    ca = tmp_path / "ca.pem"
    ca.write_bytes(b"synthetic CA")
    pool = OwnerHttpPool()
    try:
        with pool.client("core", "https://owner/facts", str(ca)) as original:
            with pool.client(next_owner, next_url, str(ca)) as changed:
                assert (original is changed) == same
                assert original.closed == changed.closed == 0
            if next_owner == "core" and not same:
                assert changed.closed == 0 and original.closed == 0
        assert original.closed == (1 if next_owner == "core" and not same else 0)
    finally:
        pool.close()
    assert all(client.closed == 1 for client in fake_clients.made)


def test_new_ca_path_retires_same_origin_even_with_the_same_bytes(tmp_path, fake_clients):
    first, second = tmp_path / "first.pem", tmp_path / "second.pem"
    first.write_bytes(b"same synthetic CA")
    second.write_bytes(first.read_bytes())
    pool = OwnerHttpPool()
    try:
        with pool.client("core", "https://owner/facts", str(first)) as old:
            pass
        with pool.client("core", "https://owner/window", str(second)) as new:
            assert old is not new and old.closed == 1
    finally:
        pool.close()


@pytest.mark.parametrize("failing", ["auth", "sources", "observations"])
def test_app_closes_all_owner_transports_even_when_one_close_raises(h, monkeypatch, failing):
    calls = []

    def closer(name):
        def close():
            calls.append(name)
            if name == failing:
                raise RuntimeError("synthetic close failure")

        return close

    monkeypatch.setattr(h.auth, "close", closer("auth"))
    h.service.source_authority = SimpleNamespace(transport=SimpleNamespace(close=closer("sources")))
    h.service.observations = SimpleNamespace(verifier=SimpleNamespace(close=closer("observations")))
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        with TestClient(create_app(service=h.service, auth=h.auth)):
            pass
    assert calls == ["auth", "sources", "observations"]


@pytest.mark.parametrize(
    "status,payload,operation,code",
    [
        (409, b'{"code":"scope_changed"}', "segment", "scope_changed"),
        (403, b'{"code":"forbidden"}', "segment", "dependency_unavailable"),
        (503, b'{"valid":true}', "segment_resolve", "dependency_unavailable"),
        (200, b'{"valid":false}', "segment", "dependency_unavailable"),
        (200, b'{"valid":true,"valid":false}', "segment", "dependency_unavailable"),
        (200, b'{"valid":true,"padding":"' + b"x" * 4096 + b'"}', "read", "dependency_unavailable"),
    ],
)
def test_verifier_preserves_error_json_and_body_boundaries(
    tls_owner, certificates, tmp_path, status, payload, operation, code
):
    path = tmp_path / "verifier.json"
    path.write_text(
        json.dumps(
            dict(
                observation_source=dict(
                    verify_url=tls_owner.url + "/internal/v2/observation-source/verify",
                    verify_token="synthetic-token-long-enough",
                    ca_file=str(certificates / "ca.pem"),
                )
            )
        ),
        encoding="utf-8",
    )
    verifier = PlatformVerifier(path)
    tls_owner.status, tls_owner.payload = status, payload
    try:
        with pytest.raises(Fault) as rejected:
            verifier({"operation": operation})
        assert rejected.value.code == code
        assert rejected.value.status == (409 if code == "scope_changed" else 503)
        if payload != b'{"valid":true,"valid":false}':
            assert rejected.value.source_owner == "platform"
            assert rejected.value.source_http_status == status
    finally:
        verifier.close()


@pytest.mark.parametrize("invalid", ["url", "token", "missing_ca", "empty_ca"])
def test_verifier_reloads_and_rejects_bad_live_configuration_without_old_socket(
    tls_owner, certificates, tmp_path, invalid
):
    ca = tmp_path / "ca.pem"
    ca.write_bytes((certificates / "ca.pem").read_bytes())
    path = tmp_path / "verifier.json"
    config = dict(
        observation_source=dict(
            verify_url=tls_owner.url + "/internal/v2/observation-source/verify",
            verify_token="synthetic-token-long-enough",
            ca_file=str(ca),
        )
    )
    path.write_text(json.dumps(config), encoding="utf-8")
    verifier = PlatformVerifier(path)
    try:
        assert verifier({"operation": "segment"}) == {"valid": True}
        if invalid == "url":
            config["observation_source"]["verify_url"] = tls_owner.url + "/wrong-endpoint"
        elif invalid == "token":
            config["observation_source"]["verify_token"] = "short"
        elif invalid == "missing_ca":
            ca.unlink()
        else:
            ca.write_bytes(b"")
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(Fault) as rejected:
            verifier({"operation": "segment_resolve"})
        assert rejected.value.code == "dependency_unavailable" and len(tls_owner.calls) == 1
    finally:
        verifier.close()


@pytest.mark.parametrize("ca_value", [None, ""])
def test_verifier_system_trust_keeps_one_shot_clients(tmp_path, monkeypatch, ca_value):
    made = []

    class Client:
        def __init__(self):
            self.closed = 0
            made.append(self)

        def post(self, url, **kwargs):
            return SimpleNamespace(
                status_code=200,
                content=b'{"valid":true}',
                headers={"content-type": "application/json"},
            )

        def close(self):
            self.closed += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def factory(verify):
        assert verify is True
        return Client()

    monkeypatch.setattr(OwnerHttpPool, "_client", staticmethod(factory))
    path = tmp_path / "verifier.json"
    path.write_text(
        json.dumps(
            dict(
                observation_source=dict(
                    verify_url="https://owner/internal/v2/observation-source/verify",
                    verify_token="synthetic-token-long-enough",
                    ca_file=ca_value,
                )
            )
        ),
        encoding="utf-8",
    )
    verifier = PlatformVerifier(path)
    try:
        assert (
            verifier({"operation": "segment"})
            == verifier({"operation": "segment_resolve"})
            == {"valid": True}
        )
        assert len(made) == 2 and all(client.closed == 1 for client in made)
    finally:
        verifier.close()
