"""Candidate partitions, never authorization or identity resolution."""

from collections import defaultdict
from itertools import zip_longest
from math import ceil

from ..domain import canonical


def partition(eligible, scopes=None):
    allowed = None if scopes is None else {canonical(scope) for scope in scopes}
    result = defaultdict(list)
    for entry in eligible:
        scope = entry[0]["scope"]
        if allowed is None or scope in allowed:
            result[scope].append(entry)
    return dict(sorted(result.items()))


def documents_by_owner(documents):
    result = defaultdict(list)
    for document in documents:
        result[document["scope"]].append(document)
    return dict(sorted(result.items()))


def candidate_limit(candidates, owners):
    return max(8, ceil(candidates / max(1, owners)) * 2)


def interleave(per_owner):
    return [item for row in zip_longest(*per_owner.values()) for item in row if item is not None]
