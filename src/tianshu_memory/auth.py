import errno
import hmac
import json
import ssl
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from jsonschema.exceptions import ValidationError

from .domain import Fault, OriginDenied, OriginUnavailable, parse_time, require


def interrupted_transport(error):
    """Do not confuse issuer configuration, TLS or protocol failures with absence."""
    timeout = isinstance(error, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout))
    if not timeout and not isinstance(error, (httpx.ReadError, httpx.WriteError)):
        return False
    seen, interrupted = set(), False
    cause = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            return False
        if isinstance(cause, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)) or (
            isinstance(cause, OSError)
            and cause.errno in {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ETIMEDOUT}
        ):
            interrupted = True
        cause = cause.__cause__ or cause.__context__
    return timeout or interrupted


class Authenticator:
    """Per-caller credentials, fixed issuer configuration, and live origin re-resolution."""

    def __init__(self, config_path, contracts, clock, *, role_database=None):
        self.path, self.contracts, self.clock = Path(config_path), contracts, clock
        config = self.config()
        grant_path = config.get("role_grants_database_path")
        if grant_path is not None:
            if not isinstance(grant_path, str) or not Path(grant_path).is_absolute():
                raise ValueError("role_grants_database_path must be absolute")
            from .role_grants import RoleGrants

            self.role_grants = RoleGrants(grant_path, database=role_database)
        else:
            self.role_grants = None

    def config(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def authenticate(self, header):
        if not header or not header.startswith("Bearer "):
            raise Fault("unauthorized", 401)
        token = header[7:]
        config = self.config()
        for name, caller in config.get("callers", {}).items():
            if caller.get("token") and hmac.compare_digest(token, caller["token"]):
                return name, caller
        raise Fault("unauthorized", 401)

    def _remote_context(
        self, service, issuer, caller, assertion_ref, request_id, allow_unavailable
    ):
        # Shared issuer configuration and trust are established before a pure
        # origin read. Their failures cannot be attributed to optional history.
        url, credential = caller.get("issuer_url"), caller.get("issuer_token")
        require(bool(url) and bool(credential), "dependency_unavailable", 503)
        require(urlparse(url).scheme == "https", "dependency_unavailable", 503)
        verify = True
        if "issuer_ca_file" in caller:
            ca_file = caller["issuer_ca_file"]
            require(
                isinstance(ca_file, str) and bool(ca_file) and Path(ca_file).is_absolute(),
                "dependency_unavailable",
                503,
            )
            try:
                verify = ssl.create_default_context(cafile=ca_file)
            except (OSError, ValueError):
                raise Fault("dependency_unavailable", 503) from None

        try:
            with httpx.Client(
                verify=verify, timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                deadline = time.monotonic() + 5
                with client.stream(
                    "POST",
                    url,
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Accept-Encoding": "identity",
                    },
                    json={
                        "schema_version": 1,
                        "request_id": request_id,
                        "assertion_ref": assertion_ref,
                    },
                ) as response:
                    if response.status_code in {403, 404} and allow_unavailable:
                        raise OriginUnavailable()
                    if response.status_code in {401, 403, 404}:
                        raise Fault("forbidden", 403)
                    require(response.status_code == 200, "dependency_unavailable", 503)
                    require(
                        response.headers.get("content-encoding", "identity").lower() == "identity",
                        "dependency_unavailable",
                        503,
                    )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        require(len(body) + len(chunk) <= 262144, "dependency_unavailable", 503)
                        if time.monotonic() >= deadline:
                            raise httpx.ReadTimeout("Origin response deadline elapsed")
                        body.extend(chunk)
                    if time.monotonic() >= deadline:
                        raise httpx.ReadTimeout("Origin response deadline elapsed")
        except (httpx.HTTPError, OSError, ValueError) as error:
            if allow_unavailable and interrupted_transport(error):
                raise OriginUnavailable() from None
            raise Fault("dependency_unavailable", 503) from None

        # Only a complete bounded successful body enters response proof checks.
        # A malformed response contributes no authority, coverage or lease.
        try:
            resolved = json.loads(body)
        except UnicodeDecodeError:
            raise Fault("dependency_unavailable", 503) from None
        except json.JSONDecodeError:
            if allow_unavailable:
                raise OriginUnavailable() from None
            raise Fault("dependency_unavailable", 503) from None
        try:
            self.contracts.validate("common#origin_resolve_response", resolved)
        except ValidationError:
            if allow_unavailable:
                raise OriginUnavailable() from None
            raise
        context = resolved["context"]
        bound = (
            resolved["request_id"] == request_id
            and context["issuer"] == issuer
            and context["authenticated_service"] == service
            and context["audience_service"] == "memory"
            and context["assertion_ref"] == assertion_ref
        )
        if not bound and allow_unavailable:
            raise OriginUnavailable()
        require(bound)
        return context

    def resolve(
        self,
        service,
        caller,
        assertion_ref,
        request_id,
        *,
        classify_denial=False,
        allow_unavailable=False,
    ):
        config = self.config()
        issuer = caller.get("issuer")
        if config.get("mode") == "source_sync":
            require(issuer == "platform", "dependency_unavailable", 503)
        if config.get("mode") == "local_fixture":
            context = config.get("origins", {}).get(assertion_ref)
            require(context is not None)
        else:
            context = self._remote_context(
                service, issuer, caller, assertion_ref, request_id, allow_unavailable
            )
        if not classify_denial:
            require(not context.get("revoked", True))
        self.contracts.validate("common#trusted_context", context)
        require(context["issuer"] == issuer and context["authenticated_service"] == service)
        require(
            context["audience_service"] == "memory" and context["assertion_ref"] == assertion_ref
        )
        # This is trusted context now. Local grants/storage failures remain
        # global; only explicit expiry or a complete actor decision is denial.
        current = not context["revoked"] and parse_time(context["expires_at"]) > self.clock()
        if not current and classify_denial:
            raise OriginDenied()
        require(current)
        allowed = caller.get("allowed_actors", [])
        actor_id = context["allowed_scope"]["actor_id"]
        decision = self.role_grants.decision(actor_id) if self.role_grants is not None else None
        permitted = decision is not False and (
            actor_id in allowed
            or caller.get("allow_runtime_roles") is True
            and self.role_grants is not None
            and decision is True
        )
        if not permitted and classify_denial:
            raise OriginDenied()
        require(permitted)
        return context
