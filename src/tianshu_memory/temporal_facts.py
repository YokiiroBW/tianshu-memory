"""Keep observation timestamps separate from source-supported effective dates."""

import re
from datetime import timedelta

from .domain import parse_time

DAY_OFFSETS = {
    "前天": -2,
    "昨天": -1,
    "今天": 0,
    "明天": 1,
    "后天": 2,
    "yesterday": -1,
    "today": 0,
    "tomorrow": 1,
}


def supported_date(value, quote, sent_at):
    """Conservative calendar anchor, not an inference that a timestamp is consent."""
    day = parse_time(value).date()
    patterns = (
        rf"(?<!\d){day.year}[-/]{day.month:02d}[-/]{day.day:02d}(?!\d)",
        rf"(?<!\d){day.year}年0?{day.month}月0?{day.day}[日号]",
        rf"(?<![\d年])0?{day.month}月0?{day.day}[日号]",
    )
    if any(re.search(p, quote) for p in patterns):
        return True
    observed = parse_time(sent_at).date()
    for word, offset in DAY_OFFSETS.items():
        pattern = rf"\b{word}\b" if word.isascii() else word
        if re.search(pattern, quote, re.IGNORECASE) and day == observed + timedelta(days=offset):
            return True
    return False


def sourced_times(fact, evidence, content):
    """Drop fabricated observation-as-effective timestamps; retain explicit dates.

    A non-null effective date without a supported quote is rejected unless it is
    exactly a source observation timestamp and the source has no temporal claim.
    The latter is the known extractor defect: repair metadata, not user facts.
    Other ambiguous/future time claims must be re-extracted, never made current.
    """
    from .domain import require

    result = dict(fact)
    for key in ("valid_from", "valid_to"):
        value = fact[key]
        if value is None:
            continue
        if any(
            supported_date(value, p["quote"], content[p["source_index"]]["sent_at"])
            for p in evidence
        ):
            continue
        source_rows = [content[p["source_index"]] for p in evidence]
        # Explicit but unsupported/omitted temporal evidence cannot become a current fact.
        timed = any(
            re.search(
                r"\d{4}[-/年]|\d{1,2}月|前天|昨天|今天|明天|后天|下周|下月|下个月|明年|去年|上周|上月|截至|到期|"
                r"\b(?:yesterday|today|tomorrow|since|until|next|last|expires)\b",
                row["text"],
                re.IGNORECASE,
            )
            for row in source_rows
        )
        observation_copy = any(
            parse_time(value) == parse_time(row["sent_at"]) for row in source_rows
        )
        require(observation_copy and not timed, "invalid_input", 400)
        result[key] = None
    return result


def unit_time(fact, proposed, evidence, content):
    """Render validated intervals; do not retain a second fabricated time claim.

    Canonical dates are validated against original evidence or exact targets by
    their existing owners. This function cannot grant them authority. Undated
    facts may retain source-supported wording without gaining invented bounds.
    """
    start, end = fact["valid_from"], fact["valid_to"]
    if start or end:
        if start and end:
            return f"有效期：{start} 至 {end}"
        return f"有效期自 {start}" if start else f"有效期截止 {end}"
    label = proposed.strip() or "未说明有效时间"
    quotes = [proof["quote"] for proof in evidence]
    dates = re.finditer(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:[日号])?", label)
    for match in dates:
        year, month, day = map(int, match.groups())
        stamp = f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z"
        try:
            supported = any(
                supported_date(stamp, p["quote"], content[p["source_index"]]["sent_at"])
                for p in evidence
            )
        except ValueError:
            supported = False
        if not supported:
            return "未说明有效时间"
    for permanent in ("长期有效", "永久", "永远", "一直有效"):
        if permanent in label and not any(permanent in quote for quote in quotes):
            return "未说明有效时间"
    return label
