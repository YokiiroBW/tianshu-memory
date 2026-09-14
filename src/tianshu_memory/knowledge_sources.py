"""Read explicit UTF-8 sources only; no crawler, execution, or asset mutation."""

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .domain import Fault, require

MAX_BYTES = 1_048_576
SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".html",
    ".htm",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".sql",
}
DENIED = {
    "node_modules",
    "venv",
    "__pycache__",
    "credentials",
    "secrets",
    "runtime",
    "models",
    "weights",
    "dist",
    "build",
}


def content_hash(raw):
    return hashlib.sha256(raw).hexdigest()


def file_path(project, locator):
    require(isinstance(locator, str) and bool(locator), "invalid_input", 400)
    relative = Path(locator)
    require(not relative.is_absolute() and not relative.drive and ":" not in locator)
    root = Path(project["root"]).resolve(strict=True)
    target = (root / relative).resolve(strict=True)
    require(target.is_relative_to(root) and target.is_file())
    for part in (*relative.parts, *target.relative_to(root).parts):
        require(not part.startswith(".") and part.casefold() not in DENIED)
        require(not any(word in part.casefold() for word in ("credential", "secret", "token")))
    require(target.suffix.lower() in SUFFIXES, "unsupported", 415)
    return target


def read_file(project, locator):
    target = file_path(project, locator)
    with target.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "source_too_large", 413)
    return raw, "text/html" if target.suffix.lower() in {".html", ".htm"} else "text/plain"


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def decode(raw, media_type):
    try:
        original = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Fault("unsupported_encoding", 415) from None
    require("\x00" not in original, "unsupported", 415)
    text = original
    if media_type == "text/html":
        parser = VisibleHTML()
        parser.feed(original)
        text = "".join(parser.parts).strip()
    require(bool(text.strip()), "empty_source", 400)
    return text


def fetch_url(url, allowed_urls):
    """Exact allowlist for every hop; public addresses pinned through TLS (no proxy).

    DNS resolution uses the host resolver; OS DNS timeout is additional to the 15s
    network deadline. A configured URL never authorizes its links or another redirect.
    """
    deadline = time.monotonic() + 15
    for _ in range(4):
        require(url in allowed_urls)
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and parsed.port in {None, 443}
        )
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        require(addresses and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses))
        remaining = deadline - time.monotonic()
        require(remaining > 0, "source_timeout", 408)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=remaining)
        try:
            # Pin the resolved address while retaining certificate/hostname verification.
            sock = socket.create_connection(addresses[0][4][:2], timeout=remaining)
            try:
                connection.sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parsed.hostname
                )
            except BaseException:
                sock.close()
                raise
            tls_socket = connection.sock

            def timeout():
                remaining = deadline - time.monotonic()
                require(remaining > 0, "source_timeout", 408)
                tls_socket.settimeout(remaining)

            timeout()
            connection.request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else "") or "/",
                headers={
                    "Accept": "text/plain, text/markdown, text/html",
                    "Accept-Encoding": "identity",
                },
            )
            timeout()
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                require(bool(response.getheader("Location")), "invalid_redirect", 400)
                url = urljoin(url, response.getheader("Location"))
                continue
            require(response.status == 200, "source_unavailable", 503)
            media = response.getheader("Content-Type", "").split(";")[0].lower().strip()
            require(media in {"text/plain", "text/markdown", "text/html"}, "unsupported", 415)
            require(
                response.getheader("Content-Encoding", "identity") == "identity", "unsupported", 415
            )
            length = response.getheader("Content-Length")
            require(
                length is None or (length.isdigit() and int(length) <= MAX_BYTES),
                "source_too_large",
                413,
            )
            chunks, size = [], 0
            while not response.isclosed():
                remaining = deadline - time.monotonic()
                require(remaining > 0, "source_timeout", 408)
                # read1 avoids an unbounded series of slow reads inside a single call.
                timeout()
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                require(size <= MAX_BYTES, "source_too_large", 413)
                chunks.append(chunk)
            require(length is None or size == int(length), "incomplete_source", 503)
            return b"".join(chunks), media, url
        finally:
            connection.close()
    raise Fault("redirect_limit", 400)
