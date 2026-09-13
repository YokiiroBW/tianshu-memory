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


class LocalWorkflow:
    """Explicit synthetic-source tooling and reviewed structured candidate commits.

    The source and confirmation input is trusted local operator data, not HTTP payload authority.
    A source/confirmation issuer for production remains a separate integration requirement.
    """

    def __init__(self, service):
        self.service = service
        if not isinstance(service.source_authority, LocalFixtureSources):
            raise ValueError("Local workflow requires the explicit local_fixture source backend")

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

    def commit_candidate(self, job_id, drafts):
        """Commit complete reviewed groups, or [] to skip. No LLM call or auto-confirmation.

        A job is atomic and single-consumption. Source-version ledger deduplicates overlapping
        events/turns, including relationship effects. Any stale dependency rejects the entire job.
        """
        digest = fingerprint(drafts)
        with self.service.store.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            require(job is not None, "not_found", 404)
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
                require(isinstance(drafts, list) and len(drafts) <= 32, "invalid_input", 400)
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
                key = source_key(source)
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

    def jobs(self):
        with self.service.store.transaction() as db:
            return [dict(r) for r in db.execute("SELECT id,state FROM jobs ORDER BY rowid")]

    def relationship_value(self, scope):
        with self.service.store.transaction() as db:
            return sum(
                r["amount"]
                for r in db.execute(
                    "SELECT e.* FROM relationship_entries e JOIN groups g ON g.id=e.group_id WHERE g.scope=? AND g.state='active'",
                    (canonical(scope),),
                )
                if self.service._group_current(db, r["group_id"])
            )

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
