"""Source-explicit corrections for typed claims, before canonical persistence.

These rules handle contradictory roles and wording, not general language
understanding. Unknown subjects and unsupported assertions remain retryable;
keywords and model-chosen entity IDs never constitute source evidence.
"""

import re

from .domain import require
from .fact_source_text import ATTRIBUTION, PAIRS, bound_contexts, quoted_positions

SEPARATOR = re.compile(r"[，,。；;！!？?\n\r]")
INTENTION = re.compile(
    r"(?:我(?:先|打算|计划|准备|想(?!起|象|到|念|知道|问|说)|考虑)|"
    r"(?:周[一二三四五六日天]|星期[一二三四五六日天])(?:打算|计划|准备|想))"
)
WISH = re.compile(r"我(?:只是)?(?:希望|盼望|想象)|但愿")
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
UNDETERMINED = re.compile(
    r"(?:不|不能|无法)(?:太)?确定|尚未确定|未确定|没(?:有)?确定|不知道|不清楚|没(?:有)?把握|"
    r"是否|是不是|有没有|能不能|尚待确认|有待确认|不一定|"
    r"\b(?:not sure|uncertain|unknown|do not know|don't know|whether)\b",
    re.I,
)
PENDING_CHANGE = re.compile(
    r"(?:改为|改成|调整为|重置为|设为)(?:暂时|暂)?(?:待定|未定|不确定|未知)"
)
RETIRING_ACTIONS = r"撤回|收回|撤销|停掉|停止|退出|归还|还来|还给|挪走|" + CANCELLED.pattern
RETIREMENT_CONTROL = re.compile(
    RETIRING_ACTIONS
    + r"|说错|有误|不可靠|不再有效|"
    + PENDING_CHANGE.pattern
    + r"|\b(?:retract|withdraw|unreliable|incorrect|no longer valid)\b",
    re.I,
)
AFFIRMATION = re.compile(r"确认|(?:仍然?|依然|依旧)有效|已明确|\bconfirm\b", re.I)
CHANGE = re.compile(r"改成|改为|改用|调整为|重置为|设为|改了|改动|更正|纠正")
NEGATED_CONTROL = re.compile(
    r"(?:不要|不必|不用|不需要|不想|不打算|不会|不能|无需|没有|并未|不曾|没|别|不)(?:再|去)?"
    r"(?:确认|说错|改为|改成|调整为|重置为|设为|"
    + RETIRING_ACTIONS
    + r")|(?:不是|并非|没有|并未|不曾|没)(?:真的|确实)?(?:说错|有误|不可靠|不再有效)|"
    + r"\b(?:do not|don't|did not|didn't|never)\s+(?:retract|withdraw)\b",
    re.I,
)
CONTROL_INTENTION = re.compile(
    r"我(?:(?:打算|计划|准备|考虑)(?:先|去)?|想(?:要)?)(?:确认|改为|改成|" + RETIRING_ACTIONS + r")"
)
INTERROGATIVE = re.compile(r"是不是|有没有|能不能|要不要|该不该|是否|\bwhether\b", re.I)
CLAIM_REFERENCE = (
    r"(?:(?:之前|先前|刚才|原来|原先)(?:确认的)?(?:那|这)?|这|那)(?:一)?(?:条|项)|"
    r"(?:原|旧)(?:说法|主张|记录)"
)
REFERENCE_CONTROL = re.compile(
    r"^(?:(?:我现在|我|请|现在|所以|因此|就|再)\s*)*"
    r"(?:(?:撤回|收回|撤销|确认)\s*(?:"
    + CLAIM_REFERENCE
    + r")|(?:"
    + CLAIM_REFERENCE
    + r").*(?:作废|有误|不可靠|不再有效|仍然?有效))"
)


def _control_syntax(text):
    quoted = quoted_positions(text)
    return "".join(
        " " if quoted[i] or (ch != "'" and (ch in PAIRS or ch in PAIRS.values())) else ch
        for i, ch in enumerate(text)
    )


def _control_clauses(proof):
    """Only current quoted wording supplies a control, never inherited evidence."""
    syntax = _control_syntax(proof["quote"])
    for match in re.finditer(r"[^，,。；;！!？?\n\r]+", syntax):
        clause = match.group().strip()
        if clause:
            yield clause, proof["quote"][match.start() : match.end()]


