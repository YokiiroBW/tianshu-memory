"""Connection reuse for the configured source owners and origin issuer only.

Callers still validate and read their complete live configuration for every
request. Only explicitly supplied CA bytes select a reusable connection pool;
responses, credentials and authorization decisions are never retained here.
"""

import hashlib
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class _RejectCookies(DefaultCookiePolicy):
    """Only sockets are shared; owner responses cannot create request state."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


@dataclass
class _Generation:
    key: tuple
    client: httpx.Client
    users: int = 0
    retired: bool = False


class OwnerHttpPool:
    def __init__(self, *, internal_clients=None, timeout=5):
        self._lock = threading.Lock()
        self._current = {}
        self._closed = False
        self.internal_clients = dict(internal_clients or {})
        self.timeout = timeout

    def _client(self, verify, *, transport=None):
        # Pass the stdlib jar itself: httpx retains this rejection policy.
        # Passing an httpx.Cookies wrapper would copy into a default-policy jar.
        options = {} if transport is None else {"transport": transport}
        return httpx.Client(
            verify=verify,
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            cookies=CookieJar(policy=_RejectCookies()),
            **options,
        )

    def _acquire(self, owner, url, ca_file, target):
        endpoint = urlsplit(url)
        port = endpoint.port
        if port is None:
            port = {"https": 443, "http": 80}.get(endpoint.scheme)
        origin = (endpoint.scheme, endpoint.hostname, port)
        factory = self.internal_clients.get(target)
        if factory is not None:
            key = (origin, "local", target)
        else:
            path = Path(ca_file)
            if not path.is_absolute():
                raise ValueError("Explicit owner CA must be absolute")
            # One trust generation uses exactly these bytes, never credentials.
            trust = path.read_bytes()
            if not trust:
                raise ValueError("Explicit owner CA must not be empty")
            key = (origin, str(path), hashlib.sha256(trust).digest())
        with self._lock:
            if self._closed:
                raise OSError("Owner transport is closed")
            current = self._current.get(owner)
            if current is not None and current.key == key:
                current.users += 1
                return current

        if factory is not None:
            client = self._client(True, transport=factory())
        else:
            try:
                certificates = trust.decode("ascii")
            except UnicodeDecodeError:
                certificates = trust
            client = self._client(ssl.create_default_context(cadata=certificates))
        candidate = _Generation(key, client)
        close = []
        with self._lock:
            if self._closed:
                close.append(candidate.client)
                selected = None
            else:
                current = self._current.get(owner)
                if current is not None and current.key == key:
                    selected = current
                    close.append(candidate.client)
                else:
                    if current is not None:
                        current.retired = True
                        if current.users == 0:
                            close.append(current.client)
                    self._current[owner] = selected = candidate
                selected.users += 1
        for client in close:
            client.close()
        if selected is None:
            raise OSError("Owner transport is closed")
        return selected

    @contextmanager
    def client(self, owner, url, ca_file=None, *, target=None):
        # A logical capability chooses an injected transport. URL, headers and
        # credentials never select a module. Unmapped peers keep real networking.
        target = owner if target is None else target
        if ca_file is None and target not in self.internal_clients:
            # Preserve the original one-shot system-trust behavior; do not cache
            # a system root store whose files are outside this configuration.
            with self._lock:
                if self._closed:
                    raise OSError("Owner transport is closed")
                previous = self._current.pop(owner, None)
                if previous is not None:
                    previous.retired = True
                close = previous is not None and previous.users == 0
            if close:
                previous.client.close()
            with self._client(True) as client:
                yield client
            return
        generation = self._acquire(owner, url, ca_file, target)
        try:
            yield generation.client
        finally:
            with self._lock:
                generation.users -= 1
                close = generation.retired and generation.users == 0
            if close:
                generation.client.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            current = list(self._current.values())
            self._current.clear()
            for generation in current:
                generation.retired = True
            close = [g.client for g in current if g.users == 0]
        # An active borrower closes its own retired generation on final release.
        # Never hold this mutex over socket shutdown or a response stream.
        for client in close:
            client.close()
