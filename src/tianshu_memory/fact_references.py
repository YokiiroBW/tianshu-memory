"""Use existing, authorized replacement links as query hints, never as current values."""

import json

from .fact_queries import families as read_families
from .recall_language import topic_tokens

MAX_DEPTH = 16
MAX_FAMILY = 128


def expand_candidates(query_sql, parameters, scope):
    # The indexed lexical pass runs once. Follow only the persisted replacement
    # relation, with the exact same owner scope and property/entity identity.
    return (
        "WITH RECURSIVE matched(group_id) AS (" + query_sql + "), "
        "fact_chain(group_id,scope,fact_key,depth) AS ("
        "SELECT f.group_id,f.scope,f.fact_key,0 FROM automatic_facts f "
        "JOIN matched m ON m.group_id=f.group_id WHERE f.scope=? UNION ALL "
        "SELECT n.group_id,n.scope,n.fact_key,c.depth+1 FROM fact_chain c "
        "JOIN automatic_facts p ON p.group_id=c.group_id "
        "JOIN automatic_facts n ON n.group_id=p.superseded_by "
        "AND n.scope=c.scope AND n.fact_key=c.fact_key "
        f"WHERE c.depth<{MAX_DEPTH}) "
        "SELECT group_id FROM matched UNION SELECT group_id FROM fact_chain",
        [*parameters, scope],
    )


def reference_terms(service, db, eligible, indexed, *, facts=None, authorized_group_ids=None):
    """Validate old groups before any of their tokens affect current ranking.

    The caller has already verified the current scope and eligible output units.
    Nothing here changes statements, indexes, history validity or permissions.
    """
    families, checked, result = {}, {}, {}
    family_rows = (
        read_families(
            db,
            {
                (fact["scope"], fact["fact_key"])
                for group, _, _ in eligible
                if (fact := facts.get(group["id"])) is not None
                and fact["superseded_by"] is None
                and fact["scope"] == group["scope"]
            },
            MAX_FAMILY,
        )
        if facts is not None
        else None
    )

    def valid_terms(group_id, scope):
        key = group_id, scope
        if key in checked:
            return checked[key]
        checked[key] = None
        # Group readers may borrow only from original whole groups whose complete
        # lineage passed this operation's viewer/source barrier and bounded window.
        if authorized_group_ids is not None and group_id not in authorized_group_ids:
            return None
        group = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if (
            not group
            or group["scope"] != scope
            or group["state"] != "active"
            or not service._group_current(db, group_id)
        ):
            return None
        rows = db.execute("SELECT * FROM records WHERE group_id=?", (group_id,)).fetchall()
        if set(json.loads(group["members"])) != {r["id"] for r in rows} or any(
            r["state"] != "active" or (r["id"], r["version"]) not in indexed for r in rows
        ):
            return None
        units = [json.loads(r["payload"]) for r in rows]
        owner = json.loads(scope)
        if any(u["subject_person_id"] != owner["person_id"] for u in units):
            return None
        if owner["audience"] == "group" and any(
            u["visibility"] != "shared_projection"
            or any(s["kind"] != "shareable_projection" for s in u["sources"])
            for u in units
        ):
            return None
        if not service._projections_current(db, units):
            return None
        value = " ".join(indexed[r["id"], r["version"]] for r in rows)
        value += " " + " ".join(topic_tokens(" ".join(u["statement"] for u in units)))
        checked[key] = value
        return value

    for group, _, _ in eligible:
        fact = (
            db.execute("SELECT * FROM automatic_facts WHERE group_id=?", (group["id"],)).fetchone()
            if facts is None
            else facts.get(group["id"])
        )
        if fact is None or fact["superseded_by"] is not None or fact["scope"] != group["scope"]:
            continue
        key = fact["scope"], fact["fact_key"]
        if key not in families:
            rows = (
                db.execute(
                    "SELECT group_id,superseded_by FROM automatic_facts WHERE scope=? AND fact_key=? LIMIT ?",
                    (*key, MAX_FAMILY + 1),
                ).fetchall()
                if family_rows is None
                else family_rows.get(key, [])
            )
            reverse = {}
            if len(rows) <= MAX_FAMILY:
                for row in rows:
                    reverse.setdefault(row["superseded_by"], []).append(row["group_id"])
            families[key] = reverse
        stack, seen, hints = [(group["id"], 0)], {group["id"]}, []
        while stack:
            current, depth = stack.pop()
            if depth >= MAX_DEPTH:
                continue
            for old in families[key].get(current, []):
                if old in seen:
                    continue
                seen.add(old)
                value = valid_terms(old, group["scope"])
                if value is not None:
                    hints.append(value)
                    stack.append((old, depth + 1))
        if hints:
            result[group["id"]] = " ".join(hints)
    return result
