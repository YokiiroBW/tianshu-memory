"""Transactional settlement and projection. The caller owns authorization/barriers."""

import json
import re

from ..domain import canonical, fingerprint, parse_time, require
from .policy import Policy
from .schema import installed
from .timestamps import stamp as utc


def identifier(value):
    return (
        type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None
    )


def pair_key(pair):
    require(type(pair) is dict and set(pair) == {"actor_id", "person_id"}, "invalid_input", 400)
    require(all(identifier(v) for v in pair.values()), "invalid_input", 400)
    return pair["actor_id"], pair["person_id"]


def get_pair(db, pair, clock, policy=Policy()):
    require(installed(db), "dependency_unavailable", 503)
    actor, person = pair_key(pair)
    require(db.execute("SELECT 1 FROM people WHERE id=?", (person,)).fetchone() is not None)
    db.execute(
        "INSERT OR IGNORE INTO relationship_pairs VALUES (?,?, 'unspecified','',0,?,0,NULL,1,?,?,0,?,?,1)",
        (
            actor,
            person,
            policy.stage(0),
            canonical(policy.__dict__),
            utc(clock),
            utc(clock),
            utc(clock),
        ),
    )
    return dict(
        db.execute(
            "SELECT * FROM relationship_pairs WHERE actor_id=? AND person_id=?", (actor, person)
        ).fetchone()
    )


def saved_policy(row):
    config = json.loads(row["policy"])
    config["boundaries"] = tuple(config["boundaries"])
    config["decay_rates"] = tuple(config["decay_rates"])
    return Policy(**config)


def save(db, row):
    db.execute(
        "UPDATE relationship_pairs SET relationship_type=?,display_label=?,score=?,stage=?,frozen=?,"
        "frozen_since=?,version=?,policy=?,activity_at=?,decay_days=?,decay_cursor=?,clock_head=?,ready=? "
        "WHERE actor_id=? AND person_id=?",
        tuple(
            row[k]
            for k in (
                "relationship_type",
                "display_label",
                "score",
                "stage",
                "frozen",
                "frozen_since",
                "version",
                "policy",
                "activity_at",
                "decay_days",
                "decay_cursor",
                "clock_head",
                "ready",
                "actor_id",
                "person_id",
            )
        ),
    )


def moment(row, clock):
    require(clock.tzinfo is not None and clock.utcoffset() is not None, "invalid_input", 400)
    return max(clock, parse_time(row["clock_head"]))


def event(db, row, *, event_id, payload, kind, delta, outcome, clock, budget_day=None, sources=()):
    digest = fingerprint(payload)
    old = db.execute(
        "SELECT digest,result FROM relationship_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if old:
        require(old["digest"] == digest, "idempotency_conflict", 409)
        return json.loads(old["result"])
    policy = saved_policy(row)
    actual = policy.clamp(row["score"] + delta) - row["score"]
    row["score"] += actual
    row["stage"] = policy.stage(row["score"], row["stage"])
    row["version"] += 1
    row["clock_head"] = utc(clock)
    result = {
        "event_id": event_id,
        "pair": {"actor_id": row["actor_id"], "person_id": row["person_id"]},
        "outcome": outcome,
        "version": row["version"],
        "applied_delta": actual,
        "settled_at": utc(clock),
    }
    db.execute(
        "INSERT INTO relationship_events VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (
            event_id,
            digest,
            row["actor_id"],
            row["person_id"],
            kind,
            actual,
            outcome,
            utc(clock),
            budget_day,
            canonical(result),
        ),
    )
    for source in sources:
        db.execute(
            "INSERT INTO relationship_sources VALUES (?,?,?,?,?)",
            (event_id, source["key"], source["revision"], source["epoch"], source["scope"]),
        )
    save(db, row)
    return result


def tick(db, row, clock):
    clock = moment(row, clock)
    if utc(clock) != row["clock_head"]:
        row["clock_head"] = utc(clock)
        save(db, row)
    if row["frozen"]:
        return
    policy = saved_policy(row)
    days = max(0, (clock - parse_time(row["activity_at"])).days)
    if days <= row["decay_days"]:
        return
    loss = min(max(0, row["score"]), policy.decay(days, row["decay_days"]))
    event(
        db,
        row,
        event_id="decay:"
        + fingerprint([row["actor_id"], row["person_id"], row["activity_at"], days]),
        payload={"anchor": row["activity_at"], "days": days},
        kind="natural_decay",
        delta=-loss,
        outcome="accepted" if loss else "no_change",
        clock=clock,
    )
    row.update(decay_days=days, decay_cursor=utc(clock))
    save(db, row)


