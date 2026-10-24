"""Literal source quotation and sentence boundaries for sourced fact rules."""

import re
from bisect import bisect_left, bisect_right

PAIRS = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"', "'": "'", "`": "`"}
CONDITION = re.compile(
    r"如果|假如|假设|要是|倘若|除非|只要|只有|可能|也许|或许|万一|"
    r"\b(?:if|unless|suppose|perhaps|maybe|might)\b",
    re.I,
)
ATTRIBUTION = re.compile(
    r"(?:说|写道|写着|问|喊|提到|告诉我|转述|said|says|wrote|asked)\s*[:：]?\s*$", re.I
)
SENTENCE_END = set("。；;！!？?.\n\r")


def quoted_positions(text):
    stack, result = [], []
    escaped = False
    for index, ch in enumerate(text):
        result.append(bool(stack))
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch in PAIRS and not (
            ch == "'"
            and index
            and index + 1 < len(text)
            and text[index - 1].isalnum()
            and text[index + 1].isalnum()
        ):
            stack.append(PAIRS[ch])
    return result


def bound_contexts(proof):
    """Only sentences intersecting an exact quotation are claim context.

    Full source text is transaction-local matching context for typed proposals;
    it does not let another sentence or a model keyword supply missing evidence.
    """
    text = proof.get("context_text", proof.get("source_text", proof["quote"]))
    quote = proof["quote"]
    quoted = quoted_positions(text)
    boundaries = [i + 1 for i, ch in enumerate(text) if ch in SENTENCE_END and not quoted[i]]
    result = set()
    for match in re.finditer(re.escape(quote), text):
        start, end = match.span()
        before, after = bisect_right(boundaries, start), bisect_left(boundaries, end)
        left = boundaries[before - 1] if before else 0
        right = boundaries[after] if after < len(boundaries) else len(text)
        sentence = text[left:right]
        prefix = text[left:start].rstrip("，, ：:")
        safe = (
            not quoted[start] and not CONDITION.search(sentence) and not ATTRIBUTION.search(prefix)
        )
        # A whole quoted question is no positive action assertion either.
        safe = safe and not sentence.rstrip().endswith(("?", "？"))
        result.add((sentence, bool(safe)))
    return sorted(result)