def _supports_update(fact, clause, literal):
    operation = fact["update"]["operation"]
    if operation == "retract":
        return bool(RETIREMENT_CONTROL.search(clause))
    if operation == "confirm":
        return not RETIREMENT_CONTROL.search(clause) and (
            fact["value"] in literal or bool(AFFIRMATION.search(clause))
        )
    return (
        fact["value"] in literal
        or bool(CHANGE.search(clause))
        or bool(RETIREMENT_CONTROL.search(clause))
        or bool(AFFIRMATION.search(clause))
    )


def retirement_supported(prior, fact, quotes):
    """A question or unknown claim cannot consume an existing known target.

    Called with the validated exact target inside the write transaction. These
    are finite source-expression checks, not general claim/target resolution.
    Exact owner/property/version authority remains in the caller. A current
    control quote is required; inherited target values alone prove no control.
    """
    if not quotes:
        return False
    for proof in quotes:
        contexts = bound_contexts(proof)
        clauses = list(_control_clauses(proof))
        supported = [
            (clause, literal)
            for clause, literal in clauses
            if _supports_update(fact, clause, literal)
        ]
        if not contexts or not supported:
            return False
        # Only an explicit old-record reference bridges an admission of not
        # knowing to an actual withdrawal. “Dinner is now pending” supplies no
        # such bridge for the preceding unknown email assertion. Narrowing the
        # quote cannot remove that same sentence's unknown/question context.
        referenced = any(REFERENCE_CONTROL.search(clause) for clause, _ in supported)
        for sentence, safe in contexts:
            syntax = _control_syntax(sentence)
            if (
                not safe
                or not any(ch.isalnum() for ch in syntax)
                or ATTRIBUTION.search(syntax.rstrip("，,。.;；!！?？ ：:'\"‘’“”"))
                or NEGATED_CONTROL.search(syntax)
                or CONTROL_INTENTION.search(syntax)
            ):
                return False
            if any(
                not UNDETERMINED.search(clause[: match.start()])
                for clause in re.split(r"[，,]", syntax)
                for match in INTERROGATIVE.finditer(clause)
            ):
                return False
            # A declared pending value is a change, not an unknown statement
            # about whether the old value changed. Remove only that expression.
            if UNDETERMINED.search(PENDING_CHANGE.sub("", syntax)) and not referenced:
                return False
        if (
            prior["canonical"]["modality"] in {"asserted", "observed"}
            and fact["canonical"]["modality"] == "uncertain"
            and not any(RETIREMENT_CONTROL.search(clause) for clause, _ in supported)
        ):
            return False
    return True


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


def _completed_author_action(value, clause, safe):
    """A wish exception needs a direct, literal first-person completion.

    Permit the same action's object-fronting/aspect, not another subject or a
    report about imagining, preparing or quoting that action. This deliberately
    recognizes a finite completion frame rather than arbitrary paraphrases.
    """
    # Quoted modifiers are meaningful too: deleting “准备” from an otherwise
    # first-person clause must never manufacture proof that it was completed.
    if not safe or _control_syntax(clause) != clause:
        return False
    match = re.fullmatch(
        r"(?:但|不过|可是|然而)?我(?:实际|确实)?(?:已经|已|刚才|刚刚)"
        r"(?:实际|确实)?(?P<action>.+)",
        clause.strip(),
    )
    if not match:
        return False
    action = re.sub(r"^(?:把|将)", "", match["action"])
    characters = {char for char in value if char.isalnum()}
    return (
        bool(characters)
        and characters <= set(action)
        and all(char.isspace() or char in characters | {"了", "过"} for char in action)
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
    """The same sourced intention constrains asserted and observed actions."""
    value = fact["canonical"]
    if fact["update"]["operation"] in {"confirm", "retract"}:
        return fact
    parts = set(value["predicate"].split("."))
    if value["modality"] not in {"asserted", "observed"} or not parts & (
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
    if parts & ACTION_PROPERTIES:
        # A wish can be recorded as a wish, but supplies neither an established
        # action nor an executable plan. A separate, explicit completion of
        # this very action by its author can still support the actual report.
        wished = any(WISH.search(part) for part, _ in action_clauses)
        completed = value["subject"]["kind"] == "author" and any(
            _completed_author_action(value["value"], part, safe) for part, safe in action_clauses
        )
        require(not wished or completed, "invalid_input", 400)
    if (
        parts & ACTION_PROPERTIES
        and action_clauses
        and any(INTENTION.search(part) for part, _ in action_clauses)
    ):
        # Search finds quoted/attributed first-person intentions too. They must
        # not bypass this guard just because the clause does not start with 我.
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
