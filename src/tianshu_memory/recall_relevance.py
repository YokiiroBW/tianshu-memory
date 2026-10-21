"""One parsed lexical query, reused across its authorized candidate groups."""

import re

from .domain import query_terms, terms
from .fact_migration import installed
from .recall_constraints import Constraints
from .recall_language import (
    correction_target,
    named_reference,
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


def named_owner(service, db, candidates, query, *, complete, fact_snapshot=None):
    """Resolve relevance only inside already authorized original group candidates.

    A current source-proven canonical name supplies the connection, including
    legacy predicates and evidence-category records. Its complete original group
    must travel with any selected target group.
    A truncated window cannot establish uniqueness, even if its prefix has one match.
    """
    if not (installed(db) if fact_snapshot is None else fact_snapshot.installed):
        return candidates, [], query
    from .automatic_facts import normalize
    from .fact_names import candidate
    from .fact_queries import snapshot_for_groups

    if fact_snapshot is None or fact_snapshot.names is None:
        fact_snapshot = snapshot_for_groups(
            db, candidates, service=service, domain_complete=complete
        )
    names = fact_snapshot.names
    current_names = names.current(service.clock, normalize)
    matches, uncertain = [], set()
    for entry in candidates:
        identity = entry[0]["id"]
        name = names.entries.get(identity)
        if name is None:
            row, fact = fact_snapshot.rows.get(identity), fact_snapshot.decoded.get(identity)
            if (
                row is not None
                and fact is not None
                and row["scope"] == entry[0]["scope"]
                and len(entry[2]) == 1
                and candidate(fact)
                and named_reference(query, fact["value"].strip()) is not None
            ):
                uncertain.add(entry[0]["scope"])
        if name is None or identity not in current_names:
            continue
        fact = name.fact
        remaining = named_reference(query, fact["value"].strip())
        if remaining is not None:
            matches.append((entry, remaining))
    if not matches:
        # A visible sourced name is not a current unique connection when its
        # family/window is incomplete, conflicted, expired or not yet effective.
        # Do not silently turn that explicit name question into ordinary group
        # recall merely because the current-name set is empty.
        if uncertain or any(
            named_reference(query, entry.fact["value"].strip()) is not None
            for entry in names.entries.values()
        ):
            return [], [], query
        return candidates, [], query
    owners = {entry[0]["scope"] for entry, _ in matches}
    if not complete or len(owners) != 1 or uncertain - owners:
        return [], [], query
    witness, remaining = matches[0]
    return (
        [entry for entry in candidates if entry[0]["scope"] == witness[0]["scope"]],
        [witness],
        remaining,
    )
