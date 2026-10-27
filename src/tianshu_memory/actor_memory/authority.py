"""Exact actor/source proofs from configured owners, outside local transactions."""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema.exceptions import ValidationError

from ..domain import Fault, canonical, require
from ..source_http_pool import OwnerHttpPool


class Authority:
    def __init__(self, config_path, contracts, *, internal_clients=None):
        self.path, self.contracts = Path(config_path), contracts
        self.pool = OwnerHttpPool(internal_clients=internal_clients)

    def _call(self, owner, definition, payload):
        try:
            config = json.loads(self.path.read_bytes())["actor_memory"]["authorities"]
            entry = (
                config["actor_check"] if owner == "actor_check" else config["producers"].get(owner)
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise Fault("dependency_unavailable", 503) from None
        require(isinstance(entry, dict), "unsupported_source", 503)
        endpoint = "url" if owner == "actor_check" else payload["operation"] + "_url"
        url, token, ca = entry.get(endpoint), entry.get("token"), entry.get("ca_file")
        try:
            parsed = urlsplit(url)
            valid = (
                isinstance(url, str)
                and not any(character.isspace() or ord(character) < 32 for character in url)
                and parsed.scheme == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
            parsed.port
        except (TypeError, ValueError):
            valid = False
        require(
            valid and isinstance(token, str) and token and all(33 <= ord(c) <= 126 for c in token),
            "dependency_unavailable",
            503,
        )
        require(
            ca is None or isinstance(ca, str) and Path(ca).is_absolute(),
            "dependency_unavailable",
            503,
        )
        self.contracts.validate(definition + "_request", payload)
        try:
            target = "platform" if owner == "actor_check" else owner
            with self.pool.client("actor-memory:" + owner, url, ca, target=target) as client:
                deadline = time.monotonic() + 5
                with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise Fault("forbidden", 403)
                    require(response.status_code == 200, "dependency_unavailable", 503)
                    require(
                        response.headers.get("content-encoding", "identity").lower() == "identity",
                        "dependency_unavailable",
                        503,
                    )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        require(
                            time.monotonic() < deadline and len(body) + len(chunk) <= 1048576,
                            "dependency_unavailable",
                            503,
                        )
                        body.extend(chunk)
                    require(time.monotonic() < deadline, "dependency_unavailable", 503)
            from ..domain import strict_json

            result = strict_json(body)
            self.contracts.validate(definition + "_result", result)
        except (httpx.HTTPError, OSError, ValueError, ValidationError):
            raise Fault("dependency_unavailable", 503) from None
        except Fault as error:
            if error.code == "invalid_input":
                raise Fault("dependency_unavailable", 503) from None
            raise
        require(
            result["request_id"] == payload["request_id"]
            and result["actor_id"] == payload["actor_id"],
            "dependency_unavailable",
            503,
        )
        return result

    def actor(self, request):
        payload = {
            key: request[key]
            for key in ("schema_version", "request_id", "actor_id", "role_version")
        }
        payload.update(operation="actor_check", requested_operation=request["operation"])
        result = self._call("actor_check", "actor_check", payload)
        require(
            result["state"] == "enabled"
            and result["role_version"] == request["role_version"]
            and request["operation"] in result["allowed_operations"],
            "forbidden",
            403,
        )
        return result

    def sources(self, request, refs, *, facts=False, context=None):
        operation = "source_facts" if facts else "source_check"
        grouped = {}
        for ref in refs:
            grouped.setdefault(ref["producer"], []).append(ref)
        results = {}
        for owner, selected in grouped.items():
            for offset in range(0, len(selected), 64):
                page = selected[offset : offset + 64]
                payload = {
                    key: request[key]
                    for key in ("schema_version", "request_id", "actor_id", "role_version")
                }
                payload.update(operation=operation, refs=page)
                if not facts:
                    payload["context"] = context
                result = self._call(owner, operation, payload)
                rows = result["facts" if facts else "items"]
                expected = {canonical(ref) for ref in page}
                actual = [canonical(row["source"]) for row in rows]
                require(
                    len(actual) == len(expected) and set(actual) == expected,
                    "dependency_unavailable",
                    503,
                )
                for row in rows:
                    if facts:
                        require(
                            row["actor_id"] == request["actor_id"], "dependency_unavailable", 503
                        )
                    results[canonical(row["source"])] = row
        return results

    def context(self, request, context):
        if context["audience"]["kind"] == "actor_only":
            return None
        payload = {
            key: request[key]
            for key in ("schema_version", "request_id", "actor_id", "role_version")
        }
        payload.update(operation="context_check", context=context)
        result = self._call("actor_check", "context_check", payload)
        require(
            result["role_version"] == request["role_version"]
            and result["context"] == context
            and result["allowed"],
            "forbidden",
            403,
        )
        return result

    def close(self):
        self.pool.close()
