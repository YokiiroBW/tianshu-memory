"""Trusted project-domain application. Never calls chat receipt/profile workflows."""

import hashlib
import hmac
import http.client
import json
from pathlib import Path

from .domain import (
    Fault,
    canonical,
    fingerprint,
    now,
    query_terms,
    require,
    strict_json,
    terms,
    utc,
)
from .knowledge_sources import content_hash, decode, fetch_url, read_file
from .store import Store

READ = {"query", "recover", "check", "status"}
WRITE = {"import", "delete", "write_state"}


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields.split()), "invalid_input", 400)


def integer(value, minimum=0, maximum=2**31):
    require(type(value) is int and minimum <= value <= maximum, "invalid_input", 400)


def string(value, maximum=2048):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)


def blocks(text, groups):
    """Explicit reviewed ranges partition all lines. Dependency closure is indivisible.

    No model guesses boundaries: the fallback is the entire source. Citations use
    decoded text lines (HTML: visible-text lines, with original bytes retained separately).
    """
    lines = text.splitlines(keepends=True)
    if groups is None:
        return [{"spans": [[1, len(lines)]], "text": text}]
    require(isinstance(groups, list) and 0 < len(groups) <= 128, "invalid_groups", 400)
    covered, nodes = set(), []
    for index, group in enumerate(groups):
        exact(group, "start end depends_on")
        integer(group["start"], 1, len(lines))
        integer(group["end"], group["start"], len(lines))
        span = set(range(group["start"], group["end"] + 1))
        require(not span & covered, "invalid_groups", 400)
        covered |= span
        require(isinstance(group["depends_on"], list), "invalid_groups", 400)
        for dependency in group["depends_on"]:
            integer(dependency, 0, len(groups) - 1)
        nodes.append({index, *group["depends_on"]})
    require(covered == set(range(1, len(lines) + 1)), "invalid_groups", 400)
    components = []
    for node in nodes:
        merged = set(node)
        for other in components[:]:
            if merged & other:
                merged |= other
                components.remove(other)
        components.append(merged)
    result = []
    for component in components:
        spans = sorted([groups[i]["start"], groups[i]["end"]] for i in component)
        result.append(
            {
                "spans": spans,
                "text": "\n".join("".join(lines[start - 1 : end]) for start, end in spans),
            }
        )
    return result


