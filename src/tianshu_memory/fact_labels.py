"""Literal object labels from verified source text, not model explanations or hints."""

import re
from bisect import bisect_right

from . import fact_names
from .domain import require
from .fact_entities import OBJECT_NOUNS

NOUNS = {kind: aliases for kind, (_, aliases) in OBJECT_NOUNS.items()} | {
    "notebook": (*OBJECT_NOUNS["notebook"][1], "本子"),
    "journal": ("手账", "笔记本", "笔记", "本子"),
    "cup": ("水杯", "杯子"),
    "bird": ("文鸟", "鹦鹉", "小鸟", "鸟"),
}
PAIRS = {"《": "》", "“": "”", "「": "」", "『": "』", '"': '"', "'": "'", "（": "）", "(": ")"}
ENDS = "，,。；;！!？?\n\r."
SENTENCE_ENDS = "。；;！!？?\n\r."
NAMING = re.compile(
    r"(?P<past>旧名(?:字)?(?:是|为|叫)?|原名(?:字)?(?:是|为|叫)?)|"
    r"改名叫|改名为|更名为|改叫|名叫|叫作|叫做|叫|"
    r"(?:标题|书名|名字|名称)(?:现在|目前)?(?:是|为|叫|[:：])|"
    r"还是|仍(?:然)?叫|依(?:然|旧)叫|仍(?:然)?是|仍(?:然)?为|"
    r"\b(?:is named|is called|name is|title is|renamed to)\s+",
    re.I,
)
NEGATIVE_WORDS = r"不是|并非|并没有|没有|并未|不曾|不再|从不|不要|别|没|不|未"
NEGATIVE_END = re.compile(
    rf"(?:{NEGATIVE_WORDS})(?:再|改|重新)*(?:把|将)|"
    rf"(?:{NEGATIVE_WORDS})(?:再|改|重新)*$"
)
UNSAFE = re.compile(r"如果|假如|假设|要是|可能|也许|或许|万一|\b(?:if|maybe|perhaps)\b", re.I)
PAST = re.compile(r"以前|之前|原来|过去|曾经|旧名|原名")
CONTINUATION = re.compile(
    r"^(?:(?:以后|今后|后来|现在|目前|就|仍然|依旧|还|它|这本|那本|这只|那只)\s*)*$"
)
SUBJECT_UNSAFE = re.compile(r"不像|不是|不如|不比|旁边|对比")
ACTION = re.compile(r"又|刚|总|会|把|在|也|正|一直")


def candidate(fact):
    if fact["key"].split(".")[-1] not in {"name", "title"} or fact_names.candidate(fact):
        return False
    return fact.get("entity") is not None or any(k in NOUNS for k in fact["key"].split("."))


def _positions(text):
    stack, quoted = [], []
    for index, ch in enumerate(text):
        quoted.append(bool(stack))
        if stack and ch == stack[-1]:
            stack.pop()
        elif ch in PAIRS and not (
            ch == "'"
            and index
            and index + 1 < len(text)
            and text[index - 1].isalnum()
            and text[index + 1].isalnum()
        ):
            stack.append(PAIRS[ch])
    return quoted


def _clauses(text):
    """Keep quoted labels and their punctuation whole; identify outside assertions."""
    quoted, start = _positions(text), 0
    for index, ch in enumerate(text):
        if not quoted[index] and ch not in PAIRS and ch in ENDS:
            yield start, index, ch, quoted
            start = index + 1
    yield start, len(text), "", quoted


def _syntax(text):
    quoted = _positions(text)
    return "".join(" " if quoted[i] or ch in PAIRS else ch for i, ch in enumerate(text))


def _literal_end(text, value):
    if text.startswith(value):
        return len(value)
    if text and text[0] in PAIRS and text.startswith(text[0] + value + PAIRS[text[0]]):
        return len(value) + 2
    return None


def _nouns(fact):
    entity = fact.get("entity")
    kinds = [entity["kind"]] if entity else fact["key"].split(".")
    return tuple(noun for kind in kinds for noun in NOUNS.get(kind, ()))


def _subject(fact, text, *, mention=None):
    text = _syntax(text)
    if SUBJECT_UNSAFE.search(text):
        return False
    entity = fact.get("entity")
    # Some existing pet proposals label the entity with the name itself. In
    # that case the source still needs its explicit typed noun, not that name
    # occurring somewhere in the value or in another object's statement.
    nouns = [
        match
        for aliases in NOUNS.values()
        for noun in aliases
        for match in re.finditer(re.escape(noun), text)
    ]
    last = max((m.end() for m in nouns), default=0)
    last_start = min((m.start() for m in nouns if m.end() == last), default=0)
    mention = entity["mention"] if entity and mention is None else mention
    if entity and mention != fact["value"]:
        return any(m.end() >= last_start for m in re.finditer(re.escape(mention), text))
    return any(m.end() == last for noun in _nouns(fact) for m in re.finditer(re.escape(noun), text))


