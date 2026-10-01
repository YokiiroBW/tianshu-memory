"""Bounded application ports with existing identity, source, and operator checks."""

import json
from contextlib import contextmanager

from ..domain import Fault, canonical, fingerprint, parse_time, require
from .ledger import (
    event,
    get_pair,
    identifier,
    moment,
    pair_key,
    projection,
    reconcile,
    settle,
    tick,
)
from .policy import Policy
from .schema import installed
from .timestamps import stamp as utc


class Relationships:
    def __init__(self, service, *, auth=None, policy=Policy(), behavior_verifier=None):
        self.service, self.auth, self.policy = service, auth, policy
        # Only a trusted same-product rule adapter may attest a boundary/repair kind.
        # No default model-output or request-body verifier is installed.
        self.behavior_verifier = behavior_verifier

    @contextmanager
    def operation(self, scope, *, context=None, **kwargs):
        """Refresh all scopes contributing to a pair before returning a private score.

        Scope count is bounded. A global owner-head change between scopes retries;
        an unverifiable scope never falls back to a cached relationship score.
        """
        service = self.service
        if not service.synchronized:
            with service.operation(scope=scope, **kwargs) as db:
                if context is not None:
                    service._authorize(db, context, scope=scope)
                yield db
            return
        for _ in range(3):
            with service.store.transaction() as db:
                require(installed(db), "dependency_unavailable", 503)
                if context is not None:
                    service._authorize(db, context, scope=scope)
                scopes = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT DISTINCT s.scope FROM relationship_sources s JOIN relationship_events e "
                        "ON e.event_id=s.event_id WHERE e.actor_id=? AND e.person_id=?",
                        (scope["actor_id"], scope["person_id"]),
                    )
                ]
            require(len(scopes) <= 32, "relationship_scope_budget", 429)
            heads = None
            drift = False
            for other in sorted(scopes, key=canonical):
                if other == scope:
                    continue
                with service.operation(scope=other) as db:
                    observed = list(
                        db.execute(
                            "SELECT owner,generation,sequence FROM owner_heads ORDER BY owner"
                        )
                    )
                    observed = [tuple(r) for r in observed]
                if heads is not None and heads != observed:
                    drift = True
                heads = observed
            if drift:
                continue
            with service.operation(scope=scope, context=context, **kwargs) as db:
                observed = [
                    tuple(r)
                    for r in db.execute(
                        "SELECT owner,generation,sequence FROM owner_heads ORDER BY owner"
                    )
                ]
                if heads is not None and heads != observed:
                    continue
                yield db
                return
        raise Fault("dependency_unavailable", 503)

    def read(self, scope, context):
        # An ordinary group view never even reads the private affinity row/version.
        if scope["audience"] != "self_private":
            with self.service.operation(scope=scope, context=context) as db:
                require(installed(db), "dependency_unavailable", 503)
                self.service._authorize(db, context, scope=scope)
            return {
                "view": "public",
                "pair": {k: scope[k] for k in ("actor_id", "person_id")},
                "version": 1,
                "policy_version": self.policy.version,
                "expression_hint": "自然回应，不谈私聊关系细节。",
                "checked_at": utc(self.service.clock()),
            }
        with self.operation(scope, context=context) as db:
            row = get_pair(
                db,
                {k: scope[k] for k in ("actor_id", "person_id")},
                self.service.clock(),
                self.policy,
            )
            reconcile(self.service, db, row, self.service.clock())
            tick(db, row, self.service.clock())
            return projection(row, moment(row, self.service.clock()))

    @staticmethod
    def check_projection(current, expected_version):
        require(type(expected_version) is int and expected_version >= 0, "invalid_input", 400)
        require(current["version"] == expected_version, "version_conflict", 409)
        return {"version": current["version"], "current": True}

    def check(self, scope, context, expected_version):
        return self.check_projection(self.read(scope, context), expected_version)

    def operator(self, pair, authorization, assertion_ref, request_id):
        require(self.auth is not None, "dependency_unavailable", 503)
        pair_key(pair)
        name, caller = self.auth.authenticate(authorization)
        require(
            "relationships.manage" in caller.get("operations", [])
            and caller.get("role_admin") is True
        )
        context = self.auth.resolve(name, caller, assertion_ref, request_id)
        principal = context.get("principal_id")
        require(context.get("issuer") == "platform" and type(principal) is str and bool(principal))
        require(context["allowed_scope"]["actor_id"] == pair["actor_id"])
        return context, principal

    def target_scope(self, pair, context):
        with self.service.store.transaction() as db:
            require(installed(db), "dependency_unavailable", 503)
            target = db.execute(
                "SELECT s.scope FROM relationship_sources s JOIN relationship_events e ON e.event_id=s.event_id WHERE e.actor_id=? AND e.person_id=? ORDER BY s.scope LIMIT 1",
                pair_key(pair),
            ).fetchone()
        return (
            json.loads(target[0]) if target is not None else dict(context["allowed_scope"], **pair)
        )

    def read_managed(self, pair, *, authorization, assertion_ref, request_id):
        context, _ = self.operator(pair, authorization, assertion_ref, request_id)
        with self.operation(self.target_scope(pair, context)) as db:
            self.service._authorize(db, context)
            row = get_pair(db, pair, self.service.clock(), self.policy)
            reconcile(self.service, db, row, self.service.clock())
            tick(db, row, self.service.clock())
            return projection(row, moment(row, self.service.clock()))

    def settle(self, candidate, scope, context):
        required = {
            "event_id",
            "pair",
            "kind",
            "turn_id",
            "source_ref",
            "source_revision",
            "occurred_at",
        }
        require(type(candidate) is dict and set(candidate) == required, "invalid_input", 400)
        pair_key(candidate["pair"])
        require(candidate["pair"] == {k: scope[k] for k in ("actor_id", "person_id")})
        require(
            candidate["kind"]
            in {"conversation_completed", "boundary_violation", "repair_acknowledged"},
            "invalid_input",
            400,
        )
        require(
            type(candidate["source_revision"]) is int and candidate["source_revision"] > 0,
            "invalid_input",
            400,
        )
        require(
            all(identifier(candidate[k]) for k in ("event_id", "turn_id", "source_ref")),
            "invalid_input",
            400,
        )
        with self.service.store.transaction() as db:
            stored = db.execute(
                "SELECT j.event FROM turn_inputs t JOIN jobs j ON j.id=json_extract(t.result,'$.candidate_job_ref') WHERE t.turn_id=? ORDER BY t.revision DESC LIMIT 1",
                (candidate["turn_id"],),
            ).fetchone()
        require(stored is not None, "dependency_unavailable", 503)
        committed = json.loads(stored[0])
        require(
            committed["scope"] == scope
            and committed["reality"] == "real"
            and committed["delivery_state"] == "sent"
            and bool(committed["reply_ids"]),
            "invalid_source",
            409,
        )
        require(type(candidate["occurred_at"]) is str, "invalid_input", 400)
        try:
            occurred = parse_time(candidate["occurred_at"])
        except ValueError:
            raise Fault("invalid_input", 400) from None
        require(
            occurred.tzinfo is not None and occurred.utcoffset() is not None, "invalid_input", 400
        )
        require(
            occurred == parse_time(committed["occurred_at"]) and occurred <= self.service.clock(),
            "invalid_source",
            409,
        )
        with self.operation(
            scope,
            context=context,
            sources=committed["sources"],
            check={"turn_id": candidate["turn_id"], "input_revision": committed["input_revision"]},
        ) as db:
            current = db.execute(
                "SELECT j.event FROM turn_inputs t JOIN jobs j ON j.id=json_extract(t.result,'$.candidate_job_ref') WHERE t.turn_id=? ORDER BY t.revision DESC LIMIT 1",
                (candidate["turn_id"],),
            ).fetchone()
            require(current is not None and current[0] == stored[0], "version_conflict", 409)
            require(
                committed["scope_version"] == self.service._scope_version(db, scope),
                "scope_changed",
                409,
            )
            sources = self.service._source_rows(db, committed["sources"], scope, reality="real")
            require(
                any(
                    r["key"] == candidate["source_ref"]
                    and r["revision"] == candidate["source_revision"]
                    for r in sources
                ),
                "invalid_source",
                409,
            )
            if candidate["kind"] != "conversation_completed":
                require(
                    self.behavior_verifier is not None
                    and self.behavior_verifier(db, candidate, committed) is True,
                    "unverified_behavior",
                    409,
                )
            row = get_pair(db, candidate["pair"], self.service.clock(), self.policy)
            reconcile(self.service, db, row, self.service.clock())
            delta = {
                "conversation_completed": 1,
                "boundary_violation": -4,
                "repair_acknowledged": 1,
            }[candidate["kind"]]
            normalized = dict(candidate, occurred_at=utc(parse_time(candidate["occurred_at"])))
            return settle(
                db,
                row,
                normalized,
                delta,
                self.service.clock(),
                sources,
                group=scope["audience"] == "group",
            )

    def manage(self, command, *, authorization, assertion_ref):
        require(self.auth is not None, "dependency_unavailable", 503)
        require(type(command) is dict, "invalid_input", 400)
        pair_key(command.get("pair"))
        operation = command.get("operation")
        keys = {
            "set_binding": {"relationship_type", "display_label"},
            "set_freeze": {"frozen"},
            "adjust_affinity": {"delta", "reason"},
        }
        require(operation in keys, "invalid_input", 400)
        require(
            set(command)
            <= {"operation", "pair", "request_id", "expected_version"} | keys[operation],
            "invalid_input",
            400,
        )
        require(
            identifier(command.get("request_id"))
            and type(command.get("expected_version")) is int
            and command["expected_version"] >= 0,
            "invalid_input",
            400,
        )
        if operation == "set_binding":
            require(
                command.get("relationship_type")
                in {"unspecified", "friend", "partner", "family", "custom"}
                and type(command.get("display_label", "")) is str
                and len(command.get("display_label", "")) <= 40,
                "invalid_input",
                400,
            )
        elif operation == "set_freeze":
            require(type(command.get("frozen")) is bool, "invalid_input", 400)
        else:
            require(
                type(command.get("delta")) is int
                and -100 <= command["delta"] <= 100
                and type(command.get("reason")) is str
                and 0 < len(command["reason"]) <= 200,
                "invalid_input",
                400,
            )
        context, principal = self.operator(
            command["pair"], authorization, assertion_ref, command["request_id"]
        )
        target_scope = self.target_scope(command["pair"], context)
        with self.operation(target_scope) as db:
            self.service._authorize(db, context)
            row = get_pair(db, command["pair"], self.service.clock(), self.policy)
            reconcile(self.service, db, row, self.service.clock())
            key = (principal, command["request_id"])
            old = db.execute(
                "SELECT digest,result FROM relationship_commands WHERE operator=? AND request_id=?",
                key,
            ).fetchone()
            digest = fingerprint(command)
            if old:
                require(old["digest"] == digest, "idempotency_conflict", 409)
                return json.loads(old["result"])
            require(row["version"] == command["expected_version"], "version_conflict", 409)
            require(row["ready"] == 1, "migration_pending", 409)
            clock = moment(row, self.service.clock())
            tick(db, row, clock)
            if operation == "set_binding":
                row.update(
                    relationship_type=command["relationship_type"],
                    display_label=command.get("display_label", ""),
                )
            elif operation == "set_freeze":
                desired = command["frozen"]
                if desired != bool(row["frozen"]):
                    if desired:
                        db.execute(
                            "INSERT INTO relationship_freezes VALUES (?,?,?,?,NULL)",
                            (
                                fingerprint([principal, command["request_id"]]),
                                row["actor_id"],
                                row["person_id"],
                                utc(clock),
                            ),
                        )
                    else:
                        db.execute(
                            "UPDATE relationship_freezes SET ended_at=? WHERE actor_id=? AND person_id=? AND ended_at IS NULL",
                            (utc(clock), row["actor_id"], row["person_id"]),
                        )
                        row.update(activity_at=utc(clock), decay_days=0, decay_cursor=utc(clock))
                    row.update(frozen=int(desired), frozen_since=utc(clock) if desired else None)
            event(
                db,
                row,
                event_id="management:" + fingerprint(key),
                payload={"operator": principal, "command": command},
                kind="manual_adjustment" if operation == "adjust_affinity" else "management",
                delta=command["delta"] if operation == "adjust_affinity" else 0,
                outcome="accepted",
                clock=clock,
            )
            db.execute(
                "UPDATE relationship_events SET result=json_set(result,'$.audit',json(?)) WHERE event_id=?",
                (
                    canonical({"operation": operation, "reason": command.get("reason")}),
                    "management:" + fingerprint(key),
                ),
            )
            result = projection(row, clock)
            db.execute(
                "INSERT INTO relationship_commands VALUES (?,?,?,?,?,?)",
                (*key, digest, row["actor_id"], row["person_id"], canonical(result)),
            )
            return result
