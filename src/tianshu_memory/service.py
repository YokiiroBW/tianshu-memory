import json
from contextlib import contextmanager
from datetime import timedelta

from .domain import (
    Fault,
    admission_key,
    canonical,
    fingerprint,
    new_id,
    now,
    parse_time,
    query_terms,
    require,
    semantic_request,
    source_key,
    utc,
)
from .qq_identity import validate_account

CATEGORIES = {"identity", "style", "relationship", "current_items", "evidence"}


class MemoryService:
    def __init__(
        self,
        store,
        contracts,
        *,
        clock=now,
        max_units=32,
        max_pending_jobs=256,
        source_authority=None,
    ):
        self.store, self.contracts, self.clock, self.max_units = store, contracts, clock, max_units
        self.source_authority = source_authority
        self.max_pending_jobs = max_pending_jobs

    @property
    def synchronized(self):
        from .source_authority import SourceAuthority

        return isinstance(self.source_authority, SourceAuthority)

    def source_identity(self, source, scope):
        return admission_key(source, scope) if self.synchronized else source_key(source)

    @contextmanager
    def operation(self, scope, **kwargs):
        if self.synchronized:
            with self.source_authority.operation(self, scope, **kwargs) as db:
                yield db
        else:
            with self.store.transaction() as db:
                yield db

    def _binding(self, db, account):
        return db.execute(
            "SELECT * FROM accounts WHERE account_key=?", (canonical(account),)
        ).fetchone()

    def _scope_version(self, db, scope):
        row = db.execute("SELECT version FROM scopes WHERE key=?", (canonical(scope),)).fetchone()
        return row[0] if row else 1

    def _bump(self, db, scope):
        if "profile_subject" in scope:
            scope = {k: v for k, v in scope.items() if k != "profile_subject"}
        db.execute(
            "INSERT INTO scopes VALUES (?,2) ON CONFLICT(key) DO UPDATE SET version=version+1",
            (canonical(scope),),
        )
        return self._scope_version(db, scope)

    def _authorize(self, db, context, scope=None, account=None):
        # Context is only the receiver's resolved result, never a wire body field.
        require(context["audience_service"] == "memory")
        require(not context["revoked"] and parse_time(context["expires_at"]) > self.clock())
        verified = context["verified_account"]
        validate_account(verified)
        if account is not None:
            validate_account(account)
            require(verified == account)
        binding = self._binding(db, verified)
        allowed = context["allowed_scope"]
        if binding and allowed["person_id"] is not None:
            require(allowed["person_id"] == binding["person_id"])
        if scope is not None:
            require(binding is not None and binding["person_id"] == scope["person_id"])
            require(
                allowed["actor_id"] == scope["actor_id"]
                and allowed["audience"] == scope["audience"]
            )
            require(allowed["conversation_id"] is not None, "dependency_unavailable", 503)
            # Only person null from first registration may resolve via current account binding.
            effective = dict(allowed, person_id=binding["person_id"])
            require(effective == scope)
        return binding

    def _idempotent(self, db, operation, request, context, action):
        command = request["command"]
        key = (context["authenticated_service"], operation, command["idempotency_key"])
        digest = fingerprint(semantic_request(request))
        old = db.execute(
            "SELECT digest,result FROM requests WHERE service=? AND operation=? AND key=?", key
        ).fetchone()
        if old:
            require(old["digest"] == digest, "idempotency_conflict", 409)
            return dict(json.loads(old["result"]), request_id=command["request_id"])
        require(parse_time(command["deadline_at"]) > self.clock(), "timeout", 408)
        result = action()
        db.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (*key, digest, canonical(result)))
        return result

    def resolve(self, request, context):
        with self.store.transaction() as db:
            binding = self._authorize(db, context, account=request["account"])
            return {
                "schema_version": 1,
                "request_id": request["query"]["request_id"],
                "state": "found" if binding else "unregistered",
                "person_id": binding["person_id"] if binding else None,
                "binding_version": binding["version"] if binding else 0,
            }

    def register(self, request, context):
        with self.store.transaction() as db:
            self._authorize(db, context, account=request["account"])

            def create():
                binding = self._binding(db, request["account"])
                created = binding is None
                if created:
                    person = new_id("person")
                    db.execute("INSERT INTO people VALUES (?)", (person,))
                    db.execute(
                        "INSERT INTO accounts VALUES (?,?,1,?)",
                        (canonical(request["account"]), person, request.get("display_name")),
                    )
                else:
                    person = binding["person_id"]
                    if "display_name" in request:
                        db.execute(
                            "UPDATE accounts SET display_name=? WHERE account_key=?",
                            (request["display_name"], canonical(request["account"])),
                        )
                return {
                    "schema_version": 1,
                    "request_id": request["command"]["request_id"],
                    "person_id": person,
                    "binding_version": 1 if created else binding["version"],
                    "created": created,
                }

            return self._idempotent(db, "register", request, context, create)

    def link(self, request, context):
        application = getattr(self, "memory_context", None)
        require(application is not None, "dependency_unavailable", 503)
        return application.legacy_link(request, context)

    def _source_rows(self, db, sources, scope, *, reality=None):
        if self.source_authority is None:
            raise Fault("dependency_unavailable", 503)
        self.source_authority.verify(db, sources, scope)
        require(len({source_key(s) for s in sources}) == len(sources), "invalid_input", 400)
        rows = []
        for source in sources:
            row = db.execute(
                "SELECT * FROM sources WHERE key=?", (self.source_identity(source, scope),)
            ).fetchone()
            if row is None:
                raise Fault("dependency_unavailable", 503)
            if (
                row["state"] != "active"
                or row["revision"] != source["message_key"]["revision"]
                or json.loads(row["payload"]) != source
            ):
                raise Fault("version_conflict", 409)
            require(json.loads(row["scope"]) == scope)
            if reality and reality != "mixed":
                require(row["reality"] == reality, "invalid_input", 400)
            rows.append(row)
        return rows

    def _group_current(self, db, group_id):
        if self.source_authority is None:
            raise Fault("dependency_unavailable", 503)
        rows = db.execute(
            "SELECT l.revision AS required_revision,l.epoch AS required_epoch,s.* "
            "FROM lineage l JOIN sources s ON s.key=l.source_key WHERE l.group_id=?",
            (group_id,),
        ).fetchall()
        return self.source_authority.current(db, rows)

    def _invalidate_jobs(self, db, *, source_keys=(), scopes=()):
        # Bounded pending queue; invalidation frees capacity without deleting evidence.
        for job in db.execute("SELECT * FROM jobs WHERE state='pending'").fetchall():
            event, snapshot = json.loads(job["event"]), json.loads(job["source_snapshot"])
            if canonical(event["scope"]) in scopes:
                state = "scope_changed"
            elif set(source_keys).intersection(snapshot):
                state = "stale_source"
            else:
                continue
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job["id"]))
            self._emit(
                db, "memory.candidate_invalidated", {"candidate_job_ref": job["id"], "state": state}
            )

    def _emit(self, db, kind, payload):
        db.execute(
            "INSERT INTO outbox(event_id,kind,payload) VALUES (?,?,?)",
            (new_id("event"), kind, canonical(payload)),
        )

    def _invalidate(self, db, group_ids, *, forgotten=False, replacement=None, target=None):
        scopes = set()
        for group_id in group_ids:
            group = db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
            if not group:
                continue
            if self.synchronized and group["state"] != "active" and target is None:
                continue
            # Shared visibility changes only on the active -> invalidated transition. Later
            # private source revisions still update retained history/tombstones below, but
            # cannot signal their existence through a previously withdrawn profile's epoch.
            # Preserve v1's own scope/history behavior; its versions are a separate domain.
            if "profile_subject" not in json.loads(group["scope"]) or group["state"] == "active":
                scopes.add(group["scope"])
            db.execute("UPDATE groups SET state='invalidated' WHERE id=?", (group_id,))
            for record in db.execute("SELECT * FROM records WHERE group_id=?", (group_id,)):
                payload = json.loads(record["payload"])
                payload["record_version"] += 1
                if record["id"] == target and replacement:
                    payload["statement"] = replacement
                # Old qualifiers remain in history only, never combined with the new sentence.
                payload["conditions"], payload["negations"] = [], []
                payload["valid_time"] = "semantic rebuild required"
                state = (
                    "tombstoned" if forgotten or record["state"] == "tombstoned" else "invalidated"
                )
                db.execute(
                    "UPDATE records SET version=?,state=?,payload=? WHERE id=?",
                    (payload["record_version"], state, canonical(payload), record["id"]),
                )
                db.execute(
                    "INSERT INTO history VALUES (?,?,?,?)",
                    (record["id"], payload["record_version"], state, canonical(payload)),
                )
                db.execute(
                    "UPDATE projections SET version=version+1,state='invalidated' WHERE record_id=?",
                    (record["id"],),
                )
                if self.synchronized:
                    db.execute("DELETE FROM search_index WHERE record_id=?", (record["id"],))
        bumped = set()
        for scope_text in scopes:
            scope = json.loads(scope_text)
            domain = canonical({k: v for k, v in scope.items() if k != "profile_subject"})
            if domain in bumped:
                continue
            bumped.add(domain)
            version = self._bump(db, scope)
            self._emit(db, "memory.revised", {"scope": scope, "scope_version": version})
        self._invalidate_jobs(db, scopes=scopes)
        return scopes

    def revise(self, request, context):
        with self.operation(
            scope=context["allowed_scope"], sources=request["evidence_refs"], context=context
        ) as db:
            return self._revision_in_transaction(db, request, context)

    def _revision_in_transaction(self, db, request, context):
        """One revision implementation, reusable inside an atomic continuity item."""
        binding = self._authorize(db, context)
        row = db.execute(
            "SELECT r.*,g.scope FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
            (request["record_id"],),
        ).fetchone()
        require(row is not None and binding is not None, "not_found", 404)
        scope = json.loads(row["scope"])
        require("profile_subject" not in scope, "not_found", 404)
        try:
            self._authorize(db, context, scope=scope)
        except Fault as error:
            if error.code == "forbidden":
                raise Fault("not_found", 404) from None
            raise

        # Replays still need current authority but may reference already consumed confirmation.
        def apply():
            if row["version"] != request["expected_version"]:
                raise Fault("version_conflict", 409, row["version"])
            # v1 has no restore operation. Do not return corrected for a retained tombstone.
            require(
                not (row["state"] == "tombstoned" and request["revision_kind"] == "correct"),
                "invalid_input",
                400,
            )
            proof = db.execute(
                "SELECT * FROM confirmations WHERE ref=?", (request["confirmation_ref"],)
            ).fetchone()
            require(
                proof is not None
                and not proof["consumed"]
                and proof["digest"] == fingerprint(semantic_request(request))
                and proof["account_key"] == canonical(context["verified_account"])
                and proof["scope"] == canonical(scope)
                and parse_time(proof["expires_at"]) > self.clock()
            )
            if self.synchronized:
                require(
                    proof["binding_version"] == binding["version"]
                    and proof["record_id"] == request["record_id"]
                    and proof["expected_version"] == request["expected_version"]
                )
            self._source_rows(db, request["evidence_refs"], scope)
            # Invalidate every group derived from any target source; blocks late candidates too.
            keys = [
                r[0]
                for r in db.execute(
                    "SELECT source_key FROM lineage WHERE group_id=?", (row["group_id"],)
                )
            ]
            groups = {row["group_id"]}
            for key in keys:
                groups.update(
                    r[0]
                    for r in db.execute("SELECT group_id FROM lineage WHERE source_key=?", (key,))
                )
                db.execute("UPDATE sources SET epoch=epoch+1,state='withdrawn' WHERE key=?", (key,))
                if self.synchronized:
                    db.execute(
                        "INSERT OR IGNORE INTO suppression VALUES (?,?)",
                        (key, request["revision_kind"]),
                    )
            self._invalidate(
                db,
                groups,
                forgotten=request["revision_kind"] == "forget",
                replacement=request["replacement_statement"],
                target=request["record_id"],
            )
            self._invalidate_jobs(db, source_keys=keys)
            db.execute("UPDATE confirmations SET consumed=1 WHERE ref=?", (proof["ref"],))
            return {
                "schema_version": 1,
                "request_id": request["command"]["request_id"],
                "record_id": row["id"],
                "record_version": row["version"] + 1,
                "scope_version": self._scope_version(db, scope),
                "index_state": "pending",
                "authoritative_state": (
                    "tombstoned" if request["revision_kind"] == "forget" else "corrected"
                ),
                "semantic_state": "invalidated",
            }

        return self._idempotent(db, "revise", request, context, apply)

    def select(self, request, context):
        scope = request["requested_scope"]
        with self.operation(scope=scope, context=context) as db:
            self._authorize(db, context, scope=scope)
            version = self._scope_version(db, scope)
            if request["known_scope_version"] not in (None, version):
                raise Fault("scope_changed", 409, version)
            if self.source_authority is None:
                raise Fault("dependency_unavailable", 503)
            verified = self.clock()
            response = {
                "schema_version": 1,
                "request_id": request["query"]["request_id"],
                "effective_scope": scope,
                "scope_version": version,
                "verified_at": utc(verified),
                "valid_until": utc(
                    min(verified + timedelta(seconds=30), parse_time(context["expires_at"]))
                ),
                "selected_units": [],
                "dependency_groups": [],
                "budget_used": {"tokens": 0, "bytes": 0},
                "omissions": [],
            }
            if not request["budget"]["tokens"] or not request["budget"]["bytes"]:
                # Scope authority is updated atomically by source/revision invalidation. A probe
                # reads identity/scope metadata only, regardless of matching or hidden content.
                response["omissions"] = ["budget"]
                return response

            exact_kind, separator, exact_value = request["query_text"].partition(":")
            exact = separator and exact_kind in {"field", "item"}
            search_terms = [] if exact else query_terms(request["query_text"])
            if not exact and not search_terms:
                response["omissions"] = ["no_match"]
                return response
            # Materialize only authorized, current groups BEFORE any lexical/FTS matching.
            placeholders = ",".join("?" for _ in request["selection"])
            exact_filter = f" AND {exact_kind}_key=?" if exact else ""
            parameters = [canonical(scope), *request["selection"]]
            if exact:
                parameters.append(exact_value)
            groups = db.execute(
                f"SELECT * FROM groups WHERE scope=? AND state='active' AND category IN ({placeholders}){exact_filter} ORDER BY id",
                parameters,
            ).fetchall()
            eligible = []
            for group in groups:
                if not self._group_current(db, group["id"]):
                    continue
                rows = db.execute(
                    "SELECT * FROM records WHERE group_id=? ORDER BY id", (group["id"],)
                ).fetchall()
                if set(json.loads(group["members"])) != {r["id"] for r in rows}:
                    continue
                if any(r["state"] != "active" for r in rows):
                    continue
                units = [json.loads(r["payload"]) for r in rows]
                if any(u["subject_person_id"] != scope["person_id"] for u in units):
                    continue
                if scope["audience"] == "group" and any(
                    u["visibility"] != "shared_projection"
                    or any(s["kind"] != "shareable_projection" for s in u["sources"])
                    for u in units
                ):
                    continue
                if not self._projections_current(db, units):
                    continue
                eligible.append((group, rows, units))
            if not exact:
                eligible = self._rank_groups(db, eligible, search_terms)
            response.update(self._fit_groups(eligible, request["budget"]))
            return response

    def _rank_groups(self, db, eligible, search_terms):
        # One FTS document per complete authorized group: qualifiers/members contribute
        # to relevance together. Neither private nor stale rows influence corpus scores.
        db.execute("CREATE VIRTUAL TABLE temp.allowed_search USING fts5(group_id UNINDEXED,text)")
        for group, rows, _ in eligible:
            texts = []
            for row in rows:
                index = db.execute(
                    "SELECT text FROM search_index WHERE record_id=? AND record_version=? AND scope=?",
                    (row["id"], row["version"], group["scope"]),
                ).fetchone()
                if index:
                    texts.append(index[0])
            db.execute(
                "INSERT INTO temp.allowed_search VALUES (?,?)",
                (group["id"], " ".join(texts)),
            )
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search_terms)
        matches = {}
        for match in db.execute(
            "SELECT group_id,text,bm25(allowed_search) AS relevance FROM temp.allowed_search WHERE allowed_search MATCH ?",
            (expression,),
        ):
            coverage = len(set(search_terms).intersection(match["text"].split()))
            matches[match["group_id"]] = (-coverage, match["relevance"], match["group_id"])
        eligible = sorted(
            (entry for entry in eligible if entry[0]["id"] in matches),
            key=lambda entry: matches[entry[0]["id"]],
        )
        return eligible

    def _fit_groups(self, eligible, budget):
        units, dependencies, omissions = [], [], []
        used = {"tokens": 0, "bytes": 0}
        for group, rows, group_units in eligible:
            dependency = {
                "semantic_group_id": group["id"],
                "record_ids": [u["record_id"] for u in group_units],
                "complete": True,
            }
            proposed_units, proposed_groups = units + group_units, dependencies + [dependency]
            # Unknown tokenizer: conservative UTF-8 byte charge in token field, not usage.
            cost = len(
                canonical(
                    {
                        "selected_units": proposed_units,
                        "dependency_groups": proposed_groups,
                    }
                ).encode("utf-8")
            )
            if (
                len(proposed_units) > self.max_units
                or cost > budget["bytes"]
                or cost > budget["tokens"]
            ):
                omissions.append("budget")
                continue
            units, dependencies, used = (
                proposed_units,
                proposed_groups,
                {"tokens": cost, "bytes": cost},
            )
        if not units and not omissions:
            omissions.append("no_match")
        return {
            "selected_units": units,
            "dependency_groups": dependencies,
            "budget_used": used,
            "omissions": list(dict.fromkeys(omissions)),
        }

    def _projections_current(self, db, units):
        for unit in units:
            for source in unit["sources"]:
                if source["kind"] == "shareable_projection":
                    row = db.execute(
                        "SELECT * FROM projections WHERE ref=?", (source["projection_ref"],)
                    ).fetchone()
                    if (
                        not row
                        or row["state"] != "active"
                        or row["record_id"] != unit["record_id"]
                        or row["version"] != source["projection_version"]
                    ):
                        return False
        return True

    def select_profiles(self, request, context):
        from . import profiles

        if self.contracts.profile_version is None:
            raise Fault("dependency_unavailable", 503)
        return profiles.select(self, request, context)

    def consume(self, event, publisher):
        require(publisher["authenticated_service"] == "companion")
        require(event["scope"]["actor_id"] not in publisher.get("blocked_role_actors", []))
        require(
            event["scope"] in publisher["allowed_scopes"]
            or event["scope"]["actor_id"] in publisher.get("allowed_role_actors", [])
        )
        require(event["conversation_id"] == event["scope"]["conversation_id"], "invalid_input", 400)
        with self.operation(scope=event["scope"], sources=event["sources"], event=event) as db:
            require(
                db.execute(
                    "SELECT id FROM people WHERE id=?", (event["scope"]["person_id"],)
                ).fetchone()
            )
            digest = fingerprint(event)
            old = db.execute(
                "SELECT * FROM inbox WHERE event_id=?", (event["event_id"],)
            ).fetchone()
            if old:
                require(old["digest"] == digest, "idempotency_conflict", 409)
                result = json.loads(old["result"])
                if result["state"] == "accepted":
                    result["state"] = "duplicate"
                return result
            turn_key = (event["aggregate_id"], event["input_revision"])
            input_digest = fingerprint(
                {
                    k: event[k]
                    for k in (
                        "aggregate_id",
                        "input_revision",
                        "scope",
                        "scope_version",
                        "sources",
                        "reality",
                        "conversation_id",
                        "turn_sequence",
                    )
                }
            )
            prior = db.execute(
                "SELECT * FROM turn_inputs WHERE turn_id=? AND revision=?", turn_key
            ).fetchone()
            result = {
                "schema_version": 1,
                "event_id": event["event_id"],
                "turn_id": event["aggregate_id"],
                "input_revision": event["input_revision"],
                "state": "accepted",
                "candidate_job_ref": None,
                "confirmed_memory_written": False,
            }
            aggregate_key = (event["owner"], event["aggregate_id"], event["aggregate_version"])
            aggregate_digest = fingerprint({k: v for k, v in event.items() if k != "event_id"})
            aggregate = db.execute(
                "SELECT digest FROM aggregate_events WHERE owner=? AND aggregate_id=? AND version=?",
                aggregate_key,
            ).fetchone()
            if aggregate:
                require(aggregate[0] == aggregate_digest, "idempotency_conflict", 409)
            latest = db.execute(
                "SELECT MAX(version) FROM aggregate_events WHERE owner=? AND aggregate_id=?",
                aggregate_key[:2],
            ).fetchone()[0]
            if (
                not self.synchronized
                and latest is not None
                and event["aggregate_version"] > latest + 1
            ):
                raise Fault("dependency_unavailable", 503)  # owner snapshot adapter not configured
            if prior:
                require(prior["digest"] == input_digest, "idempotency_conflict", 409)
                previous = json.loads(prior["result"])
                result.update(state="duplicate", candidate_job_ref=previous["candidate_job_ref"])
            else:
                latest_input = db.execute(
                    "SELECT MAX(revision) FROM turn_inputs WHERE turn_id=?",
                    (event["aggregate_id"],),
                ).fetchone()[0]
                if (latest_input is not None and event["input_revision"] < latest_input) or (
                    latest is not None and event["aggregate_version"] < latest
                ):
                    result["state"] = "stale_source"
                elif event["scope_version"] != self._scope_version(db, event["scope"]):
                    result["state"] = "scope_changed"
                else:
                    try:
                        rows = self._source_rows(
                            db, event["sources"], event["scope"], reality=event["reality"]
                        )
                    except Fault as error:
                        if error.code != "version_conflict":
                            raise
                        result["state"] = "stale_source"
                    else:
                        pending = db.execute(
                            "SELECT COUNT(*) FROM jobs WHERE state='pending'"
                        ).fetchone()[0]
                        require(pending < self.max_pending_jobs, "queue_full", 429)
                        result["candidate_job_ref"] = new_id("candidate")
                        snapshot = {r["key"]: [r["revision"], r["epoch"]] for r in rows}
                        db.execute(
                            "INSERT INTO jobs VALUES (?,'pending',?,?)",
                            (
                                result["candidate_job_ref"],
                                canonical(event),
                                canonical(snapshot),
                            ),
                        )
                        self._emit(
                            db,
                            "memory.candidate_accepted",
                            {"candidate_job_ref": result["candidate_job_ref"]},
                        )
                db.execute(
                    "INSERT INTO turn_inputs VALUES (?,?,?,?)",
                    (*turn_key, input_digest, canonical(result)),
                )
            db.execute(
                "INSERT INTO inbox VALUES (?,?,?)", (event["event_id"], digest, canonical(result))
            )
            db.execute(
                "INSERT OR IGNORE INTO aggregate_events VALUES (?,?,?,?)",
                (*aggregate_key, aggregate_digest),
            )
            return result

    def check_sources(self, request, publisher):
        require(publisher["authenticated_service"] == "companion")
        require(request["scope"]["actor_id"] not in publisher.get("blocked_role_actors", []))
        require(
            request["scope"] in publisher["allowed_scopes"]
            or request["scope"]["actor_id"] in publisher.get("allowed_role_actors", [])
        )
        require(self.synchronized, "dependency_unavailable", 503)
        with self.operation(
            scope=request["scope"], sources=request["sources"], check=request
        ) as db:
            self._source_rows(db, request["sources"], request["scope"])
            return dict(
                schema_version=1,
                request_id=request["request_id"],
                request_digest=fingerprint(request),
                scope=request["scope"],
                version_domain="text-dialogue/v1",
                scope_version=self._scope_version(db, request["scope"]),
                checked_at=utc(self.clock()),
            )
