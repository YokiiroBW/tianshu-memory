"""Source closure for canonical updates, using the existing lineage and evidence ledgers."""

import json

from .domain import canonical, require

DERIVED_UPDATES = frozenset({"retract", "confirm"})


def independent_basis(service, draft, source_map):
    return {
        (key, source_map[key]["revision"], source_map[key]["epoch"])
        for source in draft["units"][0]["sources"]
        for key in [service.source_identity(source, draft["scope"])]
    }


def reusable(db, rows, operation, basis):
    """A semantic duplicate cannot combine independent and inherited source requirements."""
    if operation in DERIVED_UPDATES:
        return []
    eligible = []
    for row in rows:
        prior = json.loads(row["payload"])
        if prior.get("canonical") and prior["update"]["operation"] not in DERIVED_UPDATES:
            eligible.append(row)
    lineage = {row["group_id"]: set() for row in eligible}
    identities = list(lineage)
    for offset in range(0, len(identities), 256):
        batch = identities[offset : offset + 256]
        for item in db.execute(
            "SELECT group_id,source_key,revision,epoch FROM lineage WHERE group_id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ):
            lineage[item["group_id"]].add((item["source_key"], item["revision"], item["epoch"]))
    return [row for row in eligible if lineage[row["group_id"]] == basis]


def target_evidence(db, draft, selected, source_map):
    """The targets have already passed scope, version and current-source checks."""
    if draft["fact"]["update"]["operation"] not in DERIVED_UPDATES:
        return draft, source_map, None
    identities = [row["group_id"] for row, _ in selected]
    require(bool(identities), "invalid_input", 400)
    placeholders = ",".join("?" for _ in identities)
    sources = {}
    for row in db.execute(
        "SELECT l.revision AS required_revision,l.epoch AS required_epoch,s.* "
        "FROM lineage l JOIN sources s ON s.key=l.source_key WHERE l.group_id IN ("
        + placeholders
        + ")",
        identities,
    ):
        require(
            row["scope"] == canonical(draft["scope"])
            and row["state"] == "active"
            and row["revision"] == row["required_revision"]
            and row["epoch"] == row["required_epoch"],
            "version_conflict",
            409,
        )
        sources[row["key"]] = dict(row)
    require(bool(sources), "version_conflict", 409)
    quotes = [
        dict(row)
        for row in db.execute(
            "SELECT DISTINCT source_key,quote,source_text FROM automatic_evidence WHERE group_id IN ("
            + placeholders
            + ")",
            identities,
        )
    ]
    require(all(row["source_key"] in sources for row in quotes), "version_conflict", 409)
    unit = draft["units"][0]
    portable = {canonical(source): source for source in unit["sources"]}
    for row in sources.values():
        source = json.loads(row["payload"])
        if len(portable) < 16:
            portable[canonical(source)] = source
    # Portable references retain their established bound; lineage contains the
    # complete flattened closure, including dependencies of a prior update.
    return (
        dict(draft, units=[dict(unit, sources=list(portable.values()))]),
        {**source_map, **sources},
        (sources, quotes),
    )


def bind(db, group_id, closure):
    if closure is None:
        return
    sources, quotes = closure
    db.executemany(
        "INSERT OR IGNORE INTO lineage VALUES (?,?,?,?)",
        [(group_id, key, row["revision"], row["epoch"]) for key, row in sources.items()],
    )
    db.executemany(
        "INSERT OR IGNORE INTO automatic_evidence(group_id,source_key,quote,source_text) VALUES (?,?,?,?)",
        [(group_id, row["source_key"], row["quote"], row["source_text"]) for row in quotes],
    )
