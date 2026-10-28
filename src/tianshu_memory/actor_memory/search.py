"""Rebuildable actor-only lexical index; no person store or model dependency."""

import json

from ..domain import terms

DDL = (
    "CREATE TABLE actor_search_terms (actor_id TEXT NOT NULL,kind TEXT NOT NULL,memory_id TEXT NOT NULL,term TEXT NOT NULL,PRIMARY KEY(actor_id,kind,memory_id,term))",
    "CREATE INDEX actor_search_lookup ON actor_search_terms(actor_id,term,kind,memory_id)",
)


def remove(db, kind, memory_id):
    db.execute("DELETE FROM actor_search_terms WHERE kind=? AND memory_id=?", (kind, memory_id))


def index(db, actor_id, kind, memory_id, text):
    remove(db, kind, memory_id)
    for term in terms(text):
        db.execute(
            "INSERT INTO actor_search_terms VALUES (?,?,?,?)",
            (actor_id, kind, memory_id, term),
        )


def rebuild(db):
    db.execute("DELETE FROM actor_search_terms")
    for kind, table, column in (
        ("episode", "actor_episodes", "fact"),
        ("derived", "actor_derived", "payload"),
    ):
        for row in db.execute("SELECT * FROM " + table + " WHERE state='active'"):
            payload = json.loads(row[column])
            index(
                db,
                row["actor_id"],
                kind,
                row["id"],
                payload.get("summary", payload.get("text", "")),
            )


def _audience(db, column, context):
    audience = context["audience"]
    if audience["kind"] == "actor_only":
        return "1=1", []

    def field(name):
        if hasattr(db, "native"):
            return "(" + column + "::jsonb->'source_constraints'->>'" + name + "')"
        return "json_extract(" + column + ",'$.source_constraints." + name + "')"

    predicate = field("kind") + "=?"
    parameters = [audience["kind"]]
    for name in ("person_id", "conversation_id", "assertion_ref"):
        if name in audience:
            predicate += " AND (" + field(name) + " IS NULL OR " + field(name) + "=?)"
            parameters.append(audience[name])
        else:
            predicate += " AND " + field(name) + " IS NULL"
    return "(" + field("kind") + "='public' OR (" + predicate + "))", parameters


def candidates(db, actor_id, kind, words, context, now, kinds):
    if not words:
        return
    table = "actor_episodes" if kind == "episode" else "actor_derived"
    # Match before chronological ordering: an old relevant memory cannot disappear
    # merely because unrelated recent records filled a candidate window.
    allowed, constraints = _audience(db, "m.fact" if kind == "episode" else "e.fact", context)
    if kind == "episode":
        predicate = allowed
    else:
        predicate = (
            "m.expires_at>? AND EXISTS (SELECT 1 FROM actor_derived_basis b WHERE b.derived_id=m.id) AND NOT EXISTS (SELECT 1 FROM actor_derived_basis b LEFT JOIN actor_episodes e ON e.id=b.episode_id AND e.actor_id=m.actor_id WHERE b.derived_id=m.id AND (e.id IS NULL OR e.state!='active' OR e.revision!=b.episode_revision OR NOT ("
            + allowed
            + ")))"
        )
        constraints = [now, *constraints]
        field = (
            "(m.payload::jsonb->>'kind')"
            if hasattr(db, "native")
            else "json_extract(m.payload,'$.kind')"
        )
        predicate += " AND " + field + " IN (" + ",".join("?" for _ in kinds) + ")"
        constraints.extend(kinds)
    cursor = db.execute(
        "SELECT m.* FROM "
        + table
        + " m JOIN (SELECT memory_id,COUNT(*) AS hits FROM actor_search_terms WHERE actor_id=? AND kind=? AND term IN ("
        + ",".join("?" for _ in words)
        + ") GROUP BY memory_id) matches ON matches.memory_id=m.id WHERE m.actor_id=? AND m.state='active' AND ("
        + predicate
        + ") ORDER BY matches.hits DESC,m.recorded_at DESC,m.id DESC LIMIT 257",
        [actor_id, kind, *words, actor_id, *constraints],
    )
    yield from cursor
