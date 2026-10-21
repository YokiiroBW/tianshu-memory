"""Source-grounded author names, shared by extraction and transaction-local fact views.

Names are relevance evidence inside an already authorized owner scope. Neither a
predicate nor a name grants a scope. Legacy rows retain their original keys.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass

from .domain import parse_time, require
from .fact_migration import source_text_installed

KEY = "identity.name"
KEYS = (KEY, "person.name", "author.name", "name")
FIELDS = tuple("automatic." + key for key in KEYS)
MAX_FAMILY = 128
BATCH = 256
SOURCE_BYTES = 65536

INTRO = re.compile(
    r"(?P<past>我(?:以前|曾经|原来|过去|之前)(?:的名字)?(?:叫|名叫))|"
    r"(?P<dated>我(?:从|在)[0-9TtZz:+/年月日时分秒 .-]{6,50}(?:起|开始|期间)(?:名叫|叫))|"
    r"(?:我(?:现在|目前|还是|仍然|依旧)?(?:改名|改)?(?:叫|名叫)|"
    r"大家(?:都)?叫我|"
    r"(?:我的|本人的)(?:名字|姓名|昵称)(?:现在|目前|还是|仍然|依旧)?(?:改成|改为|叫|是|为)|"
    r"本人(?:现在|目前)?(?:名叫|叫|名为)|"
    r"\bmy\s+name\s+is\s+|\bi\s+am\s+(?:named|called)\s+|\bi'm\s+called\s+)",
    re.I,
)
NEUTRAL = re.compile(
    r"^(?:(?:(?:用户|作者|本人)(?:的)?(?:名字|姓名|昵称)?|名字|姓名|昵称)"
    r"(?:现在|目前)?(?:为|是|叫|名为|名叫|[:：])\s*|"
    r"(?:the\s+)?(?:author|user)['’]s\s+(?:name|nickname)\s*(?:is\s+|:\s*)|"
    r"(?:the\s+)?(?:author|user)\s+is\s+(?:named|called)\s+)",
    re.I,
)
CONDITION = re.compile(
    r"如果|假如|假设|要是|倘若|除非|只要|只有|可能|也许|或许|万一|"
    r"\b(?:if|unless|suppose|perhaps|maybe|might)\b",
    re.I,
)
ATTRIBUTION = re.compile(
    r"(?:说|写道|写着|问|喊|提到|告诉我|转述|said|says|wrote|asked)\s*[:：]?\s*$", re.I
)
PAIRS = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"', "'": "'", "`": "`"}
CLAUSE_END = set("，,。；;！!？?.\n\r")
SENTENCE_END = set("。；;！!？?.\n\r")


def candidate(fact):
    return fact["key"] in KEYS and fact.get("entity") is None


def field_values(kind, value):
    """Finite metadata hints only; proof and the logical field are checked later."""
    return FIELDS if kind == "field" and value in FIELDS else (value,)


def _positions(text):
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


def _literal_end(text, value):
    if text.startswith(value):
        return len(value)
    # Presentation quotes may surround the complete literal. Never strip or
    # rewrite quotes inside the extracted value itself.
    if text and text[0] in PAIRS:
        wrapped = text[0] + value + PAIRS[text[0]]
        if text.startswith(wrapped):
            return len(wrapped)
    return None


def _claims(text, fact):
    """Return complete positive author-name claim spans, outside reported speech."""
    quoted = _positions(text)
    value = fact["value"].strip()
    for match in INTRO.finditer(text):
        if (
            quoted[match.start()]
            or (match["past"] and fact["state"] != "historical")
            or (match["dated"] and fact["valid_from"] is None)
        ):
            continue
        start = match.start()
        clause = max(
            (i + 1 for i in range(start) if not quoted[i] and text[i] in CLAUSE_END), default=0
        )
        sentence = max(
            (i + 1 for i in range(start) if not quoted[i] and text[i] in SENTENCE_END), default=0
        )
        prefix = text[clause:start].strip()
        if prefix not in {"", "其实", "对了", "顺便说一下", "顺便说一声"}:
            continue
        before = text[sentence:start]
        if CONDITION.search(before) or ATTRIBUTION.search(before.rstrip("，, ")):
            continue
        end = next(
            (i for i in range(match.end(), len(text)) if not quoted[i] and text[i] in CLAUSE_END),
            len(text),
        )
        tail = text[match.end() : end].lstrip()
        literal = _literal_end(tail, value)
        if literal is None or tail[literal:].strip() not in {"", "了", "啦", "呀", "哦"}:
            continue
        label = tail[:literal]
        quoted_literal = bool(label and label[0] in PAIRS and label.endswith(PAIRS[label[0]]))
        if not quoted_literal and (
            re.search(r"^(?:什么|谁|啥|怎么|哪(?:个|位))|(?:吗|么|呢|吧)$", value)
            or re.match(r"^(?:你们?|他们?|她们?|大家)(?:来|去|别|不要|帮|给|把|拿|做)", value)
            or re.match(r"^(?:not\b|no longer\b)", value, re.I)
        ):
            continue
        if end < len(text) and text[end] in "？?":
            continue
        yield start, end


def _statement(fact, unit):
    text = unit["statement"].strip().rstrip("。.")
    if text == fact["value"].strip() or any(_claims(text, fact)):
        return True
    match = NEUTRAL.match(text)
    if match is None:
        return False
    tail = text[match.end() :].strip()
    end = _literal_end(tail, fact["value"].strip())
    return end is not None and tail[end:].strip() in {"", "了"}


def _supported(fact, unit, quotes):
    return (
        candidate(fact)
        and unit["reality"] == "real"
        and not unit["conditions"]
        and not unit["negations"]
        and _statement(fact, unit)
        and bool(quotes)
        and all(any(_claims(quote, fact)) for quote in quotes)
    )


def grounded(fact, unit, proofs):
    """One source-bound rule for new drafts and retained original name evidence."""
    if not _supported(fact, unit, [proof["quote"] for proof in proofs]):
        return False
    for proof in proofs:
        text, quote = proof.get("source_text"), proof["quote"]
        if type(text) is not str or len(text.encode()) > SOURCE_BYTES:
            return False
        occurrences = [match.start() for match in re.finditer(re.escape(quote), text)]
        if not any(
            offset <= start < end <= offset + len(quote)
            for start, end in _claims(text, fact)
            for offset in occurrences
        ):
            return False
    return True


def prepare(fact, unit, evidence, content):
    """Normalize only after exact evidence validation, before any target identity."""
    if fact["key"] not in KEYS:
        return fact
    if fact.get("entity") is not None:
        require(fact["key"] != KEY, "invalid_input", 400)
        return fact
    require(
        grounded(
            fact,
            unit,
            [dict(proof, source_text=content[proof["source_index"]]["text"]) for proof in evidence],
        ),
        "invalid_input",
        400,
    )
    return dict(fact, key=KEY)


@dataclass(frozen=True)
class NameEntry:
    group: dict
    unit: dict
    row: dict
    fact: dict


@dataclass(frozen=True)
class NameView:
    entries: dict
    incomplete: frozenset

    def current(self, clock, normalize):
        families = defaultdict(list)
        now = clock()
        for identity, entry in self.entries.items():
            fact, row = entry.fact, entry.row
            if (
                row["scope"] in self.incomplete
                or row["superseded_by"]
                or fact["state"] != "current"
                or (fact["valid_from"] and parse_time(fact["valid_from"]) > now)
                or (fact["valid_to"] and parse_time(fact["valid_to"]) <= now)
            ):
                continue
            families[row["scope"]].append((identity, entry))
        result = set()
        for family in families.values():
            latest = max(
                parse_time(e.fact["valid_from"] or e.row["observed_at"]) for _, e in family
            )
            newest = [
                (identity, entry)
                for identity, entry in family
                if parse_time(entry.fact["valid_from"] or entry.row["observed_at"]) == latest
            ]
            if len({normalize(entry.fact["value"]) for _, entry in newest}) != 1:
                continue
            result.add(
                max(newest, key=lambda pair: (parse_time(pair[1].row["observed_at"]), pair[0]))[0]
            )
        return result

    def field_matches(self, group, kind, value):
        if kind == "field" and value in FIELDS:
            if group["id"] in self.entries:
                return True
            if value == "automatic." + KEY:
                return False
        return group[kind + "_key"] == value


def _batches(values):
    values = list(values)
    for offset in range(0, len(values), BATCH):
        batch = values[offset : offset + BATCH]
        yield ",".join("?" for _ in batch), batch


def _family_groups(db, owners):
    for marks, batch in _batches(sorted(owners)):
        yield from db.execute(
            "SELECT * FROM (SELECT g.*,count(*) OVER (PARTITION BY g.scope) AS name_count,"
            "row_number() OVER (PARTITION BY g.scope ORDER BY g.id) AS name_position "
            "FROM groups g JOIN automatic_facts f ON f.group_id=g.id "
            "WHERE g.state='active' AND f.scope=g.scope AND g.scope IN (" + marks + ") "
            "AND f.fact_key IN (?,?,?,?)) AS names WHERE name_position<=?",
            [*batch, *KEYS, MAX_FAMILY + 1],
        )


def family(service, db, scope):
    """The same bounded, verified family for an ordinary sourced name commit."""
    from .domain import canonical
    from .fact_queries import group_queries
    from .recall_index import eligible_groups

    groups = list(_family_groups(db, [canonical(scope)]))
    if any(group["name_count"] > MAX_FAMILY for group in groups):
        return NameView({}, frozenset({canonical(scope)}))
    approved = eligible_groups(service, db, groups, scope)
    rows = {}
    for sql, parameters in group_queries([group["id"] for group, _, _ in approved]):
        rows.update((row["group_id"], row) for row in db.execute(sql, parameters))
    return load(service, db, approved, rows, domain_complete=True)


def load(service, db, candidates, facts, *, domain_complete=None):
    """Build one bounded view after whole-group/source/projection approval.

    Group context supplies its complete authorized candidate domain. Exact-owner
    reads may supplement a partial lexical/category selection with that same
    owner's family. No new viewer, scope, operation or remote request is created.
    """
    entries = {group["id"]: (group, records, units) for group, records, units in candidates}
    possible = {
        identity
        for identity, row in facts.items()
        if row["fact_key"] in KEYS and identity in entries
    }
    if not possible:
        return NameView({}, frozenset())
    # Schema v1 and unproved legacy rows remain raw facts. Neither their key nor
    # a self-looking cropped quote establishes the original message boundary.
    if not source_text_installed(db):
        return NameView({}, frozenset())
    owners = {entries[identity][0]["scope"] for identity in possible}
    incomplete = set()
    if domain_complete is None:
        rows = list(_family_groups(db, owners))
        incomplete.update(row["scope"] for row in rows if row["name_count"] > MAX_FAMILY)
        groups = [
            row for row in rows if row["scope"] not in incomplete and row["id"] not in entries
        ]
        if groups:
            from .fact_queries import group_queries
            from .recall_index import eligible_groups

            scopes = {scope: json.loads(scope) for scope in owners}
            approved = eligible_groups(
                service,
                db,
                groups,
                scopes[groups[0]["scope"]],
                scopes_by_group={group["id"]: scopes[group["scope"]] for group in groups},
            )
            entries.update(
                (group["id"], (group, records, units)) for group, records, units in approved
            )
            for sql, parameters in group_queries([group["id"] for group, _, _ in approved]):
                facts.update((row["group_id"], row) for row in db.execute(sql, parameters))
    else:
        counts = defaultdict(int)
        for identity, row in facts.items():
            if identity in entries and row["fact_key"] in KEYS:
                counts[row["scope"]] += 1
        incomplete.update(scope for scope, count in counts.items() if count > MAX_FAMILY)
        if not domain_complete:
            incomplete.update(owners)
    selected = {
        identity: row
        for identity, row in facts.items()
        if identity in entries and row["fact_key"] in KEYS
    }
    lineage, evidence = defaultdict(dict), defaultdict(list)
    for marks, batch in _batches(selected):
        for row in db.execute(
            "SELECT l.group_id,l.source_key,s.scope FROM lineage l LEFT JOIN sources s "
            "ON s.key=l.source_key WHERE l.group_id IN (" + marks + ")",
            batch,
        ):
            lineage[row["group_id"]][row["source_key"]] = row["scope"]
        for row in db.execute(
            "SELECT group_id,source_key,quote,source_text FROM automatic_evidence WHERE group_id IN ("
            + marks
            + ")",
            batch,
        ):
            evidence[row["group_id"]].append(row)
    result = {}
    for identity, row in selected.items():
        group, records, units = entries[identity]
        fact = json.loads(row["payload"])
        if (
            len(units) != 1
            or row["scope"] != group["scope"]
            or row["fact_key"] != fact["key"]
            or units[0]["subject_person_id"] != json.loads(group["scope"])["person_id"]
            or not lineage[identity]
            or any(scope != group["scope"] for scope in lineage[identity].values())
            or {proof["source_key"] for proof in evidence[identity]} != set(lineage[identity])
            or not grounded(fact, units[0], [dict(proof) for proof in evidence[identity]])
        ):
            continue
        result[identity] = NameEntry(group, units[0], row, dict(fact, key=KEY))
    return NameView(result, frozenset(incomplete))
