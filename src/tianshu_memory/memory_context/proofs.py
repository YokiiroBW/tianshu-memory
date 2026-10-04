"""Purpose-bound Platform attestations; service credentials never constitute user consent."""

import json
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema import ValidationError

from ..domain import Fault, fingerprint, parse_time, require, strict_json
from .migration import semantic_payload


class PlatformProofs:
    def __init__(self, auth, contracts, clock):
        self.auth, self.contracts, self.clock = auth, contracts, clock

    def verify(self, request, context, purpose):
        payload = dict(
            schema_version=1,
            request_id=request["command"]["request_id"],
            origin=request["command"]["origin"],
            proof_ref=request["proof_ref"],
            purpose=purpose,
            operation_digest=fingerprint(semantic_payload(request)),
        )
        self.contracts.validate("memory-context#proof_request", payload)
        config = self.auth.config().get("memory_context_proofs", {})
        require(isinstance(config, dict), "dependency_unavailable", 503)
        url, credential = config.get("url"), config.get("token")
        require(
            isinstance(url, str) and isinstance(credential, str) and len(credential) >= 24,
            "dependency_unavailable",
            503,
        )
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise Fault("dependency_unavailable", 503) from None
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path == "/internal/v1/memory-context/proof/verify"
            and not parsed.query
            and not parsed.fragment,
            "dependency_unavailable",
            503,
        )
        require(port is None or 0 < port <= 65535, "dependency_unavailable", 503)
        verify = True
        if config.get("ca_file") is not None:
            require(
                isinstance(config["ca_file"], str) and Path(config["ca_file"]).is_absolute(),
                "dependency_unavailable",
                503,
            )
            try:
                verify = ssl.create_default_context(cafile=config["ca_file"])
            except OSError:
                raise Fault("dependency_unavailable", 503) from None
        try:
            with httpx.Client(
                verify=verify, timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                deadline = time.monotonic() + 5
                with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + credential,
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code in {401, 403, 404, 409}:
                        raise Fault("forbidden", 403)
                    require(
                        response.status_code == 200
                        and response.headers.get("content-type", "").split(";", 1)[0]
                        == "application/json"
                        and response.headers.get("content-encoding", "identity") == "identity",
                        "dependency_unavailable",
                        503,
                    )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        require(
                            time.monotonic() < deadline and len(body) + len(chunk) <= 16384,
                            "dependency_unavailable",
                            503,
                        )
                        body.extend(chunk)
                    require(time.monotonic() < deadline, "dependency_unavailable", 503)
            proof = strict_json(body)
            self.contracts.validate("memory-context#proof_response", proof)
        except (httpx.HTTPError, OSError, ValueError, json.JSONDecodeError, ValidationError):
            raise Fault("dependency_unavailable", 503) from None
        require(
            all(
                proof[key] == payload[key]
                for key in ("request_id", "proof_ref", "purpose", "operation_digest")
            )
            and proof["valid"] is True
            and parse_time(proof["expires_at"]) > self.clock()
        )
        require(
            context["verified_account"] in proof["accounts"]
            and context["allowed_scope"] in proof["scopes"]
        )
        return proof
