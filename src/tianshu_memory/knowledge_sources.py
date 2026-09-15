"""Read explicit UTF-8 sources only; no crawler, execution, or asset mutation."""

import hashlib
import http.client
import ipaddress
import os
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
        before = os.fstat(stream.fileno())
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    current_target = file_path(project, locator)
    current = current_target.stat()

    def signature(value):
        # Windows CPython 3.12 stat/ fstat expose different ctime meanings.
        # Compare ctime only between observations from the same open handle.
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    require(
        current_target == target
        and signature(before) == signature(after) == signature(current)
        and before.st_ctime_ns == after.st_ctime_ns,
        "source_changed",
        409,
    )
    require(len(raw) <= MAX_BYTES, "source_too_large", 413)
    return raw, "text/html" if target.suffix.lower() in {".html", ".htm"} else "text/plain"


DIRECTORY_WALK_LIMIT = 2048


def refused_component(name):
    """The name rule shared by single-file locators and directory walks."""
    return (
        name.startswith(".")
        or name.casefold() in DENIED
        or any(word in name.casefold() for word in ("credential", "secret", "token"))
    )


def directory_path(project, directory):
    """Resolve one operator-registered scan directory inside the project root.

    The same component rules as a single-file locator apply, so a registered directory can
    never be the root itself, a hidden or denied directory, or a path that leaves the root
    through `..`, a drive letter, a symbolic link or a junction.
    """
    require(isinstance(directory, str) and bool(directory), "invalid_input", 400)
    relative = Path(directory)
    require(not relative.is_absolute() and not relative.drive and ":" not in directory)
    require(all(part not in {".", ".."} for part in relative.parts))
    root = Path(project["root"]).resolve(strict=True)
    target = (root / relative).resolve(strict=True)
    require(target.is_relative_to(root) and target != root and target.is_dir())
    for part in (*relative.parts, *target.relative_to(root).parts):
        require(not refused_component(part))
    return target


def _linked(entry):
    """Whether one directory entry is a symbolic link or junction, or cannot be told apart."""
    try:
        return entry.is_symlink() or bool(getattr(entry, "is_junction", lambda: False)())
    except OSError:
        return True


def _kind(entry):
    """`dir`, `file` or None for one directory entry, without following links."""
    try:
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        if entry.is_file(follow_symlinks=False):
            return "file"
    except OSError:
        return None
    return None


def walk_directory(project, directory, limit=DIRECTORY_WALK_LIMIT):
    """Deterministic, link-free listing of one registered directory subtree.

    Returns `(paths, state)` where `paths` holds the component tuples of every regular file
    reached, relative to the registered root and prefixed with the registered directory
    spelling, and `state` counts inspected entries, skipped links, refused directories and
    whether the bound stopped the walk. Links and junctions are never followed, denied
    directories are neither entered nor listed, and a truncated walk can never prove that an
    unseen path was removed.
    """
    base = directory_path(project, directory)
    paths, state = [], {"inspected": 0, "links": 0, "denied": 0, "truncated": False}

    def visit(current, parts):
        if state["truncated"]:
            return
        try:
            with os.scandir(current) as stream:
                children = sorted(stream, key=lambda item: item.name)
        except OSError:
            return
        for entry in children:
            if state["truncated"]:
                return
            state["inspected"] += 1
            if state["inspected"] > limit:
                state["truncated"] = True
                return
            if _linked(entry):
                state["links"] += 1
                continue
            kind = _kind(entry)
            if kind == "dir":
                if refused_component(entry.name):
                    state["denied"] += 1
                    continue
                visit(entry.path, (*parts, entry.name))
            elif kind == "file":
                paths.append((*parts, entry.name))

    visit(base, tuple(Path(directory).parts))
    return paths, state


def presence(project, directory, locator):
    """Whether one stored locator is still a scannable file inside a registered directory.

    `not_scannable` means the path resolves but only through a link or junction, which a
    directory walk refuses to follow; `not_present` means it no longer resolves as a file of
    a supported type inside the root. Both are the only states that can propose a tombstone.
    """
    base = directory_path(project, directory)
    try:
        target = file_path(project, locator)
    except (Fault, OSError, ValueError):
        return "not_present"
    if not target.is_relative_to(base):
        return "not_present"
    current = base
    for part in target.relative_to(base).parts:
        current = current / part
        try:
            if os.path.islink(current) or (
                hasattr(os.path, "isjunction") and os.path.isjunction(current)
            ):
                return "not_scannable"
        except OSError:
            return "not_present"
    return "present"


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
