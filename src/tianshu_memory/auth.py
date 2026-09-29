import hmac
import json
import ssl
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .domain import Fault, parse_time, require


class Authenticator:
    """Per-caller credentials, fixed issuer configuration, and live origin re-resolution."""

    def __init__(self, config_path, contracts, clock):
        self.path, self.contracts, self.clock = Path(config_path), contracts, clock
        config = self.config()
        grant_path = config.get("role_grants_database_path")
        if grant_path is not None:
            if not isinstance(grant_path, str) or not Path(grant_path).is_absolute():
                raise ValueError("role_grants_database_path must be absolute")
            from .role_grants import RoleGrants

            self.role_grants = RoleGrants(grant_path)
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

    def resolve(self, service, caller, assertion_ref, request_id):
        config = self.config()
        issuer = caller.get("issuer")
        if config.get("mode") == "source_sync":
            require(issuer == "platform", "dependency_unavailable", 503)
        if config.get("mode") == "local_fixture":
            context = config.get("origins", {}).get(assertion_ref)
            require(context is not None)
        else:
            url = caller.get("issuer_url")
            credential = caller.get("issuer_token")
            if not url or not credential:
                raise Fault("dependency_unavailable", 503)
            # Full endpoint is local deployment configuration, never user payload.
            require(urlparse(url).scheme == "https", "dependency_unavailable", 503)
            try:
                verify = True
                if "issuer_ca_file" in caller:
                    ca_file = caller["issuer_ca_file"]
                    require(
                        isinstance(ca_file, str) and bool(ca_file) and Path(ca_file).is_absolute(),
                        "dependency_unavailable",
                        503,
                    )
                    # Private deployment trust roots, never a request field or TLS bypass.
                    verify = ssl.create_default_context(cafile=ca_file)
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
                        if response.status_code in {401, 403, 404}:
                            raise Fault("forbidden", 403)
                        require(response.status_code == 200, "dependency_unavailable", 503)
                        require(
                            response.headers.get("content-encoding", "identity").lower()
                            == "identity",
                            "dependency_unavailable",
                            503,
                        )
                        body = bytearray()
                        # Observe every transport chunk: coalescing to a fixed size
                        # could hide a peer dripping bytes beneath the read timeout.
                        for chunk in response.iter_bytes():
                            require(
                                time.monotonic() < deadline and len(body) + len(chunk) <= 262144,
                                "dependency_unavailable",
                                503,
                            )
                            body.extend(chunk)
                        require(time.monotonic() < deadline, "dependency_unavailable", 503)
                resolved = json.loads(body)
                self.contracts.validate("common#origin_resolve_response", resolved)
                require(resolved["request_id"] == request_id)
                context = resolved["context"]
            except (httpx.HTTPError, OSError, ValueError):
                raise Fault("dependency_unavailable", 503) from None
        require(not context.get("revoked", True))
        self.contracts.validate("common#trusted_context", context)
        require(context["issuer"] == issuer and context["authenticated_service"] == service)
        require(
            context["audience_service"] == "memory" and context["assertion_ref"] == assertion_ref
        )
        require(not context["revoked"] and parse_time(context["expires_at"]) > self.clock())
        allowed = caller.get("allowed_actors", [])
        actor_id = context["allowed_scope"]["actor_id"]
        decision = self.role_grants.decision(actor_id) if self.role_grants is not None else None
        require(
            decision is not False
            and (actor_id in allowed
            or caller.get("allow_runtime_roles") is True
            and self.role_grants is not None
            and decision is True)
        )
        return context
