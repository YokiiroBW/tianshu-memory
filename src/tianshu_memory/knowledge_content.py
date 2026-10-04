"""Knowledge-owned original acquisition, exact reader grants and actual range reads."""

import json
from datetime import timedelta

from . import knowledge_media as media
from .domain import canonical, fingerprint, new_id, parse_time, require, utc
from .knowledge_content_migration import ready
from .knowledge_originals import store_version
from .knowledge_sources import content_hash, fetch_url


class KnowledgeContent:
    def __init__(self, service, auth):
        self.service, self.auth = service, auth

    def authorize(self, principal, caller_name, caller):
        if principal["kind"] == "actor":
            actor = principal["actor_id"]
            decision = self.auth.role_grants.decision(actor) if self.auth.role_grants else None
            require(
                caller.get("runtime_content") is True
                and decision is not False
                and (
                    actor in caller.get("allowed_actors", [])
                    or caller.get("allow_runtime_roles") is True
                    and decision is True
                )
            )
            owner = {"kind": "actor", "actor_id": actor}
            context = None
            request_id = principal["request_id"]
        else:
            query, scope = principal["query"], principal["scope"]
            context = self.auth.resolve(
                caller_name, caller, query["origin"]["assertion_ref"], query["request_id"]
            )
            with self.service.store.transaction() as db:
                self.service._authorize(db, context, scope=scope)
            owner, request_id = {"kind": "user", "scope": scope}, query["request_id"]
        return {"owner": owner, "context": context, "request_id": request_id, "caller": caller_name}

    def _check(self, db, authority):
        ready(db)
        owner = authority["owner"]
        if owner["kind"] == "user":
            self.service._authorize(db, authority["context"], scope=owner["scope"])
        else:
            caller = self.auth.config().get("callers", {}).get(authority["caller"], {})
            decision = (
                self.auth.role_grants.decision(owner["actor_id"]) if self.auth.role_grants else None
            )
            require(
                caller.get("runtime_content") is True
                and decision is not False
                and (
                    owner["actor_id"] in caller.get("allowed_actors", [])
                    or caller.get("allow_runtime_roles") is True
                    and decision is True
                )
            )

    def _prior(self, db, operation, request, authority):
        row = db.execute(
            "SELECT digest,result FROM knowledge_content_operations WHERE owner=? AND operation=? AND request_id=?",
            (canonical(authority["owner"]), operation, authority["request_id"]),
        ).fetchone()
        if row:
            require(
                row["digest"]
                == fingerprint({k: v for k, v in request.items() if k != "principal"}),
                "idempotency_conflict",
                409,
            )
            return json.loads(row["result"])

    def _save(self, db, operation, request, authority, result):
        db.execute(
            "INSERT INTO knowledge_content_operations VALUES (?,?,?,?,?)",
            (
                canonical(authority["owner"]),
                operation,
                authority["request_id"],
                fingerprint({k: v for k, v in request.items() if k != "principal"}),
                canonical(result),
            ),
        )
        return result

    def _document(self, db, reference, authority, *, owner_only=False):
        require(reference["owner"] == "memory", "invalid_input", 400)
        row = db.execute(
            "SELECT d.*,c.owner,c.metadata,c.access_version,v.hash,v.raw,v.text,v.media_type,v.provenance FROM knowledge_documents d JOIN knowledge_content_documents c ON c.document_id=d.id JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.version WHERE d.id=?",
            (reference["object_id"],),
        ).fetchone()
        require(row is not None, "not_found", 404)
        require(row["state"] == "ready", "not_found", 404)
        require(
            row["version"] == reference["version"] and row["hash"] == reference["sha256"],
            "version_conflict",
            409,
        )
        require(self._ref(row) == reference, "invalid_input", 400)
        if row["owner"] != canonical(authority["owner"]):
            require(not owner_only and authority["owner"]["kind"] == "user")
            grant = db.execute(
                "SELECT * FROM knowledge_content_grants WHERE document_id=? AND reader=?",
                (row["id"], canonical(authority["owner"]["scope"])),
            ).fetchone()
            require(
                grant is not None
                and grant["enabled"] == 1
                and grant["version"] == row["version"]
                and grant["hash"] == row["hash"]
            )
        return row

    @staticmethod
    def _ref(row):
        provenance = json.loads(row["provenance"])
        total = provenance["coverage_total"]
        return {
            "owner": "memory",
            "object_id": row["id"],
            "version": row["version"],
            "sha256": row["hash"],
            "kind": provenance["content_kind"],
            "sources": [
                {"owner": "memory", "object_id": row["source_id"], "version": row["version"]}
            ],
            "coverage": {
                "unit": provenance["coverage_unit"],
                "start": 0,
                "end": total if total is not None else 0,
                "total": total,
            },
        }

    def _response(self, row, authority):
        until = self.service.clock() + timedelta(seconds=60)
        if authority["context"] is not None:
            until = min(until, parse_time(authority["context"]["expires_at"]))
        return {
            "schema_version": 1,
            "request_id": authority["request_id"],
            "content_ref": self._ref(row),
            "source": json.loads(row["metadata"]),
            "available_until": utc(until),
            "current": True,
            "access_version": row["access_version"],
        }

    def _upload(self, db, upload_id, authority):
        row = db.execute(
            "SELECT * FROM knowledge_content_uploads WHERE id=?", (upload_id,)
        ).fetchone()
        require(row is not None, "not_found", 404)
        require(row["owner"] == canonical(authority["owner"]))
        require(parse_time(row["expires_at"]) > self.service.clock(), "not_found", 404)
        return row

    @staticmethod
    def _upload_response(row, request_id):
        descriptor = json.loads(row["descriptor"])
        return {
            "schema_version": 1,
            "request_id": request_id,
            "upload_id": row["id"],
            "state": row["state"],
            "size": descriptor["size"],
            "sha256": descriptor["sha256"],
            "expires_at": row["expires_at"],
        }

    def uploads(self, request, authority):
        require(request["media_type"] in media.MEDIA_TYPES, "unsupported", 415)
        with self.service.store.transaction() as db:
            self._check(db, authority)
            old = self._prior(db, "uploads", request, authority)
            if old:
                return self._upload_response(
                    self._upload(db, old["upload_id"], authority), authority["request_id"]
                )
            # Expired staging bytes are cleared as part of ordinary traffic, not a new worker.
            db.execute(
                "UPDATE knowledge_content_uploads SET raw=NULL WHERE expires_at<=? AND raw IS NOT NULL",
                (utc(self.service.clock()),),
            )
            upload_id = new_id("upload")
            descriptor = {k: request[k] for k in ("filename", "media_type", "size", "sha256")}
            db.execute(
                "INSERT INTO knowledge_content_uploads VALUES (?,?,?,?,?,?,?)",
                (
                    upload_id,
                    canonical(authority["owner"]),
                    canonical(descriptor),
                    None,
                    "pending",
                    utc(self.service.clock() + timedelta(hours=1)),
                    None,
                ),
            )
            result = self._upload_response(
                self._upload(db, upload_id, authority), authority["request_id"]
            )
            return self._save(db, "uploads", request, authority, result)

    def upload_status(self, request, authority):
        with self.service.store.transaction() as db:
            self._check(db, authority)
            return self._upload_response(
                self._upload(db, request["upload_id"], authority), authority["request_id"]
            )

    def upload_size(self, upload_id, authority):
        with self.service.store.transaction() as db:
            self._check(db, authority)
            return json.loads(self._upload(db, upload_id, authority)["descriptor"])["size"]

    def put_upload(self, upload_id, raw, authority):
        with self.service.store.transaction() as db:
            self._check(db, authority)
            row = self._upload(db, upload_id, authority)
            descriptor = json.loads(row["descriptor"])
            require(
                len(raw) == descriptor["size"] and content_hash(raw) == descriptor["sha256"],
                "invalid_input",
                400,
            )
            if row["state"] == "pending":
                db.execute(
                    "UPDATE knowledge_content_uploads SET raw=?,state='complete' WHERE id=?",
                    (raw, upload_id),
                )
            return self._upload_response(
                self._upload(db, upload_id, authority), authority["request_id"]
            )

    def acquire(self, request, authority, refresh):
        source = request["source"]
        source_time = None
        with self.service.store.transaction() as db:
            self._check(db, authority)
            prior = self._prior(db, "acquire", request, authority)
            if prior:
                return self._response(
                    self._document(db, prior["content_ref"], authority), authority
                )
            if source["kind"] == "upload":
                upload = self._upload(db, source["upload_id"], authority)
                if upload["state"] == "imported":
                    row = db.execute(
                        "SELECT d.*,c.owner,c.metadata,c.access_version,v.hash,v.provenance FROM knowledge_documents d JOIN knowledge_content_documents c ON c.document_id=d.id JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.version WHERE d.id=?",
                        (upload["document_id"],),
                    ).fetchone()
                    result = self._response(
                        self._document(db, self._ref(row), authority), authority
                    )
                    return self._save(db, "acquire", request, authority, result)
                require(upload["state"] == "complete", "invalid_input", 409)
                descriptor = json.loads(upload["descriptor"])
                raw, content_type, resolved = upload["raw"], descriptor["media_type"], None
        if source["kind"] == "url":
            raw, content_type, resolved, source_time = fetch_url(
                source["url"],
                self.auth.config().get("knowledge_content", {}).get("trusted_urls", []),
                submitted=True,
                media_types=media.MEDIA_TYPES,
                max_bytes=media.MAX_BYTES,
                include_time=True,
            )
        prepared = media.prepare(raw, content_type, resolved)
        authority = refresh()
        locator = source.get("url", source.get("upload_id"))
        source_id = "source:" + fingerprint([authority["owner"], source["kind"], locator])
        document_id = "content:" + fingerprint(source_id)
        with self.service.store.transaction() as db:
            self._check(db, authority)
            prior = self._prior(db, "acquire", request, authority)
            if prior:
                return self._response(
                    self._document(db, prior["content_ref"], authority), authority
                )
            old = db.execute(
                "SELECT d.*,v.hash FROM knowledge_documents d JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.version WHERE d.id=?",
                (document_id,),
            ).fetchone()
            require(old is None or old["state"] == "ready", "not_found", 404)
            if old is None or old["hash"] != prepared["digest"]:
                version = old["version"] + 1 if old else 1
                store_version(
                    db,
                    None,
                    document_id,
                    source_id,
                    source["kind"],
                    locator,
                    prepared,
                    old,
                    version,
                )
                metadata = {
                    "kind": source["kind"],
                    "locator": locator,
                    "resolved_url": resolved,
                    "filename": descriptor["filename"] if source["kind"] == "upload" else None,
                    "acquired_at": utc(self.service.clock()),
                    "source_time": source_time,
                    "media_type": content_type,
                    "size": len(raw),
                }
                db.execute(
                    "INSERT INTO knowledge_content_documents VALUES (?,?,?,1) ON CONFLICT(document_id) DO UPDATE SET metadata=excluded.metadata,access_version=access_version+1",
                    (document_id, canonical(authority["owner"]), canonical(metadata)),
                )
            if source["kind"] == "upload":
                current_upload = self._upload(db, source["upload_id"], authority)
                require(
                    current_upload["state"] in {"complete", "imported"}, "version_conflict", 409
                )
                db.execute(
                    "UPDATE knowledge_content_uploads SET state='imported',raw=NULL,document_id=? WHERE id=?",
                    (document_id, source["upload_id"]),
                )
            row = db.execute(
                "SELECT d.*,c.owner,c.metadata,c.access_version,v.hash,v.provenance FROM knowledge_documents d JOIN knowledge_content_documents c ON c.document_id=d.id JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.version WHERE d.id=?",
                (document_id,),
            ).fetchone()
            return self._save(db, "acquire", request, authority, self._response(row, authority))

    def read(self, request, authority, refresh):
        with self.service.store.transaction() as db:
            self._check(db, authority)
            row = dict(self._document(db, request["content_ref"], authority))
        decoded = media.read(row, request["range"], request["budget_bytes"])
        authority = refresh()
        with self.service.store.transaction() as db:
            self._check(db, authority)
            current = self._document(db, request["content_ref"], authority)
            require(current["access_version"] == row["access_version"], "scope_changed", 409)
            return self._response(current, authority) | decoded

    def original(self, request, authority):
        with self.service.store.transaction() as db:
            self._check(db, authority)
            row = self._document(db, request["content_ref"], authority)
            raw, selected = row["raw"], request["range"]
            coverage = {"unit": "bytes", "start": 0, "end": len(raw)}
            if selected is not None:
                require(
                    selected["unit"] == "bytes"
                    and type(selected["start"]) is int
                    and type(selected["end"]) is int
                    and 0 <= selected["start"] < selected["end"] <= len(raw),
                    "invalid_input",
                    416,
                )
                coverage = selected
                raw = raw[selected["start"] : selected["end"]]
            return raw, {
                "Content-Type": row["media_type"],
                "X-Content-SHA256": content_hash(raw),
                "X-Source-SHA256": row["hash"],
                "X-Content-Version": str(row["version"]),
                "X-Content-Coverage": canonical(coverage),
                "Cache-Control": "no-store",
            }

    def access(self, request, authority):
        with self.service.store.transaction() as db:
            self._check(db, authority)
            prior = self._prior(db, "access", request, authority)
            if prior:
                return prior
            row = self._document(db, request["content_ref"], authority, owner_only=True)
            require(
                row["access_version"] == request["expected_access_version"], "version_conflict", 409
            )
            action = request["action"]
            if action == "withdraw":
                db.execute(
                    "UPDATE knowledge_documents SET state='deleted' WHERE id=?", (row["id"],)
                )
                db.execute(
                    "DELETE FROM knowledge_index WHERE block_id IN (SELECT id FROM knowledge_blocks WHERE document_id=?)",
                    (row["id"],),
                )
            else:
                scope = request["reader_scope"]
                actor = authority["owner"].get(
                    "actor_id", authority["owner"].get("scope", {}).get("actor_id")
                )
                require(scope["actor_id"] == actor)
                require(
                    db.execute("SELECT 1 FROM people WHERE id=?", (scope["person_id"],)).fetchone()
                    is not None,
                    "not_found",
                    404,
                )
                db.execute(
                    "INSERT INTO knowledge_content_grants VALUES (?,?,?,?,?) ON CONFLICT(document_id,reader) DO UPDATE SET version=excluded.version,hash=excluded.hash,enabled=excluded.enabled",
                    (
                        row["id"],
                        canonical(scope),
                        row["version"],
                        row["hash"],
                        int(action == "grant"),
                    ),
                )
            db.execute(
                "UPDATE knowledge_content_documents SET access_version=access_version+1 WHERE document_id=?",
                (row["id"],),
            )
            return self._save(
                db,
                "access",
                request,
                authority,
                {
                    "schema_version": 1,
                    "request_id": authority["request_id"],
                    "content_ref": request["content_ref"],
                    "state": {"grant": "granted", "revoke": "revoked", "withdraw": "withdrawn"}[
                        action
                    ],
                    "access_version": row["access_version"] + 1,
                },
            )
