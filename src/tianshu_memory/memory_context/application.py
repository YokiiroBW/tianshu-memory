"""Public application ports sharing Memory's records, revision and source authorities."""

import json

from ..domain import Fault, canonical, fingerprint, new_id, parse_time, require, semantic_request
from ..workflow import _WorkflowBase
from . import query
from .migration import bump_identity, ready, semantic_payload, target_key


class MemoryContext:
    def __init__(self, service, *, proofs=None):
        self.service, self.proofs = service, proofs
        self.writer = _WorkflowBase(service)

    def query(self, request, context):
        return query.execute(self, request, context)

    def _old(self, db, request, context):
        row = db.execute(
            "SELECT digest,receipt FROM context_operations WHERE service=? AND scope=? AND operation_id=?",
            (
                context["authenticated_service"],
                canonical(request["scope"]),
                request["command"]["idempotency_key"],
            ),
        ).fetchone()
        if row:
            require(
                row["digest"] == fingerprint(semantic_payload(request)), "idempotency_conflict", 409
            )
            return dict(json.loads(row["receipt"]), request_id=request["command"]["request_id"])

    def _save(self, db, request, context, receipt):
        db.execute(
            "INSERT INTO context_operations VALUES (?,?,?,?,?,?,?)",
            (
                context["authenticated_service"],
                canonical(request["scope"]),
                request["command"]["idempotency_key"],
                fingerprint(semantic_payload(request)),
                canonical(receipt),
                request["batch_ref"],
                request["item_id"],
            ),
        )
        self.service._emit(
            db,
            "memory.context_item_completed",
            {
                "operation_id": receipt["operation_id"],
                "state": receipt["state"],
                "batch_ref": request["batch_ref"],
                "item_id": request["item_id"],
            },
        )
        return receipt

    def _receipt(self, db, request, state, *, records=(), groups=(), error=None):
        return dict(
            schema_version=1,
            request_id=request["command"]["request_id"],
            operation_id=request["command"]["idempotency_key"],
            state=state,
            record_ids=list(records),
            group_ids=list(groups),
            scope_version=self.service._scope_version(db, request["scope"]),
            batch_ref=request["batch_ref"],
            item_id=request["item_id"],
            error_code=error.code if error else None,
            current_version=error.version if error else None,
        )

    def propose(self, request, context):
        service, scope = self.service, request["scope"]
        with service.store.transaction() as db:
            ready(db)
            service._authorize(db, context, scope=scope)
            old = self._old(db, request, context)
            if old is not None:
                return old
        require(parse_time(request["command"]["deadline_at"]) > service.clock(), "timeout", 408)
        proof = None
        if request["kind"] in {"correct", "forget"}:
            require(self.proofs is not None, "dependency_unavailable", 503)
            proof = self.proofs.verify(request, context, "revision")
            require(
                proof["accounts"] == [context["verified_account"]] and proof["scopes"] == [scope]
            )
        try:
            with service.operation(
                scope=scope, sources=request["evidence_refs"], context=context
            ) as db:
                ready(db)
                service._authorize(db, context, scope=scope)
                old = self._old(db, request, context)
                if old is not None:
                    return old
                db.execute("SAVEPOINT context_item")
                try:
                    receipt = self._apply(db, request, context, proof)
                except Fault as error:
                    db.execute("ROLLBACK TO context_item")
                    if error.code not in {
                        "version_conflict",
                        "scope_changed",
                        "not_found",
                        "invalid_input",
                        "forbidden",
                    }:
                        raise
                    receipt = self._receipt(db, request, "rejected", error=error)
                finally:
                    db.execute("RELEASE context_item")
                return self._save(db, request, context, receipt)
        except Fault as error:
            if error.code not in {"version_conflict", "scope_changed"}:
                raise
            with service.store.transaction() as db:
                ready(db)
                service._authorize(db, context, scope=scope)
                old = self._old(db, request, context)
                return (
                    old
                    if old is not None
                    else self._save(
                        db, request, context, self._receipt(db, request, "rejected", error=error)
                    )
                )

    def _apply(self, db, request, context, proof):
        service, scope, kind = self.service, request["scope"], request["kind"]
        rows = service._source_rows(db, request["evidence_refs"], scope)
        if kind == "no_op":
            return self._receipt(db, request, "no_op")
        target = request["target"]
        source_map = {row["key"]: row for row in rows}
        used_sources = {
            service.source_identity(source, scope)
            for unit in request["units"]
            for source in unit["sources"]
        }
        require(used_sources <= set(source_map), "invalid_input", 400)
        payload_digest = fingerprint({"kind": kind, "target": target, "units": request["units"]})
        target_digest = target_key(target)
        if kind == "upsert":
            duplicate = db.execute(
                "SELECT group_id FROM context_applications WHERE scope=? AND target_key=? AND digest=? LIMIT 1",
                (canonical(scope), target_digest, payload_digest),
            ).fetchone()
            if duplicate is not None:
                group = db.execute(
                    "SELECT state,members FROM groups WHERE id=?", (duplicate["group_id"],)
                ).fetchone()
                if (
                    group is not None
                    and group["state"] == "active"
                    and service._group_current(db, duplicate["group_id"])
                ):
                    return self._receipt(
                        db,
                        request,
                        "duplicate",
                        records=json.loads(group["members"]),
                        groups=[duplicate["group_id"]],
                    )
        if kind in {"correct", "forget"}:
            binding = service._binding(db, context["verified_account"])
            require(proof is not None and parse_time(proof["expires_at"]) > service.clock())
            revision = dict(
                command=request["command"],
                record_id=target["record_id"],
                expected_version=target["expected_version"],
                revision_kind=kind,
                confirmation_ref=proof["proof_ref"],
                evidence_refs=request["evidence_refs"],
                replacement_statement=request["units"][0]["statement"]
                if kind == "correct"
                else None,
            )
            service.contracts.validate("identity-memory#revise_request", revision)
            existing = db.execute(
                "SELECT * FROM confirmations WHERE ref=?", (proof["proof_ref"],)
            ).fetchone()
            digest = fingerprint(semantic_request(revision))
            if existing is None:
                db.execute(
                    "INSERT INTO confirmations(ref,digest,account_key,scope,expires_at,binding_version,record_id,expected_version) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        proof["proof_ref"],
                        digest,
                        canonical(context["verified_account"]),
                        canonical(scope),
                        proof["expires_at"],
                        binding["version"],
                        target["record_id"],
                        target["expected_version"],
                    ),
                )
            else:
                require(
                    existing["digest"] == digest and not existing["consumed"],
                    "idempotency_conflict",
                    409,
                )
            service._revision_in_transaction(db, revision, context)
            if kind == "forget":
                return self._receipt(db, request, "tombstoned", records=[target["record_id"]])
            # A correction must be rebuilt exclusively from still-active correction evidence.
            rows = service._source_rows(db, request["evidence_refs"], scope)
            source_map = {row["key"]: row for row in rows}
        reality = {unit["reality"] for unit in request["units"]}
        require(len(reality) == 1, "invalid_input", 400)
        draft = dict(
            scope=scope,
            category=target["category"],
            field_key=target["field_key"],
            item_key=target["item_key"],
            units=request["units"],
        )
        group_id, records = self.writer._write_group(
            db,
            draft,
            {"scope": scope, "sources": request["evidence_refs"], "reality": next(iter(reality))},
            source_map,
        )
        for row in rows:
            if row["key"] not in used_sources:
                continue
            db.execute(
                "INSERT INTO context_applications VALUES (?,?,?,?,?,?,?)",
                (
                    row["key"],
                    row["revision"],
                    canonical(scope),
                    target_digest,
                    request["command"]["idempotency_key"],
                    payload_digest,
                    group_id,
                ),
            )
        service._bump(db, scope)
        return self._receipt(
            db,
            request,
            "corrected" if kind == "correct" else "committed",
            records=records,
            groups=[group_id],
        )

    def receipt(self, request, context):
        # No source content is returned. Current issuer/account/scope authority is
        # sufficient to recover a durable ACK while the Core owner is unavailable.
        with self.service.store.transaction() as db:
            ready(db)
            self.service._authorize(db, context, scope=request["scope"])
            row = db.execute(
                "SELECT receipt FROM context_operations WHERE service=? AND scope=? AND operation_id=?",
                (
                    context["authenticated_service"],
                    canonical(request["scope"]),
                    request["operation_id"],
                ),
            ).fetchone()
            return dict(
                schema_version=1,
                request_id=request["query"]["request_id"],
                found=row is not None,
                receipt=json.loads(row[0]) if row else None,
            )

    def batch(self, request, context):
        with self.service.store.transaction() as db:
            ready(db)
            self.service._authorize(db, context, scope=request["scope"])
            rows = db.execute(
                "SELECT operation_id,receipt FROM context_operations WHERE service=? AND scope=? AND batch_ref=? AND operation_id>? ORDER BY operation_id LIMIT ?",
                (
                    context["authenticated_service"],
                    canonical(request["scope"]),
                    request["batch_ref"],
                    request["after"] or "",
                    request["limit"] + 1,
                ),
            ).fetchall()
            page = rows[: request["limit"]]
            return dict(
                schema_version=1,
                request_id=request["query"]["request_id"],
                batch_ref=request["batch_ref"],
                receipts=[json.loads(row["receipt"]) for row in page],
                next_cursor=page[-1]["operation_id"] if len(rows) > len(page) else None,
                complete=len(rows) <= len(page),
            )

    def association(self, request, context):
        service = self.service
        with service.store.transaction() as db:
            ready(db)
            service._authorize(db, context, account=request["source_account"])
            key = (
                context["authenticated_service"],
                "context_association",
                request["command"]["idempotency_key"],
            )
            old = db.execute(
                "SELECT digest,result FROM requests WHERE service=? AND operation=? AND key=?", key
            ).fetchone()
            if old is not None:
                require(
                    old["digest"] == fingerprint(semantic_request(request)),
                    "idempotency_conflict",
                    409,
                )
                return dict(json.loads(old["result"]), request_id=request["command"]["request_id"])
        proof = None
        if request["action"] == "link":
            require(self.proofs is not None, "dependency_unavailable", 503)
            proof = self.proofs.verify(request, context, "association")
            require(
                proof["accounts"] == [request["source_account"], request["target_account"]]
                and proof["scopes"] == request["scopes"]
            )
        with service.store.transaction() as db:
            ready(db)
            source = service._authorize(db, context, account=request["source_account"])
            require(source is not None)

            def apply():
                if request["action"] == "link":
                    require(parse_time(proof["expires_at"]) > service.clock())
                    target = service._binding(db, request["target_account"])
                    require(target is not None, "not_found", 404)
                    scopes = request["scopes"]
                    require(
                        scopes[0] == context["allowed_scope"]
                        and scopes[0] != scopes[1]
                        and all(
                            s["actor_id"] == scopes[0]["actor_id"]
                            and s["audience"] == "self_private"
                            for s in scopes
                        )
                    )
                    require(
                        [s["person_id"] for s in scopes]
                        == [source["person_id"], target["person_id"]]
                    )
                    old = db.execute(
                        "SELECT id FROM context_associations WHERE proof_ref=?",
                        (request["proof_ref"],),
                    ).fetchone()
                    require(old is None, "idempotency_conflict", 409)
                    identifier = new_id("association")
                    db.execute(
                        "INSERT INTO context_associations VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                        (
                            identifier,
                            canonical(request["source_account"]),
                            canonical(request["target_account"]),
                            source["person_id"],
                            target["person_id"],
                            scopes[0]["actor_id"],
                            canonical(scopes),
                            source["version"],
                            target["version"],
                            "linked",
                            request["proof_ref"],
                        ),
                    )
                else:
                    row = db.execute(
                        "SELECT * FROM context_associations WHERE id=?",
                        (request["association_id"],),
                    ).fetchone()
                    require(
                        row is not None
                        and canonical(request["source_account"])
                        in {row["source_account"], row["target_account"]}
                        and row["actor_id"] == context["allowed_scope"]["actor_id"],
                        "not_found",
                        404,
                    )
                    if row["version"] != request["expected_version"]:
                        raise Fault("version_conflict", 409, row["version"])
                    identifier, scopes = row["id"], json.loads(row["scopes"])
                    require(context["allowed_scope"] in scopes)
                    db.execute(
                        "UPDATE context_associations SET state='revoked',version=version+1 WHERE id=?",
                        (identifier,),
                    )
                row = db.execute(
                    "SELECT * FROM context_associations WHERE id=?", (identifier,)
                ).fetchone()
                for person in set((row["source_person"], row["target_person"])):
                    bump_identity(db, row["actor_id"], person)
                return dict(
                    schema_version=1,
                    request_id=request["command"]["request_id"],
                    association_id=identifier,
                    state=row["state"],
                    version=row["version"],
                    source_person_id=row["source_person"],
                    target_person_id=row["target_person"],
                    scopes=scopes,
                )

            return service._idempotent(db, "context_association", request, context, apply)

    def legacy_link(self, request, context):
        """The original link port now creates an association; it never merges identities."""
        service = self.service
        with service.store.transaction() as db:
            ready(db)
            service._authorize(db, context, account=request["source_account"])
            key = (context["authenticated_service"], "link", request["command"]["idempotency_key"])
            old = db.execute(
                "SELECT digest,result FROM requests WHERE service=? AND operation=? AND key=?", key
            ).fetchone()
            if old is not None:
                require(
                    old["digest"] == fingerprint(semantic_request(request)),
                    "idempotency_conflict",
                    409,
                )
                return dict(json.loads(old["result"]), request_id=request["command"]["request_id"])
        require(self.proofs is not None, "dependency_unavailable", 503)
        proof = self.proofs.verify(
            dict(request, proof_ref=request["verification_ref"]), context, "association"
        )
        require(
            proof["accounts"] == [request["source_account"], request["target_account"]]
            and len(proof["scopes"]) == 2
        )
        with service.store.transaction() as db:
            ready(db)
            source = service._authorize(db, context, account=request["source_account"])
            target = service._binding(db, request["target_account"])
            require(source is not None and target is not None, "not_found", 404)
            require(
                source["version"] == request["source_binding_version"]
                and target["version"] == request["target_binding_version"],
                "scope_changed",
                409,
            )
            scopes = proof["scopes"]
            require(
                parse_time(proof["expires_at"]) > service.clock()
                and scopes[0] == context["allowed_scope"]
                and scopes[0] != scopes[1]
            )
            require(
                all(
                    s["actor_id"] == scopes[0]["actor_id"] and s["audience"] == "self_private"
                    for s in scopes
                )
                and [s["person_id"] for s in scopes] == [source["person_id"], target["person_id"]]
            )

            def apply():
                require(
                    db.execute(
                        "SELECT 1 FROM context_associations WHERE proof_ref=?",
                        (request["verification_ref"],),
                    ).fetchone()
                    is None,
                    "idempotency_conflict",
                    409,
                )
                db.execute(
                    "INSERT INTO context_associations VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                    (
                        new_id("association"),
                        canonical(request["source_account"]),
                        canonical(request["target_account"]),
                        source["person_id"],
                        target["person_id"],
                        scopes[0]["actor_id"],
                        canonical(scopes),
                        source["version"],
                        target["version"],
                        "linked",
                        request["verification_ref"],
                    ),
                )
                for person in set((source["person_id"], target["person_id"])):
                    bump_identity(db, scopes[0]["actor_id"], person)
                return dict(
                    schema_version=1,
                    request_id=request["command"]["request_id"],
                    person_id=source["person_id"],
                    binding_version=source["version"],
                    created=True,
                )

            return service._idempotent(db, "link", request, context, apply)