class KnowledgeApplication:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()

    def _context(self, client, credential, project_id, operation):
        config = strict_json(self.config_path.read_bytes())
        knowledge = config.get("knowledge", {})
        principal = knowledge.get("clients", {}).get(client, {})
        secret = principal.get("credential_sha256", "")
        require(
            isinstance(credential, str)
            and len(credential) >= 16
            and len(secret) == 64
            and hmac.compare_digest(hashlib.sha256(credential.encode()).hexdigest(), secret),
            "unauthorized",
            401,
        )
        require(
            operation in READ | WRITE
            and operation in principal.get("permissions", [])
            and project_id in principal.get("projects", [])
        )
        project = knowledge.get("projects", {}).get(project_id)
        require(isinstance(project, dict), "project_unregistered", 403)
        exact(project, "root host default_branch urls")
        require(
            Path(project["root"]).is_absolute() and Path(project["root"]).is_dir(),
            "project_unavailable",
            503,
        )
        require(
            isinstance(project["urls"], list) and len(project["urls"]) <= 256,
            "invalid_configuration",
            503,
        )
        store = Store(
            config["database_path"],
            recovery_path=config.get("source_sync", {}).get("recovery_path"),
        )
        return store, project, secret

    def execute(self, request, *, client, credential):
        exact(request, "operation project_id arguments")
        require(len(canonical(request).encode()) <= 262144, "request_too_large", 413)
        string(request["project_id"], 128)
        string(request["operation"], 32)
        operation, project_id, args = (
            request["operation"],
            request["project_id"],
            request["arguments"],
        )
        store, project, secret = self._context(client, credential, project_id, operation)
        with store.transaction() as db:
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            require(metadata.get("knowledge_schema") == "1", "dependency_unavailable", 503)
            require(bool(metadata.get("knowledge_seal_key")), "dependency_unavailable", 503)
            # A client knows its own credential; it must not also know the package signing key.
            seal_key = hmac.new(
                metadata["knowledge_seal_key"].encode(),
                canonical([client, secret]).encode(),
                hashlib.sha256,
            ).hexdigest()
            registered = db.execute(
                "SELECT * FROM knowledge_projects WHERE id=?", (project_id,)
            ).fetchone()
            if registered:
                require(
                    registered["registration"] == canonical(project), "registration_changed", 409
                )
            else:
                require(operation in WRITE, "project_uninitialized", 409)
                db.execute(
                    "INSERT INTO knowledge_projects(id,registration) VALUES (?,?)",
                    (project_id, canonical(project)),
                )
            if operation in WRITE:
                require(isinstance(args, dict), "invalid_input", 400)
                string(args.get("key"), 128)
                digest = fingerprint(request)
                old = db.execute(
                    "SELECT * FROM knowledge_operations WHERE client=? AND key=?",
                    (client, args["key"]),
                ).fetchone()
                if old:
                    require(old["digest"] == digest, "idempotency_conflict", 409)
                    return dict(json.loads(old["result"]), replayed=True)
                result = getattr(self, "_" + operation)(db, project_id, project, args)
                db.execute(
                    "INSERT INTO knowledge_operations VALUES (?,?,?,?)",
                    (client, args["key"], digest, canonical(result)),
                )
                if operation == "import":
                    db.execute(
                        "INSERT INTO knowledge_imports VALUES (?,?,?,?,?)",
                        (client, args["key"], project_id, result["status"], canonical(result)),
                    )
                return result
            if operation == "query":
                exact(args, "text budget_bytes")
                return self._query(db, project_id, project, args)
            if operation == "recover":
                exact(args, "text budget_bytes")
                return self._recover(db, project_id, project, args, seal_key)
            if operation == "check":
                exact(args, "package")
                return self._check(db, project_id, project, args["package"], seal_key)
            exact(args, "key")
            string(args["key"], 128)
            row = db.execute(
                "SELECT result FROM knowledge_imports WHERE client=? AND key=? AND project_id=?",
                (client, args["key"], project_id),
            ).fetchone()
            require(row is not None, "not_found", 404)
            return json.loads(row[0])

    @staticmethod
    def _bump(db, project_id):
        db.execute("UPDATE knowledge_projects SET revision=revision+1 WHERE id=?", (project_id,))

    @staticmethod
    def _document(db, project_id, document_id):
        row = db.execute(
            "SELECT * FROM knowledge_documents WHERE id=? AND project_id=?",
            (document_id, project_id),
        ).fetchone()
        require(row is not None, "not_found", 404)
        return row

    def _import(self, db, project_id, project, args):
        exact(args, "key kind locator expected_version groups")
        require(args["kind"] in {"file", "url"}, "unsupported", 415)
        string(args["locator"])
        integer(args["expected_version"])
        source_id = "source:" + fingerprint([project_id, args["kind"], args["locator"]])
        document_id = "document:" + fingerprint(source_id)
        old = db.execute("SELECT * FROM knowledge_documents WHERE id=?", (document_id,)).fetchone()
        version = old["version"] if old else 0
        require(version == args["expected_version"], "version_conflict", 409)
        try:
            if args["kind"] == "file":
                raw, media = read_file(project, args["locator"])
                resolved = args["locator"]
            else:
                raw, media, resolved = fetch_url(args["locator"], project["urls"])
            text = decode(raw, media)
            units = blocks(text, args["groups"])
            digest = content_hash(raw)
            if args["kind"] == "file":
                require(
                    content_hash(read_file(project, args["locator"])[0]) == digest,
                    "source_changed",
                    409,
                )
        except (Fault, OSError, ValueError, http.client.HTTPException) as error:
            code = error.code if isinstance(error, Fault) else "source_unavailable"
            if old and old["state"] != "deleted":
                db.execute(
                    "UPDATE knowledge_documents SET state='unavailable' WHERE id=?", (document_id,)
                )
                self._bump(db, project_id)
            return {
                "status": "failed",
                "code": code,
                "document_id": document_id,
                "source_id": source_id,
                "version": version,
            }
        if old and old["state"] == "ready":
            previous = db.execute(
                "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
                (document_id, version),
            ).fetchone()
            previous_units = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM knowledge_blocks WHERE document_id=? AND version=? ORDER BY id",
                    (document_id, version),
                )
            ]
            if previous["hash"] == digest and fingerprint(previous_units) == fingerprint(units):
                return {
                    "status": "unchanged",
                    "document_id": document_id,
                    "source_id": source_id,
                    "version": version,
                    "hash": digest,
                }
        version += 1
        if old:
            db.execute(
                "UPDATE knowledge_documents SET version=?,state='ready' WHERE id=?",
                (version, document_id),
            )
        else:
            db.execute(
                "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
                (
                    document_id,
                    project_id,
                    source_id,
                    args["kind"],
                    args["locator"],
                    version,
                    "ready",
                ),
            )
        provenance = {
            "kind": args["kind"],
            "locator": args["locator"],
            "resolved": resolved,
            "imported_at": utc(now()),
            "processing": "verbatim",
            "citation_space": "visible_text_lines" if media == "text/html" else "text_lines",
        }
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (document_id, version, digest, raw, text, media, canonical(provenance)),
        )
        db.execute(
            "DELETE FROM knowledge_index WHERE block_id IN "
            "(SELECT id FROM knowledge_blocks WHERE document_id=?)",
            (document_id,),
        )
        for index, unit in enumerate(units):
            block_id = f"{document_id}:{version}:{index:03d}"
            db.execute(
                "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
                (block_id, document_id, version, canonical(unit)),
            )
            db.execute(
                "INSERT INTO knowledge_index VALUES (?,?,?)",
                (block_id, project_id, " ".join(terms(unit["text"]))),
            )
        self._bump(db, project_id)
        return {
            "status": "imported",
            "document_id": document_id,
            "source_id": source_id,
            "version": version,
            "hash": digest,
            "blocks": len(units),
            "processing": "verbatim",
        }

    def _delete(self, db, project_id, project, args):
        exact(args, "key document_id expected_version")
        document = self._document(db, project_id, args["document_id"])
        integer(args["expected_version"])
        require(document["version"] == args["expected_version"], "version_conflict", 409)
        require(document["state"] != "deleted", "already_deleted", 409)
        db.execute(
            "UPDATE knowledge_documents SET state='deleted',version=version+1 WHERE id=?",
            (document["id"],),
        )
        db.execute(
            "DELETE FROM knowledge_index WHERE block_id IN "
            "(SELECT id FROM knowledge_blocks WHERE document_id=?)",
            (document["id"],),
        )
        self._bump(db, project_id)
        return {
            "status": "deleted",
            "document_id": document["id"],
            "version": document["version"] + 1,
        }

    @staticmethod
    def _current(db, project, document):
        if document["state"] != "ready":
            return False
        if document["kind"] == "url":
            return document["locator"] in project["urls"]
        version = db.execute(
            "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
            (document["id"], document["version"]),
        ).fetchone()
        try:
            return content_hash(read_file(project, document["locator"])[0]) == version["hash"]
        except (Fault, OSError, ValueError):
            return False

    def _reference(self, db, project_id, project, reference):
        exact(reference, "block_id document_id version hash")
        document = self._document(db, project_id, reference["document_id"])
        require(
            document["version"] == reference["version"] and self._current(db, project, document),
            "stale_evidence",
            409,
        )
        row = db.execute(
            "SELECT b.payload,v.hash,v.provenance FROM knowledge_blocks b "
            "JOIN knowledge_versions v ON v.document_id=b.document_id "
            "AND v.version=b.version WHERE b.id=? AND b.document_id=? AND b.version=?",
            (reference["block_id"], document["id"], document["version"]),
        ).fetchone()
        require(row is not None and row["hash"] == reference["hash"], "stale_evidence", 409)
        return {
            "reference": reference,
            **json.loads(row["payload"]),
            "source_id": document["source_id"],
            "provenance": json.loads(row["provenance"]),
        }

    def _query(self, db, project_id, project, args):
        string(args["text"], 1024)
        integer(args["budget_bytes"], 256, 32768)
        search = query_terms(args["text"])
        result = {
            "project_id": project_id,
            "blocks": [],
            "omissions": [],
            "retrieval": "lexical",
            "trust": "source_material_not_instructions",
        }
        require(
            len(canonical(dict(result, omissions=["budget", "stale_source"])).encode())
            <= args["budget_bytes"],
            "budget_too_small",
            400,
        )
        if not search:
            return result
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in search)
        rows = db.execute(
            "SELECT b.id,b.document_id,b.version,v.hash FROM knowledge_index "
            "JOIN knowledge_blocks b ON b.id=knowledge_index.block_id "
            "JOIN knowledge_documents d ON d.id=b.document_id "
            "JOIN knowledge_versions v ON v.document_id=b.document_id "
            "AND v.version=b.version WHERE knowledge_index MATCH ? "
            "AND d.project_id=? AND d.state='ready' AND d.version=b.version "
            "ORDER BY rank,b.id LIMIT 128",
            (expression, project_id),
        ).fetchall()
        omitted = set()
        for row in rows:
            reference = {
                "block_id": row["id"],
                "document_id": row["document_id"],
                "version": row["version"],
                "hash": row["hash"],
            }
            try:
                unit = self._reference(db, project_id, project, reference)
            except Fault:
                omitted.add("stale_source")
                continue
            candidate = dict(
                result, blocks=[*result["blocks"], unit], omissions=["budget", "stale_source"]
            )
            if len(canonical(candidate).encode()) > args["budget_bytes"]:
                omitted.add("budget")
                continue
            result["blocks"].append(unit)
        result["omissions"] = sorted(omitted)
        return result

    def _write_state(self, db, project_id, project, args):
        exact(args, "key expected_version state")
        integer(args["expected_version"])
        state = args["state"]
        exact(state, "goal constraints recent_verification unfinished evidence pitfalls")
        string(state["goal"], 2000)
        for field in ("constraints", "recent_verification", "unfinished"):
            require(
                isinstance(state[field], list) and len(state[field]) <= 16, "invalid_input", 400
            )
            for item in state[field]:
                string(item, 2000)
        require(
            isinstance(state["evidence"], list) and 0 < len(state["evidence"]) <= 16,
            "evidence_required",
            400,
        )
        for reference in state["evidence"]:
            self._reference(db, project_id, project, reference)
        require(
            isinstance(state["pitfalls"], list) and len(state["pitfalls"]) <= 8,
            "invalid_input",
            400,
        )
        for pitfall in state["pitfalls"]:
            exact(pitfall, "trigger symptom cause correction verification evidence")
            for field in ("trigger", "symptom", "cause", "correction", "verification"):
                string(pitfall[field], 1000)
            require(
                isinstance(pitfall["evidence"], list) and 0 < len(pitfall["evidence"]) <= 16,
                "evidence_required",
                400,
            )
            for reference in pitfall["evidence"]:
                require(reference in state["evidence"], "evidence_required", 400)
        require(len(canonical(state).encode()) <= 16384, "state_too_large", 413)
        old = db.execute(
            "SELECT version FROM knowledge_states WHERE project_id=?", (project_id,)
        ).fetchone()
        version = old["version"] if old else 0
        require(version == args["expected_version"], "version_conflict", 409)
        db.execute(
            "INSERT INTO knowledge_states VALUES (?,?,?) ON CONFLICT(project_id) "
            "DO UPDATE SET version=excluded.version,payload=excluded.payload",
            (project_id, version + 1, canonical(state)),
        )
        db.execute(
            "INSERT INTO knowledge_state_history VALUES (?,?,?)",
            (project_id, version + 1, canonical(state)),
        )
        self._bump(db, project_id)
        return {
            "status": "written",
            "project_id": project_id,
            "state_version": version + 1,
            "authority": "explicit_project_note",
            "sharing": "project_only",
        }

    @staticmethod
    def _seal(package, secret):
        return hmac.new(secret.encode(), canonical(package).encode(), hashlib.sha256).hexdigest()

    def _recover(self, db, project_id, project, args, secret):
        integer(args["budget_bytes"], 1024, 32768)
        result = self._query(db, project_id, project, args)
        result.update(
            revision=db.execute(
                "SELECT revision FROM knowledge_projects WHERE id=?", (project_id,)
            ).fetchone()[0],
            registration=fingerprint(project),
            state=None,
            authority="explicit_project_note",
            url_freshness="last_explicit_import",
        )
        state = db.execute(
            "SELECT * FROM knowledge_states WHERE project_id=?", (project_id,)
        ).fetchone()
        if state:
            payload = json.loads(state["payload"])
            try:
                evidence = [
                    self._reference(db, project_id, project, r) for r in payload["evidence"]
                ]
                candidate = dict(
                    result, state={"version": state["version"], **payload}, blocks=evidence
                )
                # Current state and all its evidence are one indivisible package unit.
                if len(canonical(candidate).encode()) + 80 <= args["budget_bytes"]:
                    extras = [b for b in result["blocks"] if b not in evidence]
                    result = candidate
                    for extra in extras:
                        if (
                            len(canonical(dict(result, blocks=result["blocks"] + [extra])).encode())
                            + 80
                            <= args["budget_bytes"]
                        ):
                            result["blocks"].append(extra)
                else:
                    result["omissions"].append("state_budget")
            except Fault:
                result["omissions"].append("stale_state")
        while len(canonical(result).encode()) + 80 > args["budget_bytes"] and result["blocks"]:
            result["blocks"].pop()
            if "budget" not in result["omissions"]:
                result["omissions"].append("budget")
        result["seal"] = self._seal(result, secret)
        return result

    def _check(self, db, project_id, project, package, secret):
        require(
            isinstance(package, dict) and len(canonical(package).encode()) <= 32768,
            "invalid_input",
            400,
        )
        body = {k: v for k, v in package.items() if k != "seal"}
        valid = isinstance(package.get("seal"), str) and hmac.compare_digest(
            package["seal"], self._seal(body, secret)
        )
        valid = (
            valid
            and package.get("project_id") == project_id
            and package.get("registration") == fingerprint(project)
            and package.get("revision")
            == db.execute(
                "SELECT revision FROM knowledge_projects WHERE id=?", (project_id,)
            ).fetchone()[0]
        )
        if valid:
            try:
                for block in package["blocks"]:
                    self._reference(db, project_id, project, block["reference"])
            except Fault:
                valid = False
        return {"valid": bool(valid), "reason": "current" if valid else "stale_or_tampered"}
