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
