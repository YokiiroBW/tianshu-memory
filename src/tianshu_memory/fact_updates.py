"""Conservative replacement rules for sourced, same-predicate automatic facts.

A denial is evidence against its named value, not a replacement for every other
value of the predicate. Ambiguous denials remain evidence without retiring facts.
"""

import re

NEGATIVE = re.compile(
    r"(?:不再|没有|没|不|很少|从未|从不)(?:再|太|怎么|跟着|重新|换回去|换回)*"
    r"(?:喝|吃|碰|选|点|用|住|喜欢|爱|养|去|是|叫|换回)|"
    r"\b(?:no longer|do not|don't|doesn't|not|never)\b",
    re.I,
)
UNCHANGED = re.compile(r"(?:没|没有|并未|不)(?:跟着)?(?:换回|改回|改变|更换|变化)")
DENIED_TARGET = re.compile(
    r"(?:不再|没有|没|不|很少|从未|从不)(?:再|太|怎么|跟着|重新|换回去|换回)*"
    r"(?:喝|吃|碰|选|点|用|住在|住|喜欢|爱|养|去|是|叫)"
    r"(?:以前的|原来的|以前那杯|以前那种|以前那本)?(.+?)(?:了|啦|的了)?$"
)
ENGLISH_DENIED_TARGET = re.compile(
    r"\b(?:no longer|do not|don't|does not|doesn't|never)\s+"
    r"(?:drink|eat|use|like|prefer|live in|have)\s+(.+?)(?:\s+anymore)?[.!]?$",
    re.I,
)


def clauses(value):
    # Quoted names are literal values, not executable assertions or negations.
    return [part.strip() for part in re.split(r"[，,。；;]|\bbut\b", value) if part.strip()]


def positive_clauses(fact, unit):
    parts = clauses(fact["value"])
    if unit["negations"] and not any(NEGATIVE.search(part) for part in parts):
        parts = clauses(unit["statement"])
    return [part for part in parts if not NEGATIVE.search(part) and not UNCHANGED.search(part)]


def may_replace(prior, prior_unit, current, current_unit):
    """Called only after entity, source, scope and effective-time checks."""
    # Attribute names/titles can themselves look like negative sentences.
    if current["key"].split(".")[-1] in {"name", "title"}:
        return True
    positive = positive_clauses(current, current_unit)
    if positive:
        return True
    if current_unit["conditions"] and set(current_unit["conditions"]) != set(
        prior_unit["conditions"]
    ):
        # A restricted denial does not erase a more general positive preference.
        return False
    if UNCHANGED.search(current["value"]):
        return False
    targets = []
    for part in clauses(current["value"]):
        match = DENIED_TARGET.search(part) or ENGLISH_DENIED_TARGET.search(part)
        if match:
            target = match[1].strip(' 。！？!?“”"')
            if len(target) >= 2 and target not in {"这个", "那个", "以前那种", "原来的"}:
                targets.append(target)
    return any(
        target.casefold() in part.casefold()
        for target in targets
        for part in positive_clauses(prior, prior_unit)
    )
