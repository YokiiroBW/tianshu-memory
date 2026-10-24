"""Transport reuse preserves live trust rotation and in-flight stream ownership."""

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_auth_https import certificates as certificates

from tianshu_memory.app import create_app
from tianshu_memory.source_http_pool import OwnerHttpPool


@pytest.fixture
def fake_clients(monkeypatch):
    made, trust = [], []

    class Client:
        def __init__(self, **options):
            self.options, self.closed = options, 0
            made.append(self)

        def close(self):
            self.closed += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def context(**options):
        trust.append(options)
        return options

    monkeypatch.setattr("tianshu_memory.source_http_pool.httpx.Client", Client)
    monkeypatch.setattr("tianshu_memory.source_http_pool.ssl.create_default_context", context)
    return SimpleNamespace(made=made, trust=trust)


def test_same_path_trust_rotation_uses_the_exact_bytes_read_once(
    tmp_path, monkeypatch, fake_clients
):
    ca = tmp_path / "owner-ca.pem"
    ca.write_bytes(b"first CA")
    reads, original = [], Path.read_bytes

    def replaced_after_read(path):
        value = original(path)
        if path == ca:
            reads.append(value)
            ca.write_bytes(b"second CA")
        return value

    monkeypatch.setattr(Path, "read_bytes", replaced_after_read)
    pool = OwnerHttpPool()
    try:
        with pool.client("core", "https://owner/facts", str(ca)) as first:
            assert first.options["verify"] == {"cadata": "first CA"}
        with pool.client("core", "https://owner/facts", str(ca)) as second:
            assert second is not first
            assert second.options["verify"] == {"cadata": "second CA"}
        assert reads == [b"first CA", b"second CA"]
        assert first.closed == 1
    finally:
        pool.close()


def test_inflight_generation_survives_rotation_and_idempotent_close(tmp_path, fake_clients):
    ca = tmp_path / "owner-ca.pem"
    ca.write_bytes(b"old CA")
    pool = OwnerHttpPool()
    with pool.client("issuer", "https://issuer/resolve", str(ca)) as first:
        ca.write_bytes(b"new CA")
        with pool.client("issuer", "https://issuer/resolve", str(ca)) as second:
            assert first is not second and first.closed == second.closed == 0
            pool.close()
            pool.close()
            assert first.closed == second.closed == 0
            with pytest.raises(OSError, match="closed"):
                with pool.client("issuer", "https://issuer/resolve", str(ca)):
                    pass
        assert second.closed == 1 and first.closed == 0
    assert first.closed == second.closed == 1


def test_bad_current_ca_cannot_fall_back_to_live_old_generation(tmp_path, fake_clients):
    ca = tmp_path / "owner-ca.pem"
    ca.write_bytes(b"old CA")
    pool = OwnerHttpPool()
    try:
        with pool.client("core", "https://owner/facts", str(ca)) as current:
            pass
        ca.unlink()
        with pytest.raises(OSError):
            with pool.client("core", "https://owner/facts", str(ca)):
                pytest.fail("Missing current CA must not reuse a previous client")
        assert len(fake_clients.made) == 1 and current.closed == 0
    finally:
        pool.close()


def test_system_trust_remains_one_shot_and_retires_an_explicit_pool(tmp_path, fake_clients):
    ca = tmp_path / "owner-ca.pem"
    ca.write_bytes(b"CA")
    pool = OwnerHttpPool()
    try:
        with pool.client("core", "https://owner/facts", str(ca)) as previous:
            pass
        with pool.client("core", "https://owner/facts") as first:
            assert first.options["verify"] is True and previous.closed == 1
        with pool.client("core", "https://owner/facts") as second:
            assert second is not first and first.closed == 1
        assert second.closed == 1
    finally:
        pool.close()


def test_empty_explicit_ca_never_loads_default_roots_or_reuses_old_pool(tmp_path, fake_clients):
    ca = tmp_path / "owner-ca.pem"
    ca.write_bytes(b"old CA")
    pool = OwnerHttpPool()
    try:
        with pool.client("core", "https://owner/facts", str(ca)):
            pass
        ca.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            with pool.client("core", "https://owner/facts", str(ca)):
                pytest.fail("An explicit empty CA must not select system roots")
        assert len(fake_clients.made) == len(fake_clients.trust) == 1
    finally:
        pool.close()


def test_app_lifespan_closes_auth_and_source_transport(h, monkeypatch):
    closed = []
    monkeypatch.setattr(h.auth, "close", lambda: closed.append("auth"))
    h.service.source_authority = SimpleNamespace(
        transport=SimpleNamespace(close=lambda: closed.append("sources"))
    )
    with TestClient(create_app(service=h.service, auth=h.auth)):
        assert closed == []
    assert closed == ["auth", "sources"]


def test_actual_http11_tls_connection_reused_with_fresh_request_headers(certificates, tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.headers.get("Authorization"), self.headers.get("Cookie")))
            payload = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if len(calls) == 1:
                self.send_header("Set-Cookie", "owner_state=first-origin; Path=/; Secure")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        accepted = 0

        def get_request(self):
            request = super().get_request()
            self.accepted += 1
            return request

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificates / "server.pem", certificates / "server.key")
    ca = tmp_path / "live-owner-ca.pem"
    ca.write_bytes((certificates / "ca.pem").read_bytes())
    pool = OwnerHttpPool()
    with Server(("127.0.0.1", 0), Handler) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        url = f"https://127.0.0.1:{server.server_port}/read"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            for token in ["first-synthetic-token", "rotated-synthetic-token"]:
                with pool.client("core", url, str(ca)) as client:
                    with client.stream(
                        "POST", url, headers={"Authorization": "Bearer " + token}, content=b"{}"
                    ) as response:
                        assert response.status_code == 200 and json.loads(response.read()) == {}
                    assert len(client.cookies.jar) == 0
            assert calls == [
                ("Bearer first-synthetic-token", None),
                ("Bearer rotated-synthetic-token", None),
            ]
            assert server.accepted == 1
            # Same path, newly replaced CA: an old accepted TLS connection may
            # not carry credentials under the now-invalid trust configuration.
            ca.write_bytes((certificates / "wrong-ca.pem").read_bytes())
            import httpx

            with pytest.raises(httpx.HTTPError):
                with pool.client("core", url, str(ca)) as client:
                    with client.stream(
                        "POST",
                        url,
                        headers={"Authorization": "Bearer must-not-arrive"},
                        content=b"{}",
                    ):
                        pytest.fail("Wrong replacement CA must fail before HTTP")
            assert len(calls) == 2
            ca.write_bytes((certificates / "ca.pem").read_bytes())
            with pool.client("core", url, str(ca)) as client:
                assert client.post(url, content=b"{}").status_code == 200
            assert server.accepted == 2
        finally:
            pool.close()
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