def _apposition(text, value):
    """Return the complete source-parsed label length, never a model-chosen cut."""
    if text and text[0] in PAIRS:
        quoted = _positions(text)
        end = next(
            (
                i + 1
                for i, ch in enumerate(text)
                if i
                and quoted[i]
                and ch == PAIRS[text[0]]
                and (i + 1 == len(text) or not quoted[i + 1])
            ),
            None,
        )
        label = text[:end] if end else ""
    else:
        # A naming/denial clause is not the noun apposition 狗豆包. Its
        # assertion must be checked by the ordinary naming path instead.
        if NAMING.search(text) or re.match(rf"^(?:{NEGATIVE_WORDS})", text):
            return None
        action = ACTION.search(text)
        label = text[: action.start()] if action else text
    literal = _literal_end(label, value)
    return len(label) if label and literal == len(label) else None


def _appositions(fact, text):
    nouns = sorted(set(_nouns(fact)), key=len, reverse=True)
    if not nouns:
        return
    quoted = _positions(text)
    # Prefer complete vocabulary nouns: 猫咪糖球 cannot become 猫 + 咪糖球.
    for match in re.finditer("|".join(re.escape(noun) for noun in nouns), text):
        if quoted[match.start()]:
            continue
        tail_start = match.end()
        while tail_start < len(text) and text[tail_start].isspace():
            tail_start += 1
        length = _apposition(text[tail_start:].strip(), fact["value"].strip())
        if length is not None:
            yield match.start(), match.end(), tail_start + length


def _appositive_head(fact, text, noun_start, noun_end, label_end):
    """Bind a whole composite mention, excluding the already parsed name from its head."""
    entity = fact.get("entity")
    if entity is None:
        return None
    mention = entity["mention"]
    begin = label_end - len(mention)
    if 0 <= begin <= noun_start and text[begin:label_end] == mention:
        return text[begin:noun_end]
    return None


def constraint_head(fact):
    """Only a relevance projection; this does not certify old source metadata."""
    mention = fact["entity"]["mention"]
    if mention == fact["value"]:
        return ""
    for noun_start, noun_end, label_end in _appositions(fact, mention):
        head = _appositive_head(fact, mention, noun_start, noun_end, label_end)
        if (
            label_end == len(mention)
            and head is not None
            and _subject(fact, mention[:noun_end], mention=head)
        ):
            return head
    return mention


def _claims(text, fact, *, subject=True):
    value = fact["value"].strip()
    anchor = None
    sentence = 0
    syntax = _syntax(text)
    all_nouns = tuple(noun for aliases in NOUNS.values() for noun in aliases)
    for start, end, punctuation, quoted in _clauses(text):
        clause = text[start:end]
        # Only a prior subject in the same sentence may supply an omitted object.
        grammar = syntax[start:end]
        first_naming = NAMING.search(grammar)
        head = clause[: first_naming.start()] if first_naming else clause
        bound = _subject(fact, head)
        if bound:
            anchor = start
        elif any(noun in head for noun in all_nouns):
            anchor = None
        for match in NAMING.finditer(grammar):
            position = start + match.start()
            if quoted[position] or punctuation in "？?" and punctuation:
                continue
            head = grammar[: match.start()].strip()
            if NEGATIVE_END.search(head) or UNSAFE.search(syntax[sentence:position]):
                continue
            explicit = _subject(fact, head)
            inherited = anchor is not None and CONTINUATION.fullmatch(head)
            if subject and not (explicit or inherited):
                continue
            # A cropped quote cannot remove a negation, condition or a needed subject.
            claim_start = start if explicit or not subject else anchor
            past = bool(match["past"] or PAST.search(head))
            if past != (fact["state"] == "historical"):
                continue
            tail = clause[match.end() :].lstrip()
            literal = _literal_end(tail, value)
            if literal is None or tail[literal:].strip() not in {"", "了", "啦", "呀", "哦"}:
                continue
            yield claim_start, end
        # Existing social input also uses the explicit apposition 我家狗豆包又… .
        for noun_start, noun_end, label_end in _appositions(fact, clause):
            position = start + noun_start
            if UNSAFE.search(syntax[sentence:position]):
                continue
            head = clause[:noun_end]
            composite = _appositive_head(fact, clause, noun_start, noun_end, label_end)
            if subject and not (
                _subject(fact, head)
                or composite is not None
                and _subject(fact, head, mention=composite)
            ):
                continue
            if NEGATIVE_END.search(grammar[:noun_start].strip()) or fact["state"] != "current":
                continue
            if punctuation not in "？?" or not punctuation:
                yield start, end
        if punctuation in SENTENCE_ENDS:
            anchor = None
            sentence = end + 1


def prepare(fact, evidence, content):
    """Reject the whole proposal; never trim a model value or fill it from known facts."""
    if not candidate(fact):
        return fact
    for proof in evidence:
        text, quote = content[proof["source_index"]]["text"], proof["quote"]
        occurrences = [match.start() for match in re.finditer(re.escape(quote), text)]

        def covered(start, end):
            index = bisect_right(occurrences, start) - 1
            return index >= 0 and end <= occurrences[index] + len(quote)

        require(
            any(covered(start, end) for start, end in _claims(text, fact)),
            "invalid_input",
            400,
        )
    return fact


def positive_source(fact, quotes):
    """Intent only, using the group's source quotes rather than model summaries.

    Binding was separately proved at admission. A resolved ordinal/alias may
    differ from the literal subject in a valid original quote.
    """
    return any(any(_claims(text, fact, subject=False)) for text in quotes)
