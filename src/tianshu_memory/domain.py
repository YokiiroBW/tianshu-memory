import hashlib
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def now() -> datetime:
    return datetime.now(UTC)


def utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def semantic_request(request: dict) -> dict:
    return {k: v for k, v in request.items() if k not in {"command", "query"}}


def source_key(source: dict) -> str:
    key = source["message_key"]
    return fingerprint({"channel": key["channel"], "message_id": key["message_id"]})


def terms(value: str) -> list[str]:
    """NFKC/casefold Latin words and CJK unigrams/bigrams, never FTS query syntax."""
    value = unicodedata.normalize("NFKC", value).casefold()
    result = re.findall(r"[^\W_]+", value, re.UNICODE)
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        result.extend(run)
        result.extend(run[i : i + 2] for i in range(len(run) - 1))
    return list(dict.fromkeys(result))


class Fault(Exception):
    def __init__(self, code: str, status: int = 400, version: int | None = None):
        super().__init__(code)
        self.code, self.status, self.version = code, status, version

    def wire(self, request_id: str) -> dict:
        result = {
            "schema_version": 1,
            "request_id": request_id,
            "code": self.code,
            "execution_state": "not_started",
            "retryable": self.status in {408, 429, 503},
        }
        if self.version is not None:
            result["current_version"] = self.version
        return result


def require(condition, code="forbidden", status=403):
    if not condition:
        raise Fault(code, status)
