"""Memory-owned operator/worker application layer. Never exposed as arbitrary model HTTP writes."""

import json

from . import profiles
from .domain import (
    Fault,
    canonical,
    fingerprint,
    new_id,
    parse_time,
    require,
    semantic_request,
    source_key,
    terms,
)
from .service import CATEGORIES
from .sources import LocalFixtureSources


class _WorkflowBase:
    """Memory-owned transaction helpers shared by separately gated applications."""

    def __init__(self, service):
        self.service = service

    def _commit_candidate(self, job_id, drafts, *, exact_scope=False):
        """Commit complete reviewed groups, or [] to skip. No LLM call or auto-confirmation.

        A job is atomic and single-consumption. Source-version ledger deduplicates overlapping
        events/turns, including relationship effects. Any stale dependency rejects the entire job.
        """
        require(isinstance(drafts, list) and len(drafts) <= 32, "invalid_input", 400)
        require(
            all(
                isinstance(draft, dict) and isinstance(draft.get("units"), list) for draft in drafts
            ),
            "invalid_input",
            400,
        )
        # The result contract cannot represent a partial commit or more than 256 records.
        require(sum(len(draft["units"]) for draft in drafts) <= 256, "invalid_input", 400)
        digest = fingerprint(drafts)
        with self.service.store.transaction() as db:
            initial = db.execute("SELECT event FROM jobs WHERE id=?", (job_id,)).fetchone()
            require(initial is not None, "not_found", 404)
            event = json.loads(initial["event"])
        with self.service.operation(
            scope=event["scope"], sources=event["sources"], event=event
        ) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            require(job is not None, "not_found", 404)
            require(job["event"] == initial["event"], "version_conflict", 409)
            if exact_scope:
                # Reviewed source extraction does not itself approve broader disclosure.
                require(all(draft["scope"] == event["scope"] for draft in drafts))
            old = db.execute("SELECT * FROM write_ledger WHERE job_id=?", (job_id,)).fetchone()
            if old:
                require(old["digest"] == digest, "idempotency_conflict", 409)
                return json.loads(old["result"])
            event, snapshot = json.loads(job["event"]), json.loads(job["source_snapshot"])
            if event["scope_version"] != self.service._scope_version(db, event["scope"]):
                raise Fault("scope_changed", 409)
            rows = self.service._source_rows(
                db, event["sources"], event["scope"], reality=event["reality"]
            )
            require(
                snapshot == {r["key"]: [r["revision"], r["epoch"]] for r in rows},
                "version_conflict",
                409,
            )
            source_map = {r["key"]: r for r in rows}
            # An already-accounted source cannot be re-used under a new turn id or job id.
            duplicate = any(
                db.execute(
                    "SELECT job_id FROM source_writes WHERE source_key=? AND revision=? AND scope=?",
                    (r["key"], r["revision"], canonical(event["scope"])),
                ).fetchone()
                for r in rows
            )
            if duplicate:
                result = {"state": "duplicate_source", "record_ids": [], "group_ids": []}
            else:
                record_ids, group_ids = [], []
                for draft in drafts:
                    result_group = self._write_group(db, draft, event, source_map)
                    group_ids.append(result_group[0])
                    record_ids.extend(result_group[1])
                for row in rows:
                    db.execute(
                        "INSERT INTO source_writes VALUES (?,?,?,?)",
                        (
                            row["key"],
                            row["revision"],
                            canonical(event["scope"]),
                            job_id,
                        ),
                    )
                result = {
                    "state": "committed" if drafts else "skipped",
                    "record_ids": record_ids,
                    "group_ids": group_ids,
                }
            db.execute(
                "INSERT INTO write_ledger VALUES (?,?,?)", (job_id, digest, canonical(result))
            )
            db.execute("UPDATE jobs SET state=? WHERE id=?", (result["state"], job_id))
            self.service._emit(
                db, "memory.candidate_completed", {"candidate_job_ref": job_id, **result}
            )
            return result

    def _write_group(self, db, draft, event, source_map):
        require(
            set(draft)
            <= {"scope", "category", "field_key", "item_key", "units", "relationship_delta"},
            "invalid_input",
            400,
        )
        scope = draft["scope"]
        self.service.contracts.validate("common#scope", scope)
        require(draft["category"] in CATEGORIES, "invalid_input", 400)
        require(
            scope["actor_id"] == event["scope"]["actor_id"]
            and scope["person_id"] == event["scope"]["person_id"]
        )
        shared = scope["audience"] == "group"
        # Publishing a projection is an explicit reviewed draft, not a change in the source scope.
        if not shared:
            require(scope == event["scope"])
        require(
            isinstance(draft["units"], list) and 0 < len(draft["units"]) <= self.service.max_units,
            "invalid_input",
            400,
        )
        group_id = new_id("group")
        ids = [new_id("record") for _ in draft["units"]]
        db.execute(
            "INSERT INTO groups VALUES (?,?,'active',?,?,?,?)",
            (
                group_id,
                canonical(scope),
                canonical(ids),
                draft["category"],
                draft.get("field_key"),
                draft.get("item_key"),
            ),
        )
        lineage = set()
        for record_id, draft_unit in zip(ids, draft["units"], strict=True):
            require(
                set(draft_unit)
                == {
                    "statement",
                    "conditions",
                    "negations",
                    "valid_time",
                    "uncertainty",
                    "reality",
                    "sources",
                },
                "invalid_input",
                400,
            )
            require(bool(draft_unit["sources"]), "invalid_input", 400)
            for source in draft_unit["sources"]:
                key = self.service.source_identity(source, event["scope"])
                require(key in source_map and json.loads(source_map[key]["payload"]) == source)
                require(source_map[key]["reality"] == draft_unit["reality"], "invalid_input", 400)
                lineage.add(key)
            unit = {k: v for k, v in draft_unit.items() if k != "sources"}
            unit.update(
                record_id=record_id,
                record_version=1,
                semantic_group_id=group_id,
                subject_person_id=scope["person_id"],
                visibility="shared_projection" if shared else "self_private",
            )
            if shared:
                ref = new_id("projection")
                unit["sources"] = [
                    {
                        "kind": "shareable_projection",
                        "owner": "memory",
                        "projection_ref": ref,
                        "projection_version": 1,
                    }
                ]
            else:
                unit["sources"] = [
                    {"kind": "raw_message", "source": s} for s in draft_unit["sources"]
                ]
            self.service.contracts.validate("identity-memory#unit", unit)
            self._store_unit(
                db, unit, group_id, scope, draft.get("field_key"), draft.get("item_key")
            )
        for key in lineage:
            row = source_map[key]
            db.execute(
                "INSERT INTO lineage VALUES (?,?,?,?)",
                (group_id, key, row["revision"], row["epoch"]),
            )
        delta = draft.get("relationship_delta")
        if delta is not None:
            require(
                draft["category"] == "relationship" and type(delta) is int and -100 <= delta <= 100,
                "invalid_input",
                400,
            )
            db.execute("INSERT INTO relationship_entries VALUES (?,?)", (group_id, delta))
            from .relationships.legacy import route_legacy

            route_legacy(self.service, db, group_id, scope, delta, event)
        return group_id, ids

    def _store_unit(self, db, unit, group_id, scope, field, item):
        record_id = unit["record_id"]
        db.execute(
            "INSERT INTO records VALUES (?,?,1,'active',?)", (record_id, group_id, canonical(unit))
        )
        db.execute("INSERT INTO history VALUES (?,1,'active',?)", (record_id, canonical(unit)))
        for source in unit["sources"]:
            if source["kind"] == "shareable_projection":
                db.execute(
                    "INSERT INTO projections VALUES (?,?,1,'active')",
                    (source["projection_ref"], record_id),
                )
        self._index(db, unit, scope, field, item)

    def _index(self, db, unit, scope, field, item):
        text = " ".join(
            [
                unit["statement"],
                *unit["conditions"],
                *unit["negations"],
                unit["valid_time"],
                field or "",
                item or "",
            ]
        )
        db.execute(
            "INSERT INTO search_index VALUES (?,?,?,?)",
            (
                unit["record_id"],
                unit["record_version"],
                canonical(scope),
                " ".join(terms(text)),
            ),
        )

    def _publish_profile(self, db, draft, approval_ref, sources):
        scope = profiles.storage_scope(draft)
        group_id, ids = new_id("group"), [new_id("record") for _ in draft["units"]]
        db.execute(
            "INSERT INTO groups VALUES (?,?,'active',?,?,?,NULL)",
            (group_id, canonical(scope), canonical(ids), draft["category"], draft["field_key"]),
        )
        subject = draft["subject"]
        subject_id = (
            subject["person_id"] if subject["kind"] == "person" else subject["conversation_id"]
        )
        db.execute(
            "INSERT INTO profile_shares VALUES (?,?,?,?,?,?,?)",
            (
                group_id,
                scope["actor_id"],
                subject["kind"],
                subject_id,
                draft["sharing"],
                draft["conversation_id"],
                approval_ref,
            ),
        )
        for record_id, unit_draft in zip(ids, draft["units"], strict=True):
            unit = dict(
                unit_draft,
                record_id=record_id,
                record_version=1,
                semantic_group_id=group_id,
                subject=subject,
                field_key=draft["field_key"],
                category=draft["category"],
                sharing=draft["sharing"],
                visibility="shared_projection",
                sources=[
                    {
                        "kind": "shareable_projection",
                        "owner": "memory",
                        "projection_ref": new_id("projection"),
                        "projection_version": 1,
                    }
                ],
            )
            if self.service.contracts.profile_version is not None:
                self.service.contracts.validate("profiles#unit", unit)
            self._store_unit(db, unit, group_id, scope, draft["field_key"], None)
        for key, row in sources.items():
            db.execute(
                "INSERT INTO lineage VALUES (?,?,?,?)",
                (group_id, key, row["revision"], row["epoch"]),
            )
        version = self.service._bump(db, scope)
        result = {"group_id": group_id, "record_ids": ids}
        db.execute(
            "UPDATE profile_approvals SET consumed=1,result=? WHERE ref=?",
            (canonical(result), approval_ref),
        )
        self.service._emit(
            db, "memory.profile_published", {"scope": scope, "scope_version": version}
        )
        return result

    def jobs(self):
        with self.service.store.transaction() as db:
            return [dict(r) for r in db.execute("SELECT id,state FROM jobs ORDER BY rowid")]

    def outbox(self, after=0, limit=100):
        require(0 < limit <= 1000, "invalid_input", 400)
        with self.service.store.transaction() as db:
            return [
                dict(r, payload=json.loads(r["payload"]))
                for r in db.execute(
                    "SELECT * FROM outbox WHERE position>? AND acknowledged=0 ORDER BY position LIMIT ?",
                    (after, limit),
                )
            ]

    def acknowledge(self, event_id):
        with self.service.store.transaction() as db:
            db.execute("UPDATE outbox SET acknowledged=1 WHERE event_id=?", (event_id,))