def settle(db, row, payload, requested, clock, sources, *, group=False):
    clock = moment(row, clock)
    old = db.execute(
        "SELECT digest,result FROM relationship_events WHERE event_id=?", (payload["event_id"],)
    ).fetchone()
    if old:
        require(old["digest"] == fingerprint(payload), "idempotency_conflict", 409)
        return json.loads(old["result"])
    require(row["ready"] == 1, "migration_pending", 409)
    tick(db, row, clock)
    policy = saved_policy(row)
    day = policy.day(clock)
    occurred = payload.get("occurred_at")
    was_frozen = (
        occurred is not None
        and db.execute(
            "SELECT 1 FROM relationship_freezes WHERE actor_id=? AND person_id=? "
            "AND started_at<=? AND (ended_at IS NULL OR ended_at>?) LIMIT 1",
            (row["actor_id"], row["person_id"], occurred, occurred),
        ).fetchone()
        is not None
    )
    if row["frozen"] or was_frozen:
        outcome, delta = "rejected_frozen", 0
    elif group and not policy.group_growth:
        outcome, delta = "no_change", 0
    elif any(
        db.execute(
            "SELECT 1 FROM relationship_sources s JOIN relationship_events e ON e.event_id=s.event_id "
            "WHERE e.actor_id=? AND e.person_id=? AND e.kind IN ('automatic','legacy_import') "
            "AND s.source_key=? AND s.revision=? AND s.epoch=? LIMIT 1",
            (row["actor_id"], row["person_id"], source["key"], source["revision"], source["epoch"]),
        ).fetchone()
        is not None
        for source in sources
    ):
        outcome, delta = "no_change", 0
    else:
        delta = max(-policy.negative_limit, min(policy.positive_limit, requested))
        used = db.execute(
            "SELECT COALESCE(SUM(MAX(delta,0)),0) FROM relationship_events WHERE actor_id=? AND person_id=? AND budget_day=? AND kind='automatic'",
            (row["actor_id"], row["person_id"], day),
        ).fetchone()[0]
        if delta > 0:
            delta = min(delta, max(0, policy.daily_positive_limit - used))
        outcome = "accepted" if delta else "rejected_budget"
        row.update(activity_at=utc(clock), decay_days=0, decay_cursor=utc(clock))
    return event(
        db,
        row,
        event_id=payload["event_id"],
        payload=payload,
        kind="automatic",
        delta=delta,
        outcome=outcome,
        clock=clock,
        budget_day=day,
        sources=sources,
    )


def projection(row, clock):
    require(row["ready"] == 1, "migration_pending", 409)
    return {
        "view": "private",
        "pair": {"actor_id": row["actor_id"], "person_id": row["person_id"]},
        "version": row["version"],
        "policy_version": saved_policy(row).version,
        "relationship_type": row["relationship_type"],
        "display_label": row["display_label"],
        "score": row["score"],
        "stage": row["stage"],
        "frozen": bool(row["frozen"]),
        "frozen_since": row["frozen_since"],
        "decay_cursor": row["decay_cursor"],
        "checked_at": utc(clock),
    }


def reconcile(service, db, row, clock):
    """Privacy correction is distinct from interactions and ignores affinity freeze."""
    invalid = []
    for recorded in db.execute(
        "SELECT e.event_id,e.delta,s.* FROM relationship_events e JOIN relationship_sources s ON s.event_id=e.event_id WHERE e.actor_id=? AND e.person_id=? AND e.valid=1",
        (row["actor_id"], row["person_id"]),
    ):
        current = db.execute(
            "SELECT * FROM sources WHERE key=?", (recorded["source_key"],)
        ).fetchone()
        valid = (
            current is not None
            and current["state"] == "active"
            and current["revision"] == recorded["revision"]
            and current["epoch"] == recorded["epoch"]
        )
        if valid and service.synchronized:
            valid = (
                db.execute(
                    "SELECT 1 FROM source_admissions WHERE key=? AND verified=1", (current["key"],)
                ).fetchone()
                is not None
                and db.execute(
                    "SELECT 1 FROM suppression WHERE source_key=?", (current["key"],)
                ).fetchone()
                is None
            )
        if not valid:
            invalid.append(recorded["event_id"])
    for event_id in sorted(set(invalid)):
        old = db.execute(
            "SELECT delta FROM relationship_events WHERE event_id=? AND valid=1", (event_id,)
        ).fetchone()
        if old is None:
            continue
        db.execute("UPDATE relationship_events SET valid=0 WHERE event_id=?", (event_id,))
        # Rebuild the score from retained valid applied entries. Decay never makes
        # a removed positive contribution turn into a negative relationship.
        policy = saved_policy(row)
        score = 0
        for item in db.execute(
            "SELECT kind,delta FROM relationship_events WHERE actor_id=? AND person_id=? AND valid=1 AND kind!='source_correction' ORDER BY settled_at,rowid",
            (row["actor_id"], row["person_id"]),
        ):
            if item["kind"] == "natural_decay":
                score -= min(max(0, score), -item["delta"])
            else:
                score = policy.clamp(score + item["delta"])
        event(
            db,
            row,
            event_id="invalidation:" + fingerprint(event_id),
            payload={"invalidated": event_id},
            kind="source_correction",
            delta=score - row["score"],
            outcome="accepted",
            clock=moment(row, clock),
        )
