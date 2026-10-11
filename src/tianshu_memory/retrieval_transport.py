"""Bounded calls to the Platform-owned retrieval model configuration and executor."""

import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .domain import Fault, require, strict_json


class RetrievalTransport:
    def __init__(self, config_path):
        self.path = Path(config_path)

    def call(self, operation, payload):
        try:
            config = strict_json(self.path.read_text(encoding="utf-8"))["retrieval"]
            url = config["url"]
            parsed = urlsplit(url)
            require(
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment,
                "dependency_unavailable",
                503,
            )
            token = config["token"]
            require(
                isinstance(token, str) and token and all(33 <= ord(c) <= 126 for c in token),
                "dependency_unavailable",
                503,
            )
            verify = (
                ssl.create_default_context(cafile=config["ca_file"])
                if config.get("ca_file")
                else True
            )
            data = json.dumps(
                {"operation": operation, **payload}, ensure_ascii=False, allow_nan=False
            ).encode()
            require(len(data) <= 1024 * 1024, "budget_exceeded", 429)
            with httpx.Client(
                verify=verify, timeout=8, trust_env=False, follow_redirects=False
            ) as client:
                with client.stream(
                    "POST",
                    url,
                    content=data,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    require(response.status_code == 200, "dependency_unavailable", 503)
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        require(len(raw) <= 8 * 1024 * 1024, "dependency_unavailable", 503)
                    answer = strict_json(raw)
                    require(isinstance(answer, dict), "dependency_unavailable", 503)
                    return answer
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            raise Fault("dependency_unavailable", 503) from None
