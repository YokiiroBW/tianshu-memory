"""Transaction-local batched reads of automatic fact metadata on PostgreSQL."""

import json
from dataclasses import dataclass

from .fact_migration import installed
from .postgres import is_postgres


@dataclass(frozen=True)
class FactSnapshot:
    installed: bool
    rows: dict
    decoded: dict


def snapshot_for_groups(db, candidates):
    """Capture once for filtering/ranking inside one select transaction only."""
    if not is_postgres(db):
        return None
    if not installed(db):
        return FactSnapshot(False, {}, {})
    rows = for_groups(db, candidates)
    return FactSnapshot(
        True, rows, {identity: json.loads(row["payload"]) for identity, row in rows.items()}
    )


def for_groups(db, candidates):
    # SQLite callers retain the existing lazy indexed lookups. A remote backend
    # reads the same complete candidate set in bounded parameter batches.
    if not is_postgres(db):
        return None
    keys = [entry[0]["id"] for entry in candidates]
    result = {}
    for offset in range(0, len(keys), 128):
        batch = keys[offset : offset + 128]
        result.update(
            (row["group_id"], row)
            for row in db.execute(
                "SELECT * FROM automatic_facts WHERE group_id IN ("
                + ",".join("?" for _ in batch)
                + ")",
                batch,
            )
        )
    return result


def families(db, keys, limit):
    """Read limit+1 per exact scope/identity, preserving the overflow refusal."""
    result = {}
    keys = list(keys)
    for offset in range(0, len(keys), 128):
        batch = keys[offset : offset + 128]
        rows = db.native(
            "SELECT group_id,superseded_by,scope,fact_key FROM ("
            "SELECT group_id,superseded_by,scope,fact_key,"
            "row_number() OVER (PARTITION BY scope,fact_key ORDER BY group_id) AS position "
            "FROM automatic_facts WHERE (scope,fact_key) IN ("
            + ",".join("(?,?)" for _ in batch)
            + ")) AS family WHERE position<=?",
            [value for key in batch for value in key] + [limit + 1],
        )
        for row in rows:
            result.setdefault((row["scope"], row["fact_key"]), []).append(row)
    return result
