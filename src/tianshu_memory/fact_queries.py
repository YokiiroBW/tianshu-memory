"""Transaction-local batched fact metadata and source-grounded name views."""

import json
from dataclasses import dataclass

from .fact_migration import installed
from .postgres import GROUP_READ_BATCH, is_postgres


@dataclass(frozen=True)
class FactSnapshot:
    installed: bool
    rows: dict
    decoded: dict
    names: object = None

    @classmethod
    def from_rows(cls, rows, *, service=None, db=None, candidates=(), domain_complete=None):
        """Decode only rows retained after complete-group/projection validation."""
        rows = dict(rows)
        names = None
        if service is not None:
            from .fact_names import load

            names = load(service, db, candidates, rows, domain_complete=domain_complete)
        return cls(
            True,
            rows,
            {identity: json.loads(row["payload"]) for identity, row in rows.items()},
            names,
        )


def snapshot_for_groups(db, candidates, *, service=None, domain_complete=None):
    """Capture once for filtering/ranking inside one select transaction only."""
    if not is_postgres(db) and service is None:
        return None
    if not installed(db):
        return FactSnapshot(False, {}, {})
    rows = {}
    for sql, parameters in group_queries([entry[0]["id"] for entry in candidates]):
        rows.update((row["group_id"], row) for row in db.execute(sql, parameters))
    return FactSnapshot.from_rows(
        rows, service=service, db=db, candidates=candidates, domain_complete=domain_complete
    )


def group_queries(keys):
    """The same bounded fact reads, independently queueable after source approval."""
    for offset in range(0, len(keys), GROUP_READ_BATCH):
        batch = keys[offset : offset + GROUP_READ_BATCH]
        yield (
            "SELECT * FROM automatic_facts WHERE group_id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        )


def for_groups(db, candidates):
    # SQLite callers retain the existing lazy indexed lookups. A remote backend
    # reads the same complete candidate set in bounded parameter batches.
    if not is_postgres(db):
        return None
    keys = [entry[0]["id"] for entry in candidates]
    result = {}
    for sql, parameters in group_queries(keys):
        result.update((row["group_id"], row) for row in db.execute(sql, parameters))
    return result


def families(db, keys, limit):
    """Read limit+1 per exact scope/identity, preserving the overflow refusal."""
    result = {}
    from .fact_names import KEY, KEYS

    keys = list(keys)
    read = db.native if is_postgres(db) else db.execute
    ordinary = [key for key in keys if key[1] != KEY]
    for offset in range(0, len(ordinary), 128):
        batch = ordinary[offset : offset + 128]
        rows = read(
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
    # Cross-key links arise only from a normal, source-verified name correction.
    # Keep one family bound across all finite legacy spellings, not one per key.
    name_scopes = {scope for scope, key in keys if key == KEY}
    for offset in range(0, len(name_scopes), 128):
        batch = sorted(name_scopes)[offset : offset + 128]
        rows = read(
            "SELECT group_id,superseded_by,scope,fact_key FROM ("
            "SELECT group_id,superseded_by,scope,fact_key,"
            "row_number() OVER (PARTITION BY scope ORDER BY group_id) AS position "
            "FROM automatic_facts WHERE scope IN (" + ",".join("?" for _ in batch) + ") "
            "AND fact_key IN (?,?,?,?)) AS family WHERE position<=?",
            [*batch, *KEYS, limit + 1],
        )
        for scope in batch:
            result[scope, KEY] = []
        for row in rows:
            result[row["scope"], KEY].append(row)
    return result
