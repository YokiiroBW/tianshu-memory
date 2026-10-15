"""Lexical candidate admission, separate from ranking and prompt size limits."""

import re

from .domain import query_terms, terms
from .recall_language import (
    correction_target,
    query_aliases,
    query_clauses,
    query_focus,
    topic_family,
    topic_tokens,
)


def admits(query, indexed_terms, text=None):
    return bool(matching_clauses(query, indexed_terms, text))


def matching_clauses(query, indexed_terms, text=None):
    """Require a matching topic or sufficient content coverage, not any shared bigram.

    Clauses are independent so asking about several facts does not require each
    fact to mention every requested topic. Semantic retrieval has its own scores;
    this gate applies to the keyword candidate lane only.
    """
    clauses = query_clauses(query)
    # A dependent instruction like '按我的口味选' is not a second request about
    # every food preference when the actual request has already named a drink.
    if "topic19" in topic_tokens(query):
        clauses = [
            clause
            for clause in clauses
            if not re.match(r"^(?:按|照)(?:我|我的)?(?:平时|平常)?口味", clause)
        ]
    return [clause for clause in clauses if _clause_admits(clause, indexed_terms, text)]


def _clause_admits(clause, indexed_terms, text=None):
    from .recall_constraints import constraints

    decision = constraints(clause, text or " ".join(indexed_terms))
    if decision is not None:
        return decision
    replacement = correction_target(clause)
    quoted = re.findall(r'[“「《"]([^”」》"]+)[”」》"]', clause)
    if quoted and replacement is None:
        return any(
            bool(terms(title)) and set(terms(title)).issubset(indexed_terms) for title in quoted
        )
    focus = replacement or query_focus(clause)
    if focus:
        positions = re.findall(r"第[一二三四五六七八九十百零两0-9]+", focus)
        if positions and not any(
            set(terms(position)).issubset(indexed_terms) for position in positions
        ):
            return False
        topics = set(topic_tokens(focus))
        if topics:
            return topics.issubset(topic_family(indexed_terms))
        anchors = {term for term in terms(focus) if not re.fullmatch(r"[\u3400-\u9fff]{3,}", term)}
        if anchors:
            return anchors.issubset(indexed_terms)
    if clause:
        topics = set(topic_tokens(clause))
        if topics:
            if topics.intersection(topic_family(indexed_terms)):
                return True
            return False
        expanded = query_aliases(clause)
        if expanded and set(terms(" ".join(expanded))).intersection(indexed_terms):
            return True
        content = {
            term for term in query_terms(clause) if not re.fullmatch(r"[\u3400-\u9fff]{3,}", term)
        }
        if content and len(content.intersection(indexed_terms)) / len(content) >= 0.5:
            return True
    return False
