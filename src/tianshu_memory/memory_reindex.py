"""Resumable, source-verified rebuilds of derived documents; never re-extract facts."""

import json
from contextlib import closing

from .domain import Fault, canonical, new_id, require, source_selector, utc
from .memory_documents import capture, index_group
from .memory_migration import installed

CURSOR = "memory_reindex_cursor"


def materialize(service, scope, sources):
    """Read exact original inputs through the existing authenticated owner ports."""
    require(service.synchronized, "dependency_unavailable", 503)
    with service.operation(scope=scope, sources=sources) as db:
        before = {
            row["key"]: (row["revision"], row["epoch"])
            for row in service._source_rows(db, sources, scope)
        }
    authority = service.source_authority
    request = dict(
        schema_version=1,
        request_id=new_id("episode-source"),
        mode="snapshot",
        selectors=[source_selector(source, scope) for source in sources],
        turn_ids=[],
        include_content=True,
    )
    snapshot = authority.transport.facts(request)
    authority._relations("source_snapshot", request, snapshot)
    access_request, access = authority._read_access(snapshot, None)
    authority._relations("current_access", access_request, access, snapshot, utc(service.clock()))
    authority._relations("current_actor_sources", scope, sources, snapshot)
    physicals = {canonical(item["key"]): item for item in snapshot["physicals"]}
    with service.operation(scope=scope, sources=sources) as db:
        rows = service._source_rows(db, sources, scope)
        for source, row in zip(sources, rows, strict=True):
            require(before[row["key"]] == (row["revision"], row["epoch"]), "version_conflict", 409)
            physical = physicals[canonical(source_selector(source, scope)["key"])]
            stored = db.execute(
                "SELECT p.payload FROM source_admissions a JOIN physical_sources p ON p.key=a.physical_key WHERE a.key=?",
                (row["key"],),
            ).fetchone()
            require(
                stored is not None and json.loads(stored[0]) == dict(physical, content=None),
                "version_conflict",
                409,
            )
            require(
                not db.execute(
                    "SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)
                ).fetchone(),
                "scope_changed",
                409,
            )
            content = physical["content"]
            text = "\n".join(part["text"] for part in content["parts"] if part["kind"] == "text")
            capture(db, row, text, content["sent_at"])
            for group in db.execute(
                "SELECT group_id FROM lineage WHERE source_key=?", (row["key"],)
            ):
                if service._group_current(db, group[0]):
                    index_group(db, group[0])
    return len(sources)


class MemoryReindexer:
    def __init__(self, service):
        self.service = service

    def step(self, *, batch_size=32, restart=False):
        require(type(batch_size) is int and 1 <= batch_size <= 256, "invalid_input", 400)
        service = self.service
        with service.store.transaction() as db:
            require(installed(db), "dependency_unavailable", 503)
            saved = db.execute("SELECT value FROM metadata WHERE key=?", (CURSOR,)).fetchone()
            progress = (
                json.loads(saved[0])
                if saved and not restart
                else {"after": "", "completed": False, "processed": 0, "unavailable": []}
            )
            retry = progress["completed"] and bool(progress["unavailable"])
            if progress["completed"] and not retry:
                return progress
            if retry:
                keys = progress["unavailable"][:batch_size]
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM sources WHERE key IN ("
                        + ",".join("?" for _ in keys)
                        + ") ORDER BY key",
                        keys,
                    )
                ]
            else:
                rows = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM sources WHERE key>? ORDER BY key LIMIT ?",
                        (progress["after"], batch_size),
                    )
                ]
        by_scope, failed = {}, []
        for row in rows:
            if row["state"] == "active":
                by_scope.setdefault(row["scope"], []).append(row)
        for encoded, batch in by_scope.items():
            try:
                materialize(
                    service, json.loads(encoded), [json.loads(row["payload"]) for row in batch]
                )
            except (Fault, OSError, ValueError, TypeError, KeyError):
                # Record actual unavailable sources. The cursor can progress past
                # a withdrawn row, but never calls that source rebuilt.
                for row in batch:
                    try:
                        materialize(service, json.loads(encoded), [json.loads(row["payload"])])
                    except (Fault, OSError, ValueError, TypeError, KeyError):
                        failed.append(row["key"])
        progress["processed"] += len(rows) - len(failed)
        visited = {row["key"] for row in rows}
        progress["unavailable"] = list(
            dict.fromkeys([key for key in progress["unavailable"] if key not in visited] + failed)
        )
        if not retry:
            progress["after"] = rows[-1]["key"] if rows else progress["after"]
            progress["completed"] = len(rows) < batch_size
        with service.store.transaction() as db:
            db.execute(
                "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (CURSOR, canonical(progress)),
            )
        return progress

    def index_step(self, *, batch_size=4):
        """Fill missing vectors under the configured backend, with fresh source checks."""
        from .persistent_retrieval import missing, write
        from .retrieval_vectors import vectors

        service = self.service
        require(type(batch_size) is int and 1 <= batch_size <= 32, "invalid_input", 400)
        if service.retrieval is None:
            return {"indexed": 0, "state": "not_configured"}
        with closing(service.store.readonly()) as db:
            if not installed(db):
                return {"indexed": 0, "state": "migration_required"}
        settings = service.retrieval.settings()
        if settings["mode"] == "keyword":
            return {"indexed": 0, "state": "keyword"}
        with service.store.transaction() as db:
            if not installed(db):
                return {"indexed": 0, "state": "migration_required"}
            documents = missing(db, settings["embedding_revision"], batch_size)
            scopes = {
                row[0]
                for document in documents
                if document["kind"] == "fact"
                for row in db.execute(
                    "SELECT DISTINCT s.scope FROM lineage l JOIN sources s ON s.key=l.source_key WHERE l.group_id=?",
                    (document["group_id"],),
                )
            }
            scopes.update(
                document["scope"] for document in documents if document["kind"] == "episode"
            )
        if not documents:
            return {"indexed": 0, "state": "complete"}
        from .memory_documents import discard

        requirements = [{"scope": json.loads(encoded)} for encoded in scopes]
        with service.source_authority.bundle_operation(service, requirements) as db:
            ready = []
            for document in documents:
                if self._current(db, document):
                    ready.append(document)
                else:
                    discard(db, document["id"])
        if not ready:
            return {"indexed": 0, "state": "pending"}
        answer = service.retrieval.backend.call(
            "embed",
            {"revision": settings["revision"], "texts": [document["text"] for document in ready]},
        )
        computed = vectors(answer["vectors"], len(ready))
        indexed = 0
        with service.source_authority.bundle_operation(service, requirements) as db:
            for document, vector in zip(ready, computed, strict=True):
                if self._current(db, document):
                    indexed += write(db, document, settings["embedding_revision"], vector)
                else:
                    discard(db, document["id"])
        return {
            "indexed": indexed,
            "state": "complete" if len(documents) < batch_size else "pending",
        }

    def _current(self, db, document):
        if document["kind"] == "fact":
            group = db.execute(
                "SELECT state FROM groups WHERE id=?", (document["group_id"],)
            ).fetchone()
            return bool(
                group
                and group[0] == "active"
                and self.service._group_current(db, document["group_id"])
            )
        row = db.execute("SELECT * FROM sources WHERE key=?", (document["source_key"],)).fetchone()
        return bool(
            row
            and self.service.source_authority.current(
                db,
                [
                    dict(
                        row,
                        required_revision=document["source_revision"],
                        required_epoch=document["source_epoch"],
                    )
                ],
            )
        )
