"""One parsed lexical query, reused across its authorized candidate groups."""

import re

from .domain import query_terms, terms
from .recall_constraints import Constraints
from .recall_language import (
    correction_target,
    normalize_objects,
    query_aliases,
    query_clauses,
    query_focus,
    topic_family,
    topic_tokens,
)


def anchors(value):
    return {term for term in value if not re.fullmatch(r"[\u3400-\u9fff]{3,}", term)}


class Clause:
    def __init__(self, text):
        self.text = text
        self.constraints = Constraints(text)
        replacement = correction_target(text)
        quoted = re.findall(r'[“「《"]([^”」》"]+)[”」》"]', text)
        self.quoted = [set(terms(title)) for title in quoted] if replacement is None else []
        focus = replacement or query_focus(text)
        self.positions = [
            set(terms(p)) for p in re.findall(r"第[一二三四五六七八九十百零两0-9]+", focus or "")
        ]
        self.focus_topics = set(topic_tokens(focus)) if focus else set()
        self.focus_anchors = anchors(terms(focus)) if focus else set()
        self.topics = set(topic_tokens(text))
        self.aliases = set(terms(" ".join(query_aliases(text))))
        self.content = anchors(query_terms(text))

    def admits(self, indexed, text):
        decision = self.constraints.admit(text)
        if decision is not None:
            return decision
        if self.quoted:
            return any(title and title.issubset(indexed) for title in self.quoted)
        if self.positions and not any(p.issubset(indexed) for p in self.positions):
            return False
        if self.focus_topics:
            return self.focus_topics.issubset(topic_family(indexed))
        if self.focus_anchors:
            return self.focus_anchors.issubset(indexed)
        if self.topics:
            return bool(self.topics.intersection(topic_family(indexed)))
        if self.aliases.intersection(indexed):
            return True
        return (
            bool(self.content)
            and len(self.content.intersection(indexed)) / len(self.content) >= 0.5
        )


class LexicalQuery:
    """Request-local only: no remembered text or permission results survive a query."""

    def __init__(self, query):
        query = normalize_objects(query)
        clauses = query_clauses(query)
        # A dependent '按我的口味选' does not request every food preference.
        if "topic19" in topic_tokens(query):
            clauses = [
                c for c in clauses if not re.match(r"^(?:按|照)(?:我|我的)?(?:平时|平常)?口味", c)
            ]
        self.clauses = [Clause(c) for c in clauses]

    def match(self, indexed, text):
        return {
            c.text: c.constraints.specificity(text) for c in self.clauses if c.admits(indexed, text)
        }
