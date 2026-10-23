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
    from .recall_language import (
        clean_query,
        lexical_question_text,
        normalize_objects,
        query_aliases,
        topic_tokens,
    )

    value = normalize_objects(clean_query(value))

    scaffolding = {
        "我的",
        "你的",
        "我是",
        "我在",
        "用户",
        "自己",
        "记得",
        "记住",
        "知道",
        "告诉",
        "一下",
        "关于",
        "之前",
        "以前",
        "还是",
        "不是",
        "哪些",
        "几个",
        "给我",
        "是什么",
        "的事",
        "情况",
        "相关",
        "what",
        "where",
        "when",
        "who",
        "how",
        "my",
        "me",
        "i",
        "the",
        "a",
        "user",
        "is",
        "do",
        "does",
        "you",
        "remember",
        "like",
        "prefer",
        "usually",
        "normally",
        "currently",
    }
    topics = topic_tokens(value)
    if "topic10" in topics and not {"topic11", "topic12"}.intersection(topics):
        topics += ["topic11", "topic12"]
    if "topic4" in topics and not {"topic5", "topic6"}.intersection(topics):
        topics += ["topic5", "topic6", "topic20", "topic21"]
    if "topic19" in topics:
        # Query-side aliases also reach existing indexes without rewriting memory.
        topics += ["topic7", "topic8", "beverage", "drink", "饮料", "饮品"]
    aliases = query_aliases(value)
    if "topic2" in topics or "工作室" in value or "工作坊" in value:
        # Occupation already has a topic token; its raw bigram also occurs in studios.
        scaffolding.update({"工作", "work", "job"})
    if (topics or aliases) and not re.search(
        r"(我的|本人|我自己|本人的)(名字|姓名)|my name", value.casefold()
    ):
        # Naming a pet/company/topic is not a request for the speaker's own name.
        scaffolding.update({"名字", "姓名", "名称", "称呼", "name"})
    # Remove full context phrases before tokenization so adjacent pronouns/time words
    # cannot create shared bigrams across unrelated facts. Topic aliases use the original.
    context = (
        CONTEXT_TERMS
        | scaffolding
        | set(
            "平常 平时 通常 常用 一般来说 长期 短期 目前 当前 喜欢 喜爱 偏爱 "
            "已经 主要 一直 仍然 仍是 还会 还在 原来 曾经 那次 那时候 那只 这只 "
            "是我 是你 是他 是她 我也 我的 你的 他的 她的 本人".split()
        )
    )
    chinese = sorted((word for word in context if not word.isascii()), key=len, reverse=True)
    lexical_value = re.sub(
        r"(?:我|你|他|她)?(?:" + "|".join(map(re.escape, chinese)) + ")",
        " ",
        value,
    )
    lexical_value = lexical_question_text(lexical_value)
    return list(
        dict.fromkeys(
            [term for term in terms(lexical_value) if term not in CONTEXT_TERMS | scaffolding]
            + topics
            + terms(" ".join(aliases))
        )
    )[:64]


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


class OriginDenied(Fault):
    """A fully authenticated origin is definitively unusable for this read."""

    def __init__(self):
        super().__init__("forbidden", 403)


class OriginUnavailable(Fault):
    """A pure independent origin read could not supply a trusted context."""

    def __init__(self):
        super().__init__("dependency_unavailable", 503)


class DependencyChanged(Fault):
    """A validated historical expectation differs from the fresh authorized state."""

    def __init__(self, version=None):
        super().__init__("scope_changed", 409, version)


class SelectionChanged(Fault):
    """The current signed selection fails a specific fresh comparison."""

    def __init__(self):
        super().__init__("scope_changed", 409)


def require_dependency(condition):
    if not condition:
        raise DependencyChanged()


def require_selection(condition):
    if not condition:
        raise SelectionChanged()
