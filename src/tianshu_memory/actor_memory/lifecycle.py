"""Body erasure, evidence suppression and bounded deterministic maintenance."""

import json

from ..domain import canonical, fingerprint, require, utc
from . import search
from .facts import INTERNAL


def evidence(db, actor_id, episode_refs):
    """Stable original object identities; a new revision/self-retelling is not new evidence."""
    values = set()
    for ref in episode_refs:
        row = db.execute(
            "SELECT fact,state FROM actor_episodes WHERE actor_id=? AND id=?",
            (actor_id, ref["episode_id"]),
        ).fetchone()
        if row is None or row["state"] != "active":
            continue
        fact = json.loads(row["fact"])
        if fact.get("provenance") not in {"digital_action", "observation"}:
            continue
        # Content-bearing readings share original content identity. A repeated
        # summary, diary or reflection cannot count as an independent encounter.
        sources = [part["content_ref"] for part in fact.get("content_reads", [])]
        if not sources and fact.get("outcome") in {"completed", "observed"}:
            sources = [fact["source"]]
        values.update(fingerprint([part["producer"], part["object_id"]]) for part in sources)
    return values


def suppress(db, actor_id, payload):
    basis = sorted(evidence(db, actor_id, payload["episode_refs"]))
    # Also record exact episode identities, including non-digital material, to
    # suppress partial-basis and reworded retries even when no independent proof exists.
    episodes = sorted(part["episode_id"] for part in payload["episode_refs"])
    value = dict(actor_id=actor_id, kind=payload["kind"], evidence=basis, episodes=episodes)
    db.execute(
        "INSERT INTO actor_metadata VALUES (?,?) ON CONFLICT(key) DO NOTHING",
        ("forgotten:" + fingerprint(value), canonical(value)),
    )


def ensure_unsuppressed(db, actor_id, payload):
    episodes = {part["episode_id"] for part in payload["episode_refs"]}
    current = evidence(db, actor_id, payload["episode_refs"])
    for row in db.execute("SELECT value FROM actor_metadata WHERE key LIKE 'forgotten:%'"):
        old = json.loads(row[0])
        if old["actor_id"] != actor_id or old["kind"] != payload["kind"]:
            continue
        require(
            not episodes.issubset(set(old["episodes"])) and bool(current - set(old["evidence"])),
            "source_withdrawn",
            409,
        )


def erase_derived(db, row, *, state="withdrawn", suppression=True):
    payload = json.loads(row["payload"])
    if payload and suppression:
        suppress(db, row["actor_id"], payload)
    db.execute(
        "UPDATE actor_derived SET state=?,revision=revision+1,payload='{}' WHERE id=?",
        (state, row["id"]),
    )
    search.remove(db, "derived", row["id"])


def dependents(db, actor_id, ref):
    """Include replacement history: old corrected bodies must not survive forget."""
    rows = [
        dict(row) for row in db.execute("SELECT * FROM actor_derived WHERE actor_id=?", (actor_id,))
    ]
    if ref["kind"] == "episode":
        selected = {
            row[0]
            for row in db.execute(
                "SELECT derived_id FROM actor_derived_basis WHERE actor_id=? AND episode_id=?",
                (actor_id, ref["id"]),
            )
        }
    else:
        selected = {ref["id"]}
    edges = {
        row["id"]: {part["id"] for part in json.loads(row["payload"]).get("replaces", [])}
        for row in rows
    }
    while True:
        previous = set(selected)
        for key, targets in edges.items():
            if key in selected or targets & selected:
                selected.add(key)
                selected.update(targets)
        if selected == previous:
            break
    return [row for row in rows if row["id"] in selected and row["id"] != ref["id"]]


def forget(db, actor_id, ref):
    table = "actor_episodes" if ref["kind"] == "episode" else "actor_derived"
    row = db.execute(
        "SELECT * FROM " + table + " WHERE actor_id=? AND id=?", (actor_id, ref["id"])
    ).fetchone()
    require(row is not None, "not_found", 404)
    require(row["revision"] == ref["revision"], "version_conflict", 409)
    related = dependents(db, actor_id, ref)
    for derived in related:
        erase_derived(db, derived)
    if ref["kind"] == "derived":
        erase_derived(db, row)
    else:
        db.execute(
            "UPDATE actor_episodes SET state='withdrawn',revision=revision+1,fact='{}' WHERE id=?",
            (row["id"],),
        )
        search.remove(db, "episode", row["id"])
    return {**ref, "revision": row["revision"] + 1}


def maintain(app, request, actor, caller, recheck):
    app._refresh(request, {}, [], INTERNAL, actor, recheck, caller)
    with app.store.transaction() as db:
        expired = compressed = 0
        pending = []
        seen = set()
        # Expiry is selected before LIMIT, so a long-lived recent record cannot
        # starve older expired records. Compression visits a bounded rotating page.
        expired_rows = list(
            db.execute(
                "SELECT * FROM actor_derived WHERE actor_id=? AND state IN ('active','superseded') AND payload!='{}' AND expires_at<=? ORDER BY expires_at,id LIMIT ?",
                (request["actor_id"], utc(app.clock()), request["limit"] + 1),
            )
        )
        pending.extend((dict(row), "expired") for row in expired_rows)
        cursor_key = "maintenance:" + request["actor_id"]
        marker = db.execute(
            "SELECT value FROM actor_metadata WHERE key=?", (cursor_key,)
        ).fetchone()
        after = marker[0] if marker else ""
        page = list(
            db.execute(
                "SELECT * FROM actor_derived WHERE actor_id=? AND state='active' AND expires_at>? AND id>? ORDER BY id LIMIT 257",
                (request["actor_id"], utc(app.clock()), after),
            )
        )
        for row in page[:256]:
            payload = json.loads(row["payload"])
            signature = fingerprint(
                {
                    key: payload.get(key)
                    for key in (
                        "kind",
                        "text",
                        "episode_refs",
                        "interest",
                        "importance",
                        "retention",
                    )
                }
            )
            if signature in seen:
                pending.append((dict(row), "superseded"))
            seen.add(signature)
        db.execute(
            "INSERT INTO actor_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cursor_key, page[255]["id"] if len(page) > 256 else ""),
        )
        for row, state in pending[: request["limit"]]:
            erase_derived(db, row, state=state, suppression=False)
            expired += state == "expired"
            compressed += state == "superseded"
        if expired or compressed:
            app.store.record_change(db)
        result = dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            command="maintain",
            idempotency_key=request["idempotency_key"],
            state="accepted",
            domain_version=app.store.version(db),
            expired_count=expired,
            compressed_count=compressed,
            remaining=len(pending) > request["limit"] or len(page) > 256,
        )
        return app._record_receipt(db, caller, request, result, "maintenance_receipt")