class LocalWorkflow(_WorkflowBase):
    """Explicit synthetic operator tooling; never a production approval issuer."""

    def __init__(self, service):
        if not isinstance(service.source_authority, LocalFixtureSources):
            raise ValueError("Local workflow requires the explicit local_fixture source backend")
        super().__init__(service)

    def observe_source(self, source, scope, *, reality="real", state="active"):
        self.service.contracts.validate("common#source", source)
        self.service.contracts.validate("common#scope", scope)
        require(reality in {"real", "fictional"}, "invalid_input", 400)
        require(state in {"active", "withdrawn"}, "invalid_input", 400)
        with self.service.store.transaction() as db:
            require(
                db.execute("SELECT id FROM people WHERE id=?", (scope["person_id"],)).fetchone()
            )
            key, revision = source_key(source), source["message_key"]["revision"]
            old = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            if old:
                require(revision >= old["revision"], "version_conflict", 409)
                if revision == old["revision"]:
                    # Same revision can revoke, never un-revoke. Archive transitions need a real
                    # owner receipt adapter; local fixture loads the final attested form once.
                    require(
                        old["payload"] == canonical(source)
                        and old["scope"] == canonical(scope)
                        and old["reality"] == reality,
                        "idempotency_conflict",
                        409,
                    )
                    require(
                        not (old["state"] == "withdrawn" and state == "active"),
                        "version_conflict",
                        409,
                    )
                    if old["state"] == state:
                        return {"revision": revision, "epoch": old["epoch"]}
                groups = [
                    r[0]
                    for r in db.execute("SELECT group_id FROM lineage WHERE source_key=?", (key,))
                ]
                self.service._invalidate(db, groups)
                self.service._invalidate_jobs(db, source_keys=[key])
                epoch = old["epoch"] + 1
                db.execute(
                    "UPDATE sources SET revision=?,epoch=?,state=?,scope=?,reality=?,payload=? WHERE key=?",
                    (revision, epoch, state, canonical(scope), reality, canonical(source), key),
                )
            else:
                epoch = 1
                db.execute(
                    "INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                    (key, revision, epoch, state, canonical(scope), reality, canonical(source)),
                )
            return {"revision": revision, "epoch": epoch}

    def confirm_revision(self, request, account, scope, expires_at):
        """Register an exact, expiring confirmation after an operator reviewed the whole command."""
        self.service.contracts.validate("identity-memory#revise_request", request)
        require(parse_time(expires_at) > self.service.clock(), "invalid_input", 400)
        with self.service.store.transaction() as db:
            row = self.service._binding(db, account)
            require(row is not None and row["person_id"] == scope["person_id"])
            db.execute(
                "INSERT INTO confirmations(ref,digest,account_key,scope,expires_at) VALUES (?,?,?,?,?)",
                (
                    request["confirmation_ref"],
                    fingerprint(semantic_request(request)),
                    canonical(account),
                    canonical(scope),
                    expires_at,
                ),
            )

    def approve_profile(self, draft, context, expires_at):
        """Synthetic operator approval of the exact subject, sharing boundary and wording.

        Group approval here is fixture curation, not proof that a real member can speak for a
        group. No production consent/curator issuer is shipped or inferred from service tokens.
        """
        require(parse_time(expires_at) > self.service.clock(), "invalid_input", 400)
        with self.service.store.transaction() as db:
            self.service.store.require_profiles(db)
            sources = profiles.validate_draft(self.service, db, draft)
            profiles.approval_authority(self.service, db, draft, context)
            ref = new_id("profile-approval")
            db.execute(
                "INSERT INTO profile_approvals(ref,digest,source_snapshot,expires_at) VALUES (?,?,?,?)",
                (ref, fingerprint(draft), profiles.snapshot(sources), expires_at),
            )
            return ref

    def publish_profile(self, draft, approval_ref, context):
        """Consume one live, payload-bound local approval. Never derive consent from a field."""
        with self.service.store.transaction() as db:
            self.service.store.require_profiles(db)
            sources = profiles.validate_draft(self.service, db, draft)
            profiles.approval_authority(self.service, db, draft, context)
            proof = db.execute(
                "SELECT * FROM profile_approvals WHERE ref=?", (approval_ref,)
            ).fetchone()
            require(proof is not None and proof["digest"] == fingerprint(draft))
            require(parse_time(proof["expires_at"]) > self.service.clock())
            require(proof["source_snapshot"] == profiles.snapshot(sources), "version_conflict", 409)
            if proof["consumed"]:
                return json.loads(proof["result"])
            return self._publish_profile(db, draft, approval_ref, sources)

    def commit_candidate(self, job_id, drafts):
        return self._commit_candidate(job_id, drafts)

    def rebuild_index(self):
        with self.service.store.transaction() as db:
            db.execute("DELETE FROM search_index")
            count = 0
            for row in db.execute(
                "SELECT r.*,g.scope,g.field_key,g.item_key FROM records r JOIN groups g ON g.id=r.group_id WHERE r.state='active' AND g.state='active'"
            ).fetchall():
                if self.service._group_current(db, row["group_id"]):
                    self._index(
                        db,
                        json.loads(row["payload"]),
                        json.loads(row["scope"]),
                        row["field_key"],
                        row["item_key"],
                    )
                    count += 1
            return {"indexed_records": count}

    def relationship_value(self, scope):
        from .relationships.legacy import value

        migrated = value(self.service, scope)
        if migrated is not None:
            return migrated
        with self.service.store.transaction() as db:
            return sum(
                r["amount"]
                for r in db.execute(
                    "SELECT e.* FROM relationship_entries e JOIN groups g ON g.id=e.group_id WHERE g.scope=? AND g.state='active'",
                    (canonical(scope),),
                )
                if self.service._group_current(db, r["group_id"])
            )


