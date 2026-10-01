"""Private, bounded management history from the single Memory ledger."""

import json

from ..domain import fingerprint, require
from .ledger import get_pair, moment, projection, reconcile, tick


def read(app, pair, *, authorization, assertion_ref, request_id):
    context, _ = app.operator(pair, authorization, assertion_ref, request_id)
    require(context["allowed_scope"]["audience"] == "self_private")
    with app.operation(app.target_scope(pair, context)) as db:
        app.service._authorize(db, context)
        row = get_pair(db, pair, app.service.clock(), app.policy)
        reconcile(app.service, db, row, app.service.clock())
        tick(db, row, app.service.clock())
        records = db.execute(
            "SELECT event_id,kind,delta,outcome,settled_at,result,valid "
            "FROM relationship_events WHERE actor_id=? AND person_id=? "
            "ORDER BY settled_at DESC,event_id DESC LIMIT 21",
            (pair["actor_id"], pair["person_id"]),
        ).fetchall()
        items = []
        for event in records[:20]:
            audit = json.loads(event["result"]).get("audit", {})
            items.append(
                {
                    "id": fingerprint(event["event_id"]),
                    "kind": event["kind"],
                    "delta": event["delta"],
                    "outcome": event["outcome"],
                    "at": event["settled_at"],
                    "valid": bool(event["valid"]),
                    "operation": audit.get("operation"),
                    "reason": audit.get("reason"),
                }
            )
        return {
            "projection": projection(row, moment(row, app.service.clock())),
            "items": items,
            "has_more": len(records) > 20,
        }
