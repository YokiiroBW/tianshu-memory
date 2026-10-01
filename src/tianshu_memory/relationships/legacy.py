"""Retained legacy increments, explicit provenance, and a single settlement adapter."""

import json

from ..domain import Fault, canonical, fingerprint, parse_time, require
from .ledger import event, get_pair, pair_key, projection, reconcile, settle, tick
from .policy import Policy
from .schema import installed
from .timestamps import stamp


def mapping(db, group_id):
    group = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    try:
        scope = json.loads(group["scope"])
        require(
            set(scope) == {"actor_id", "person_id", "audience", "conversation_id"},
            "migration_pending",
            409,
        )
        pair = {k: scope[k] for k in ("actor_id", "person_id")}
        pair_key(pair)
        require(all(type(v) is str and v for v in pair.values()), "migration_pending", 409)
        require(
            db.execute("SELECT 1 FROM people WHERE id=?", (pair["person_id"],)).fetchone()
            is not None,
            "migration_pending",
            409,
        )
        require(
            scope.get("audience") in {"self_private", "group"}
            and type(scope.get("conversation_id")) is str,
            "migration_pending",
            409,
        )
        sources = []
        for edge in db.execute(
            "SELECT s.*,l.revision AS required_revision,l.epoch AS required_epoch FROM lineage l JOIN sources s ON s.key=l.source_key WHERE l.group_id=?",
            (group_id,),
        ):
            source_scope = json.loads(edge["scope"])
            require(all(source_scope.get(k) == pair[k] for k in pair), "migration_pending", 409)
            sources.append(dict(edge))
        require(bool(sources), "migration_pending", 409)
        return pair, scope, sources, group["state"]
    except (KeyError, TypeError, ValueError, Fault):
        return None


def import_legacy(db, clock, policy=Policy()):
    entries = db.execute(
        "SELECT e.group_id,e.amount FROM relationship_entries e LEFT JOIN relationship_legacy l ON l.group_id=e.group_id WHERE l.group_id IS NULL ORDER BY e.group_id"
    ).fetchall()
    grouped = {}
    for entry in entries:
        mapped = mapping(db, entry["group_id"])
        if mapped is None:
            db.execute(
                "INSERT INTO relationship_legacy VALUES (?,NULL,NULL,?,'pending','ambiguous_scope_or_lineage')",
                (entry["group_id"], entry["amount"]),
            )
            continue
        pair, scope, sources, state = mapped
        key = (pair["actor_id"], pair["person_id"])
        grouped.setdefault(key, []).append((entry, pair, scope, sources, state))
    for _, values in grouped.items():
        row = get_pair(db, values[0][1], clock, policy)
        # Existing different visibility/conversation scores cannot be silently merged.
        scopes = {canonical(v[2]) for v in values}
        total = sum(
            v[0]["amount"]
            for v in values
            if v[4] == "active"
            and all(
                s["state"] == "active"
                and s["revision"] == s["required_revision"]
                and s["epoch"] == s["required_epoch"]
                for s in v[3]
            )
        )
        pending = len(scopes) != 1 or not policy.minimum <= row["score"] + total <= policy.maximum
        if pending:
            from .ledger import save

            row["ready"] = 0
            save(db, row)
        for entry, pair, scope, sources, state in values:
            db.execute(
                "INSERT INTO relationship_legacy VALUES (?,?,?,?,?,?)",
                (
                    entry["group_id"],
                    pair["actor_id"],
                    pair["person_id"],
                    entry["amount"],
                    "pending" if pending else "imported",
                    "multiple_scopes_or_out_of_range" if pending else "retained_provenance",
                ),
            )
            if pending:
                continue
            valid = state == "active" and all(
                s["state"] == "active"
                and s["revision"] == s["required_revision"]
                and s["epoch"] == s["required_epoch"]
                for s in sources
            )
            event(
                db,
                row,
                event_id="legacy:" + fingerprint(entry["group_id"]),
                payload={"group_id": entry["group_id"], "amount": entry["amount"]},
                kind="legacy_import",
                delta=entry["amount"] if valid else 0,
                outcome="accepted" if valid else "no_change",
                clock=clock,
                sources=sources,
            )
    return migration_report(db)


def migration_report(db):
    counts = {
        row["state"]: row["n"]
        for row in db.execute("SELECT state,COUNT(*) AS n FROM relationship_legacy GROUP BY state")
    }
    pending = [
        dict(row)
        for row in db.execute(
            "SELECT group_id,actor_id,person_id,amount,reason FROM relationship_legacy WHERE state='pending' ORDER BY group_id LIMIT 50"
        )
    ]
    return {
        "imported": counts.get("imported", 0),
        "pending": counts.get("pending", 0),
        "pending_items": pending,
    }


def route_legacy(service, db, group_id, scope, delta, committed):
    """Called only inside the existing approved workflow transaction."""
    if not installed(db):
        return
    row = get_pair(
        db,
        {k: scope[k] for k in ("actor_id", "person_id")},
        service.clock(),
        getattr(getattr(service, "relationships", None), "policy", Policy()),
    )
    mapped = mapping(db, group_id)
    if mapped is None or not row["ready"]:
        db.execute(
            "INSERT OR IGNORE INTO relationship_legacy VALUES (?,?,?,?, 'pending','needs_explicit_mapping')",
            (group_id, scope["actor_id"], scope["person_id"], delta),
        )
        return
    _, _, sources, _ = mapped
    reconcile(service, db, row, service.clock())
    require(
        committed["reality"] == "real"
        and committed["delivery_state"] == "sent"
        and bool(committed["reply_ids"]),
        "invalid_source",
        409,
    )
    occurred = parse_time(committed["occurred_at"])
    require(occurred.tzinfo is not None and occurred <= service.clock(), "invalid_source", 409)
    settle(
        db,
        row,
        {
            "event_id": "legacy:" + fingerprint(group_id),
            "group_id": group_id,
            "requested_delta": delta,
            "occurred_at": stamp(occurred),
        },
        delta,
        service.clock(),
        sources,
        group=scope["audience"] == "group",
    )
    db.execute(
        "INSERT OR IGNORE INTO relationship_legacy VALUES (?,?,?,?, 'imported','workflow_routed_once')",
        (group_id, scope["actor_id"], scope["person_id"], delta),
    )


def value(service, scope):
    """Compatibility for an already-trusted internal workflow, never a new public port."""
    with service.store.transaction() as db:
        if not installed(db):
            return None
    from .application import Relationships

    application = getattr(service, "relationships", None) or Relationships(service)
    with application.operation(scope) as db:
        if scope["audience"] != "self_private":
            raise Fault("private_relationship_projection", 403)
        row = get_pair(
            db,
            {k: scope[k] for k in ("actor_id", "person_id")},
            service.clock(),
            application.policy,
        )
        if not row["ready"]:
            # Preserve old per-scope values while the global pair cannot yet be mapped.
            return sum(
                r["amount"]
                for r in db.execute(
                    "SELECT e.* FROM relationship_entries e JOIN groups g ON g.id=e.group_id WHERE g.scope=? AND g.state='active'",
                    (canonical(scope),),
                )
                if service._group_current(db, r["group_id"])
            )
        reconcile(service, db, row, service.clock())
        tick(db, row, service.clock())
        return projection(row, service.clock())["score"]
