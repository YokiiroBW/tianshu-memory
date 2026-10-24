"""Source-explicit corrections for typed claims, before canonical persistence.

These rules handle contradictory roles and wording, not general language
understanding. Unknown subjects and unsupported assertions remain retryable;
keywords and model-chosen entity IDs never constitute source evidence.
"""

import re

from .domain import require
from .fact_source_text import bound_contexts, quoted_positions

SEPARATOR = re.compile(r"[，,。；;！!？?\n\r]")
INTENTION = re.compile(
    r"^(?:我(?:先|打算|计划|准备|想|考虑)|(?:周[一二三四五六日天]|星期[一二三四五六日天])(?:打算|计划|准备|想))"
)
COMPLETED = re.compile(
    r"已经|已(?:经)?(?:把|将|去|到|做|送|关|找)|刚才|后来|做完|好了|上了|到了|去了|过了"
)
EXPLICIT_PLAN = re.compile(
    r"^(?:我(?:打算|计划|准备|考虑)|(?:周[一二三四五六日天]|星期[一二三四五六日天])(?:打算|计划|准备|考虑))"
)
ACTION_PROPERTIES = {"action", "task", "intention", "plan", "travel", "outing"}
CANCELLED = re.compile(r"(?:已经|已)?(?:退了|退掉|退款|取消|作废)|不再参加|不去了")
ATTENDED = re.compile(
    r"(?:实际|确实|已经|已)(?:已经|已)?(?:去|到场|到达|参加)|"
    r"去[了过]|参加[了过]|到(?:场|达)了"
)
POSSESSION_LOCATION = re.compile(
    r"(?P<mention>我(?:家)?的[^，,。；;！!？?\n\r]{1,40}?)"
    r"(?:平时|通常|一般|现在|刚刚|刚|已经|一直|总是|都)?"
    r"(?:收|放|挪|移|留)(?:在|到了?|到)"
    r"(?P<place>[^，,。；;！!？?\n\r]{1,60})"
)


def location_property(predicate):
    parts = set(predicate.split("."))
    return bool(
        parts & {"location", "position", "storage_location", "current_location", "stored_in"}
    )


def _action_clauses(value, contexts):
    """Select literal action wording, never a model keyword as source proof."""
    grams = {value[i : i + 2] for i in range(len(value) - 1)}
    scored = [
        (sum(gram in clause for gram in grams), clause, safe)
        for sentence, safe in contexts
        for clause in SEPARATOR.split(sentence)
        if clause.strip()
    ]
    best = max((score for score, _, _ in scored), default=0)
    if not best:
        # Chinese object-fronting/aspect can split the value's literal bigrams
        # (关上门 / 把门关了). Require at least two sourced value characters;
        # every tied clause still participates in the subject/safety checks.
        characters = {char for char in value if char.isalnum()}
        scored = [
            (sum(char in clause for char in characters), clause, safe) for _, clause, safe in scored
        ]
        best = max((score for score, _, _ in scored), default=0)
        if best < 2:
            return []
    return [(clause.strip(), safe) for score, clause, safe in scored if best and score == best]


def _actual_attendance(clause):
    quoted = quoted_positions(clause)
    return any(
        not quoted[match.start()]
        and not re.search(r"打算|计划|准备|考虑|会|将|不一定|不确定", clause[: match.start()])
        and not re.search(
            r"(?:没(?:有)?|不曾|未(?:曾)?|不再)(?:真的|实际|确实)?$", clause[: match.start()]
        )
        for match in ATTENDED.finditer(clause)
    )


