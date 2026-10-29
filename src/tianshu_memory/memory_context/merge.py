"""Quota and diversity act on complete selection units, never on source authority."""

from collections import defaultdict

from ..retrieval_vectors import dot


def quota_merge(per_owner, budget, floor=None):
    owners = sorted(owner for owner, items in per_owner.items() if items)
    if not owners or budget <= 0:
        return []
    floor = max(1, budget // len(owners)) if floor is None else max(0, floor)
    result, used = [], 0
    for owner in owners:
        covered = 0
        for item in per_owner[owner]:
            cost = item.get("cost", 1)
            if covered >= floor:
                break
            if used + cost <= budget:
                result.append(item)
                used += cost
                covered += cost
    included = {item["id"] for item in result}
    remaining = sorted(
        (item for owner in owners for item in per_owner[owner] if item["id"] not in included),
        key=lambda item: (-item["score"], item["id"]),
    )
    for item in remaining:
        if used + item.get("cost", 1) <= budget:
            result.append(item)
            used += item.get("cost", 1)
    return result


def mmr_diversify(items, vectors, lambda_=0.7, limit=None):
    selected, remaining = [], list(items)
    limit = len(items) if limit is None else limit
    while remaining and len(selected) < limit:

        def key(item):
            similarity = max(
                (
                    dot(vectors[item["id"]], vectors[prior["id"]])
                    for prior in selected
                    if item["id"] in vectors
                    and prior["id"] in vectors
                    and len(vectors[item["id"]]) == len(vectors[prior["id"]])
                ),
                default=0,
            )
            return (-(lambda_ * item["score"] - (1 - lambda_) * similarity), item["id"])

        chosen = min(remaining, key=key)
        selected.append(chosen)
        remaining.remove(chosen)
    return selected


def unit_items(units, positions):
    return [
        dict(
            id=unit[0]["id"],
            owner=unit[0]["scope"],
            cost=len(unit),
            members=unit,
            score=max(1 / (1 + positions.get(member["id"], len(positions))) for member in unit),
        )
        for unit in units
    ]


def by_owner(items):
    result = defaultdict(list)
    for item in items:
        result[item["owner"]].append(item)
    return result


def fit_order(units, limits):
    """Reserve each kind's available owner quota, then keep spares for actual fit."""
    items = unit_items(units, {member["id"]: i for i, unit in enumerate(units) for member in unit})
    chosen = []
    for kind in ("facts", "episodes"):
        channel = [
            item
            for item in items
            if (item["members"][0]["kind"] == "episode") == (kind == "episodes")
        ]
        chosen.extend(quota_merge(by_owner(channel), limits[kind]))
    included = {item["id"] for item in chosen}
    return [
        item["members"] for item in chosen + [item for item in items if item["id"] not in included]
    ]
