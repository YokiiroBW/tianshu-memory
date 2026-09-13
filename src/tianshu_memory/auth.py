import hmac
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .domain import Fault, parse_time, require


class Authenticator:
    """Per-caller credentials, fixed issuer configuration, and live origin re-resolution."""

    def __init__(self, config_path, contracts, clock):
        self.path, self.contracts, self.clock = Path(config_path), contracts, clock

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
                with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                    response = client.post(
                        url,
                        headers={"Authorization": f"Bearer {credential}"},
                        json={
                            "schema_version": 1,
                            "request_id": request_id,
                            "assertion_ref": assertion_ref,
                        },
                    )
                if response.status_code in {401, 403, 404}:
                    raise Fault("forbidden", 403)
                if response.status_code != 200:
                    raise Fault("dependency_unavailable", 503)
                resolved = response.json()
                self.contracts.validate("common#origin_resolve_response", resolved)
                require(resolved["request_id"] == request_id)
                context = resolved["context"]
            except (httpx.HTTPError, ValueError):
                raise Fault("dependency_unavailable", 503) from None
        require(not context.get("revoked", True))
        self.contracts.validate("common#trusted_context", context)
        require(context["issuer"] == issuer and context["authenticated_service"] == service)
        require(
            context["audience_service"] == "memory" and context["assertion_ref"] == assertion_ref
        )
        require(not context["revoked"] and parse_time(context["expires_at"]) > self.clock())
        allowed = caller.get("allowed_actors", [])
        require(context["allowed_scope"]["actor_id"] in allowed)
        return context
