import hashlib
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime


def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


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


def source_selector(source, scope):
    key = source["message_key"]
    return {
        "key": {"channel": key["channel"], "message_id": key["message_id"]},
        "actor_id": scope["actor_id"],
    }


def admission_key(source, scope):
    return fingerprint(source_selector(source, scope))


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def invalid(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def terms(value: str) -> list[str]:
    """NFKC/casefold words and CJK bigrams; a single CJK character is not evidence."""
    value = unicodedata.normalize("NFKC", value).casefold()
    result = []
    for word in re.findall(r"[^\W_]+", value, re.UNICODE):
        # Split mixed-script words rather than joining a Latin identifier to adjacent CJK.
        for part in re.findall(r"[\u3400-\u9fff]+|[^\u3400-\u9fff]+", word):
            if re.fullmatch(r"[\u3400-\u9fff]+", part):
                if len(part) >= 2:
                    result.append(part)
                    result.extend(part[i : i + 2] for i in range(len(part) - 1))
            else:
                result.append(part)
    return list(dict.fromkeys(result))


# Time, question scaffolding and greetings alone cannot identify the topic of a memory.
# This is a conservative lexical gate, not Chinese segmentation or semantic understanding.
CONTEXT_TERMS = frozenset(
    "今天 明天 昨天 前天 后天 早上 上午 中午 下午 晚上 白天 夜里 最近 现在 时候 "
    "怎么样 什么 怎么 哪里 哪个 是否 可以 有吗 好吗 有没有 要不要 你好 您好 大家好".split()
)


def query_terms(value: str) -> list[str]:
    return [term for term in terms(value) if term not in CONTEXT_TERMS][:64]


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