class TrustedWorkflow(_WorkflowBase):
    """Internal application ports backed by owner synchronization and real approval.

    The injected adapter must authenticate the approving user and verify their approval of
    the whole input. A schema-valid context, service credential or model field is no proof.
    The local user application authenticates explicit operations with deployment credentials.
    """

    def __init__(self, service, confirmation_adapter=None, profile_approval_adapter=None):
        from .source_authority import SourceAuthority

        if not isinstance(service.source_authority, SourceAuthority):
            raise ValueError("Trusted workflow requires the production SourceAuthority")
        super().__init__(service)
        self.confirmation_adapter = confirmation_adapter
        self.profile_approval_adapter = profile_approval_adapter

    def commit_candidate(self, input):
        self.service.contracts.validate("sync-workflow#candidate_commit", input)
        result = self._commit_candidate(input["job_id"], input["drafts"], exact_scope=True)
        self.service.contracts.validate("sync-workflow#candidate_result", result)
        return result

    def confirm_revision(self, input):
        self.service.contracts.validate("sync-workflow#confirmation_input", input)
        # Retain our own immutable value across the external approval call. The adapter may
        # not change what is bound into the durable confirmation after approving a copy.
        input = json.loads(canonical(input))
        self._approval(self.confirmation_adapter, "confirm_revision", input)
        return self._register_confirmation(input)

    def _register_confirmation(self, input):
        request, context = input["request"], input["verified_context"]
        scope = context["allowed_scope"]
        with self.service.operation(
            scope=scope, sources=request["evidence_refs"], context=context
        ) as db:
            require(
                context["issuer"] == "platform"
                and context["authenticated_service"] == "companion"
                and context["audience_service"] == "memory"
                and context["assertion_ref"] == request["command"]["origin"]["assertion_ref"]
            )
            if self.confirmation_adapter is not None:
                self.confirmation_adapter.recheck(db, context)
            binding = self.service._authorize(db, context, scope=scope)
            require(binding is not None and binding["version"] == input["binding_version"])
            require(parse_time(input["expires_at"]) > self.service.clock(), "invalid_input", 400)
            row = db.execute(
                "SELECT r.*,g.scope FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                (request["record_id"],),
            ).fetchone()
            require(
                row is not None
                and "profile_subject" not in json.loads(row["scope"])
                and row["scope"] == canonical(scope),
                "not_found",
                404,
            )
            if row["version"] != request["expected_version"]:
                raise Fault("version_conflict", 409, row["version"])
            proof = {
                "confirmation_ref": request["confirmation_ref"],
                "record_id": request["record_id"],
                "semantic_digest": fingerprint(semantic_request(request)),
                "account": context["verified_account"],
                "scope": scope,
                "binding_version": binding["version"],
                "expected_version": request["expected_version"],
                "expires_at": input["expires_at"],
                "consumed": False,
            }
            self.service.contracts.validate("sync-shared#confirmation_record", proof)
            old = db.execute(
                "SELECT * FROM confirmations WHERE ref=?", (proof["confirmation_ref"],)
            ).fetchone()
            if old:
                require(
                    old["digest"] == proof["semantic_digest"]
                    and old["account_key"] == canonical(proof["account"])
                    and old["scope"] == canonical(scope)
                    and old["binding_version"] == proof["binding_version"]
                    and old["record_id"] == proof["record_id"]
                    and old["expected_version"] == proof["expected_version"]
                    and old["expires_at"] == proof["expires_at"],
                    "idempotency_conflict",
                    409,
                )
                return dict(proof, consumed=bool(old["consumed"]))
            db.execute(
                "INSERT INTO confirmations"
                "(ref,digest,account_key,scope,expires_at,binding_version,record_id,expected_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    proof["confirmation_ref"],
                    proof["semantic_digest"],
                    canonical(proof["account"]),
                    canonical(scope),
                    proof["expires_at"],
                    proof["binding_version"],
                    proof["record_id"],
                    proof["expected_version"],
                ),
            )
            return proof

    def _approval(self, adapter, operation, payload):
        from .user_approval import LocalUserApproval

        require(type(adapter) is LocalUserApproval, "dependency_unavailable", 503)
        adapter.verify(operation, payload)
        return adapter

    def _profile_operation(self, draft, context):
        sources = [source for unit in draft["units"] for source in unit["sources"]]
        return self.service.operation(
            scope=context["allowed_scope"], sources=sources, context=context, profile=True
        )

    def approve_profile(self, draft, context, expires_at):
        payload = dict(draft=draft, context=context, expires_at=expires_at)
        adapter = self._approval(self.profile_approval_adapter, "approve_profile", payload)
        draft, context = json.loads(canonical(draft)), json.loads(canonical(context))
        require(parse_time(expires_at) > self.service.clock(), "invalid_input", 400)
        with self._profile_operation(draft, context) as db:
            self.service.store.require_user_actions(db)
            sources = profiles.validate_draft(self.service, db, draft)
            authority = adapter.authority(db, draft, context)
            ref = new_id("profile-approval")
            db.execute(
                "INSERT INTO profile_approvals(ref,digest,source_snapshot,expires_at) VALUES (?,?,?,?)",
                (ref, fingerprint(draft), profiles.snapshot(sources), expires_at),
            )
            db.execute(
                "INSERT INTO profile_approval_authorities(ref,authority,draft) VALUES (?,?,?)",
                (ref, canonical(authority), canonical(draft)),
            )
            return ref

    def publish_profile(self, draft, approval_ref, context):
        adapter = self._approval(
            self.profile_approval_adapter,
            "publish_profile",
            dict(draft=draft, approval_ref=approval_ref, context=context),
        )
        draft, context = json.loads(canonical(draft)), json.loads(canonical(context))
        with self._profile_operation(draft, context) as db:
            self.service.store.require_user_actions(db)
            sources = profiles.validate_draft(self.service, db, draft)
            authority = adapter.authority(db, draft, context)
            proof = db.execute(
                "SELECT p.*,a.authority,a.draft,a.revoked FROM profile_approvals p "
                "JOIN profile_approval_authorities a ON a.ref=p.ref WHERE p.ref=?",
                (approval_ref,),
            ).fetchone()
            require(proof is not None and not proof["revoked"])
            require(proof["authority"] == canonical(authority))
            require(proof["digest"] == fingerprint(draft) and proof["draft"] == canonical(draft))
            require(parse_time(proof["expires_at"]) > self.service.clock())
            require(proof["source_snapshot"] == profiles.snapshot(sources), "version_conflict", 409)
            if proof["consumed"]:
                result = json.loads(proof["result"])
                require(
                    self.service._group_current(db, result["group_id"]), "version_conflict", 409
                )
                return result
            return self._publish_profile(db, draft, approval_ref, sources)

    def revoke_profile(self, draft, approval_ref, context):
        adapter = self._approval(
            self.profile_approval_adapter,
            "revoke_profile",
            dict(draft=draft, approval_ref=approval_ref, context=context),
        )
        # The owner barrier commits remote negatives before this withdrawal transaction.
        with self._profile_operation(draft, context) as db:
            self.service.store.require_user_actions(db)
            authority = adapter.authority(db, draft, context)
            proof = db.execute(
                "SELECT p.*,a.authority,a.draft,a.revoked FROM profile_approvals p "
                "JOIN profile_approval_authorities a ON a.ref=p.ref WHERE p.ref=?",
                (approval_ref,),
            ).fetchone()
            require(proof is not None and proof["draft"] == canonical(draft))
            original = json.loads(proof["authority"])
            require(
                all(original[key] == authority[key] for key in ("principal", "account", "scope"))
            )
            if not proof["revoked"]:
                db.execute(
                    "UPDATE profile_approval_authorities SET revoked=1 WHERE ref=?", (approval_ref,)
                )
                if proof["consumed"]:
                    self.service._invalidate(db, [json.loads(proof["result"])["group_id"]])
            return {"approval_ref": approval_ref, "state": "revoked"}

    def rebuild_index(self):
        # Whole-store maintenance needs independently bounded coverage for every scope.
        raise Fault("dependency_unavailable", 503)
