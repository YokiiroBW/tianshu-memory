"""Bounded HTTPS transport to configured owners; SQL and authority checks live elsewhere."""

import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema import ValidationError

from .domain import Fault, require, strict_json

MAX_BODY_BYTES = 1024 * 1024


class SourceTransport:
    """Read full configured endpoints and credentials anew for every owner request."""

    def __init__(self, config_path, contracts):
        self.config_path = Path(config_path)
        self.contracts = contracts

    def facts(self, request):
        return self._post("core", "/internal/v1/source-facts/read", "facts", request)

    def current(self, request):
        return self._post("platform", "/internal/v1/source-access/read", "current_access", request)

    def _post(self, owner, endpoint, schema, request):
        try:
            require(self.contracts.source_version == "1.0.0", "dependency_unavailable", 503)
            self.contracts.validate(f"sync-sources#{schema}_request", request)
            body = json.dumps(
                request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            require(len(body) <= MAX_BODY_BYTES, "dependency_unavailable", 503)
            config = strict_json(self.config_path.read_text(encoding="utf-8"))["source_sync"][owner]
            url, token = config["url"], config["token"]
            require(
                isinstance(url, str) and not any(c.isspace() or ord(c) < 32 for c in url),
                "dependency_unavailable",
                503,
            )
            parsed = urlsplit(url)
            require(
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path == endpoint
                and "?" not in url
                and "#" not in url
                and (parsed.port is None or 0 < parsed.port <= 65535),
                "dependency_unavailable",
                503,
            )
            require(
                isinstance(token, str) and bool(token) and all(33 <= ord(c) <= 126 for c in token),
                "dependency_unavailable",
                503,
            )
            verify = True
            if "ca_file" in config:
                ca_file = config["ca_file"]
                require(
                    isinstance(ca_file, str) and bool(ca_file) and Path(ca_file).is_absolute(),
                    "dependency_unavailable",
                    503,
                )
                verify = ssl.create_default_context(cafile=ca_file)
            with httpx.Client(
                verify=verify, timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                with client.stream(
                    "POST",
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    content=body,
                ) as response:
                    require(response.status_code == 200, "dependency_unavailable", 503)
                    require(
                        response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        == "application/json"
                        and response.headers.get("Content-Encoding", "identity").lower()
                        == "identity",
                        "dependency_unavailable",
                        503,
                    )
                    length = response.headers.get("Content-Length")
                    if length is not None:
                        require(
                            length.isascii()
                            and length.isdecimal()
                            and int(length) <= MAX_BODY_BYTES,
                            "dependency_unavailable",
                            503,
                        )
                    payload = bytearray()
                    for chunk in response.iter_raw(chunk_size=65536):
                        require(
                            len(payload) + len(chunk) <= MAX_BODY_BYTES,
                            "dependency_unavailable",
                            503,
                        )
                        payload.extend(chunk)
                    require(
                        length is None or len(payload) == int(length), "dependency_unavailable", 503
                    )
            resolved = strict_json(payload.decode("utf-8"))
            self.contracts.validate(f"sync-sources#{schema}_response", resolved)
            require(resolved["request_id"] == request["request_id"], "dependency_unavailable", 503)
            return resolved
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            ValidationError,
        ):
            raise Fault("dependency_unavailable", 503) from None
