"""Explicit source-level forgetting through the existing user proof and operation ledger."""

import json

from ..domain import Fault, canonical, parse_time, require
from ..memory_migration import installed


def execute(application, request, context):
    service, scope = application.service, request["scope"]
    with service.store.transaction() as db:
        require(installed(db), "dependency_unavailable", 503)
        service._authorize(db, context, scope=scope)
        old = application._old(db, request, context)
        if old is not None:
            return old
    require(parse_time(request["command"]["deadline_at"]) > service.clock(), "timeout", 408)
    require(application.proofs is not None, "dependency_unavailable", 503)
    proof = application.proofs.verify(request, context, "revision")
    require(proof["accounts"] == [context["verified_account"]] and proof["scopes"] == [scope])
    with service.operation(scope=scope, sources=request["evidence_refs"], context=context) as db:
        service._authorize(db, context, scope=scope)
        old = application._old(db, request, context)
        if old is not None:
            return old
        db.execute("SAVEPOINT forget_episode")
        try:
            require(parse_time(proof["expires_at"]) > service.clock())
            service._source_rows(db, request["evidence_refs"], scope)
            target = request["target"]
            document = db.execute(
                "SELECT * FROM memory_documents WHERE id=? AND kind='episode' AND scope=?",
                (target["episode_ref"], canonical(scope)),
            ).fetchone()
            require(document is not None, "not_found", 404)
            require(
                document["source_revision"] == target["episode_version"], "version_conflict", 409
            )
            source = db.execute(
                "SELECT * FROM sources WHERE key=?", (document["source_key"],)
            ).fetchone()
            require(
                source is not None
                and source["revision"] == document["source_revision"]
                and source["epoch"] == document["source_epoch"],
                "scope_changed",
                409,
            )
            original = json.loads(source["payload"])
            if original.get("kind") == "observation":
                from ..segment_sources import admitted

                require(
                    source["scope"] == canonical(scope)
                    and source["state"] == "active"
                    and admitted(db, source["key"]),
                    "scope_changed",
                    409,
                )
            else:
                service._source_rows(db, [original], scope)
            service._invalidate_sources(db, [source["key"]], suppress="forget", forgotten=True)
            # The operation targets this exact episode/source, not one record
            # group. Its request digest is retained in context_operations;
            # the existing one-group receipt cannot enumerate a source closure.
            receipt = application._receipt(db, request, "tombstoned")
        except Fault as error:
            db.execute("ROLLBACK TO forget_episode")
            if error.code not in {
                "version_conflict",
                "scope_changed",
                "not_found",
                "invalid_input",
                "forbidden",
            }:
                raise
            receipt = application._receipt(db, request, "rejected", error=error)
        finally:
            db.execute("RELEASE forget_episode")
        return application._save(db, request, context, receipt)