def source_subject(fact, quotes):
    """An explicit possessed object's location cannot belong to its author/place.

    The transaction resolver can reuse a uniquely matching sourced object. No
    absent object is recovered from keywords or from a room-wide guess.
    """
    value = fact["canonical"]
    subject = value["subject"]
    if fact["update"]["operation"] in {"confirm", "retract"}:
        return subject, False
    parts = set(value["predicate"].split("."))
    agent_property = parts & {"action", "task", "intention"} or (
        "plan" in parts
        and value["predicate"].split(".")[-1] in {"visit", "action", "prepare", "departure"}
    )
    agent_repair = (
        subject["kind"] == "entity"
        and subject["type"] in {"object", "place", "location", "bag"}
        and agent_property
    )
    if not agent_repair and not location_property(value["predicate"]):
        return subject, False
    contexts = [context for proof in quotes for context in bound_contexts(proof)]
    action_clauses = _action_clauses(value["value"], contexts)
    if agent_repair:
        # A tied friend/author clause, question or conditional cannot select an
        # actor just because one of the matching clauses uses first person.
        require(
            bool(action_clauses)
            and all(
                safe and (INTENTION.match(clause) or clause.startswith("先把"))
                for clause, safe in action_clauses
            ),
            "invalid_input",
            400,
        )
        return dict(kind="author", id=None, type=None, mention=None), True
    if not location_property(value["predicate"]):
        return subject, False
    self_location = (
        subject["kind"] != "author" and subject["mention"].strip() == value["value"].strip()
    )
    if subject["kind"] != "author" and not self_location:
        return subject, False
    found = {
        match["mention"]
        for sentence, safe in contexts
        if safe
        for match in POSSESSION_LOCATION.finditer(sentence)
        if value["value"].strip() == match["place"].strip()
    }
    if len(found) == 1:
        return dict(kind="entity", id=None, type="object", mention=found.pop()), True
    require(not self_location, "invalid_input", 400)
    return subject, False


def sourced_modality(fact, evidence, content):
    """Do not turn an explicit immediate intention into a completed action."""
    value = fact["canonical"]
    if fact["update"]["operation"] in {"confirm", "retract"}:
        return fact
    parts = set(value["predicate"].split("."))
    if value["modality"] != "observed" or not parts & (
        ACTION_PROPERTIES | {"attendance", "attendance_date", "arrived_at_destination"}
    ):
        return fact
    contexts = [
        context
        for proof in evidence
        for context in bound_contexts(
            dict(quote=proof["quote"], context_text=content[proof["source_index"]]["text"])
        )
    ]
    clauses = [
        (part.strip(), safe)
        for sentence, safe in contexts
        for part in SEPARATOR.split(sentence)
        if part.strip()
    ]
    if value["modality"] == "observed" and parts & {
        "attendance",
        "attendance_date",
        "arrived_at_destination",
    }:
        # A refunded ticket is an observed refund, not observed attendance. The
        # producer can re-extract that actual event; do not invent a new property.
        unsupported = [
            part for part, _ in clauses if value["value"] in part and CANCELLED.search(part)
        ]
        actual = [
            part
            for part, safe in clauses
            if safe
            and value["value"] in part
            and _actual_attendance(part)
            and not CANCELLED.search(part)
        ]
        require(not unsupported or actual, "invalid_input", 400)
    action_clauses = _action_clauses(value["value"], contexts)
    if (
        value["modality"] == "observed"
        and parts & ACTION_PROPERTIES
        and action_clauses
        and any(INTENTION.match(part) for part, _ in action_clauses)
    ):
        require(
            all(safe and INTENTION.match(part) for part, safe in action_clauses),
            "invalid_input",
            400,
        )
        completed = [
            bool(COMPLETED.search(part))
            and not EXPLICIT_PLAN.match(part)
            and not re.search(r"了(?:再|之后再|以后再)", part)
            for part, _ in action_clauses
        ]
        require(all(completed) or not any(completed), "invalid_input", 400)
        if all(completed):
            return fact
        immediate = any(part.startswith("我先") for part, _ in action_clauses)
        value = dict(value, modality="planned", state="current" if immediate else value["state"])
        return dict(fact, canonical=value, state=value["state"])
    return fact
