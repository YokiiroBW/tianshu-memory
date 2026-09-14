"""Real HTTPS bytes on loopback; only DNS/address pin seam targets the synthetic server."""

import socket
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from test_auth_https import certificates as certificates

from tianshu_memory.domain import Fault
from tianshu_memory.knowledge_sources import MAX_BYTES, fetch_url


@pytest.fixture
def source_server(certificates, monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            redirects = {"/redirect": "/ok", "/loop": "/loop", "/outside": "/unregistered"}
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.end_headers()
                return
            body = b"Only retry without a receipt. Never resend after success."
            if self.path == "/large":
                body = b"x" * (MAX_BYTES + 1)
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/pdf" if self.path == "/pdf" else "text/plain"
            )
            if self.path == "/compressed":
                self.send_header("Content-Encoding", "gzip")
            if self.path not in {"/large"}:
                self.send_header(
                    "Content-Length", str(len(body) + (10 if self.path == "/short" else 0))
                )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionError, ssl.SSLError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "server.pem", certificates / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context(cafile=str(certificates / "ca.pem"))
    monkeypatch.setattr(
        "tianshu_memory.knowledge_sources.ssl.create_default_context", lambda: client_context
    )
    monkeypatch.setattr(
        "tianshu_memory.knowledge_sources.socket.getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    def connect(address, timeout):
        assert address == ("93.184.216.34", 443)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(server.server_address)
        return sock

    monkeypatch.setattr("tianshu_memory.knowledge_sources.socket.create_connection", connect)
    try:
        yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_real_https_text_redirect_and_hostname(source_server):
    prefix = "https://127.0.0.1"
    raw, media, final = fetch_url(prefix + "/redirect", [prefix + "/redirect", prefix + "/ok"])
    assert b"Never resend" in raw and media == "text/plain" and final == prefix + "/ok"
    assert source_server == ["/redirect", "/ok"]
    with pytest.raises(ssl.SSLCertVerificationError):
        fetch_url("https://wrong-host.invalid/ok", ["https://wrong-host.invalid/ok"])


@pytest.mark.parametrize(
    "path,code",
    [
        ("/pdf", "unsupported"),
        ("/compressed", "unsupported"),
        ("/large", "source_too_large"),
        ("/short", "incomplete_source"),
        ("/loop", "redirect_limit"),
        ("/outside", "forbidden"),
    ],
)
def test_real_https_boundaries(source_server, path, code):
    url = "https://127.0.0.1" + path
    with pytest.raises(Fault, match=code):
        fetch_url(url, [url])
    assert "/unregistered" not in source_server


def test_network_deadline_before_read(source_server, monkeypatch):
    # Advance monotonic time at the read boundary without sleeping in the test.
    ticks = iter([0, 1, 2, 3, 16])
    monkeypatch.setattr("tianshu_memory.knowledge_sources.time.monotonic", lambda: next(ticks, 17))
    with pytest.raises(Fault, match="source_timeout"):
        fetch_url("https://127.0.0.1/ok", ["https://127.0.0.1/ok"])
