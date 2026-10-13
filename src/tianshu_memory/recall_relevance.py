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


def admits(query, indexed_terms):
    """Require a matching topic or sufficient content coverage, not any shared bigram.

    Clauses are independent so asking about several facts does not require each
    fact to mention every requested topic. Semantic retrieval has its own scores;
    this gate applies to the keyword candidate lane only.
    """
    return any(_clause_admits(clause, indexed_terms) for clause in query_clauses(query))


def _clause_admits(clause, indexed_terms):
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
