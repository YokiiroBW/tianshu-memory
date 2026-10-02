"""Versioned project lesson book and explicitly approved global experience.

Reuses the project identity, authorization, Store transaction and source-evidence checks of
the knowledge domain. No model judgement, no automatic promotion and no chat receipt: a
lesson is an operator statement with current source evidence, and a global experience entry
exists only because an operator holding the separate promote permission approved it with
evidence from at least two different projects that the same operator is authorized to read.
"""

import hashlib
import hmac
import json

from .domain import (
    Fault,
    canonical,
    fingerprint,
    query_terms,
    require,
    terms,
)
from .knowledge_sources import FileReader

LESSON_FIELDS = "trigger symptom cause correction verification scope evidence".split()
SCOPE_FIELDS = "platform language framework applies_to excludes"
ENTRY_FIELDS = "title rule applicability excludes counterexamples recheck_after evidence".split()
REFERENCE_FIELDS = "lesson_id version hash"
BLOCK_FIELDS = "block_id document_id version hash"
MINIMUM_EVIDENCE_PROJECTS = 2
MAX_CANDIDATES = 128


def text(value, maximum=1000):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)
    return value


def listing(value, maximum=2000, count=16):
    require(isinstance(value, list) and 0 < len(value) <= count, "invalid_input", 400)
    for item in value:
        text(item, maximum)
    return value


def exact_fields(value, fields, code="invalid_input"):
    require(isinstance(value, dict) and set(value) == set(fields.split()), code, 400)


def lesson_id(project_id, key):
    return "lesson:" + fingerprint([project_id, key])


def lesson_of(payload):
    """The stable identity key a stored lesson version was recorded under."""
    return payload.get("key")


def experience_id(project_id, key):
    return "experience:" + fingerprint([project_id, key])


def validate_scope(scope):
    exact_fields(scope, SCOPE_FIELDS)
    text(scope["platform"], 64)
    text(scope["language"], 64)
    text(scope["framework"], 64)
    listing(scope["applies_to"], 200, 8)
    listing(scope["excludes"], 200, 8)


def validate_blocks(evidence):
    require(isinstance(evidence, list) and 0 < len(evidence) <= 16, "evidence_required", 400)
    for reference in evidence:
        exact_fields(reference, BLOCK_FIELDS, "evidence_required")
        text(reference["block_id"], 2048)
        text(reference["document_id"], 2048)
        require(
            type(reference["version"]) is int and reference["version"] >= 1, "invalid_input", 400
        )
        text(reference["hash"], 64)


def validate_lesson(lesson):
    exact_fields(lesson, " ".join(LESSON_FIELDS))
    for field in ("trigger", "symptom", "cause", "correction", "verification"):
        text(lesson[field], 1000)
    validate_scope(lesson["scope"])
    validate_blocks(lesson["evidence"])


def validate_entry(entry):
    exact_fields(entry, " ".join(ENTRY_FIELDS))
    text(entry["title"], 200)
    text(entry["rule"], 2000)
    listing(entry["applicability"], 500, 16)
    listing(entry["excludes"], 500, 16)
    listing(entry["counterexamples"], 500, 16)
    require(
        entry["recheck_after"] is None
        or (isinstance(entry["recheck_after"], str) and 0 < len(entry["recheck_after"]) <= 32),
        "invalid_input",
        400,
    )
    require(isinstance(entry["evidence"], list), "evidence_required", 400)


def validate_references(references, minimum=1, maximum=16):
    require(
        isinstance(references, list) and minimum <= len(references) <= maximum,
        "evidence_required",
        400,
    )
    for reference in references:
        exact_fields(
            reference, " ".join([*REFERENCE_FIELDS.split(), "project_id"]), "evidence_required"
        )
        text(reference["lesson_id"], 128)
        text(reference["project_id"], 128)
        require(
            type(reference["version"]) is int and reference["version"] >= 1, "invalid_input", 400
        )
        text(reference["hash"], 64)


def evidence_projects(args):
    """Registered project ids a caller-supplied global evidence list names.

    Only ids the caller states are returned; this never discovers or enumerates projects.
    A lesson recovery package cites no other project and therefore yields none.
    """
    package = args.get("package") if isinstance(args, dict) else None
    references = package.get("evidence") if isinstance(package, dict) else None
    if not isinstance(references, list):
        return ()
    found = set()
    for reference in references:
        require(isinstance(reference, dict), "invalid_input", 400)
        found.add(reference.get("project_id"))
    require(all(isinstance(value, str) and value for value in found), "invalid_input", 400)
    return sorted(found)


def authorized_projects(principal):
    """Every registered project this principal may both reach and name in its operations.

    Global retrieval is bounded by this set, so a caller cannot broaden its view by asking
    for a project it is not registered for, and cannot learn that the project exists.
    """
    registered = set(principal.get("projects", []))
    permissions = set(principal.get("permissions", []))
    return sorted(
        project
        for project in registered
        if permissions & {"query", "promote", "review", "lesson_query", "lesson_recover"}
    )


def lesson_projects(args):
    """Projects an authored global evidence list names, without judging its content.

    The reference shape is validated later, inside the transaction. This only has to name
    the projects so that an unauthorized one is refused before any data is read, and to
    reject a list that cannot possibly carry two-project evidence.
    """
    found = set()
    entries = args.get("entry") if isinstance(args, dict) else None
    references = entries.get("evidence") if isinstance(entries, dict) else None
    if not isinstance(references, list):
        return ()
    require(len(references) >= MINIMUM_EVIDENCE_PROJECTS, "evidence_required", 400)
    for reference in references:
        if not isinstance(reference, dict):
            continue
        project = reference.get("project_id")
        if isinstance(project, str) and project:
            found.add(project)
    return sorted(found)


def snapshot(db, project_id, project, client, secret, operation):
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    require(
        metadata.get("knowledge_schema") == "1" and bool(metadata.get("knowledge_seal_key")),
        "dependency_unavailable",
        503,
    )
    require(metadata.get("lessons_schema") == "1", "dependency_unavailable", 503)
    seal_key = hmac.new(
        metadata["knowledge_seal_key"].encode(),
        canonical([client, secret]).encode(),
        hashlib.sha256,
    ).hexdigest()
    registered = db.execute("SELECT * FROM knowledge_projects WHERE id=?", (project_id,)).fetchone()
    if registered:
        require(registered["registration"] == canonical(project), "registration_changed", 409)
    else:
        require(operation in {"lesson_record", "experience_promote"}, "project_uninitialized", 409)
    return (registered["revision"] if registered else None), seal_key


def index_text(lesson):
    return " ".join(
        terms(
            " ".join(
                lesson[field]
                for field in ("trigger", "symptom", "cause", "correction", "verification")
            )
        )
    )


def entry_text(entry):
    return " ".join(
        terms(
            " ".join(
                [
                    entry["title"],
                    entry["rule"],
                    *entry["applicability"],
                    *entry["excludes"],
                    *entry["counterexamples"],
                ]
            )
        )
    )


def lesson_hash(payload, block_hashes):
    """Content hash of one lesson version, bound to the content hashes of its sources.

    Re-importing identical bytes keeps the same fingerprint; changing or deleting a source
    changes it, which is what global experience review and lesson reads rely on.
    """
    return fingerprint([payload, sorted(block_hashes)])


class SourceContext:
    """One registered project bound to one dispatch phase.

    The project dictionary, the project row and the file reader are captured together, so
    evidence taken for a lesson or a promoted entry always belongs to the transaction that
    commits it. The reader is installed before `Evidence` captures it, because that object
    holds the callable rather than looking it up per call.
    """

    def __init__(self, project, lookup, db, project_id, *, reader, phase):
        from .knowledge_evidence import Evidence

        self.lookup = lookup
        self.db = db
        self.project_id = project_id
        self.project = project
        require(isinstance(self.project, dict), "project_unregistered", 403)
        require(
            set(self.project) == {"root", "host", "default_branch", "urls"},
            "invalid_configuration",
            503,
        )
        self.reader = reader
        self.read = getattr(reader, phase)
        self.evidence = Evidence(lookup, db, project_id, self.project, self.read)
        self.revision = self._revision()

    def _revision(self):
        row = self.db.execute(
            "SELECT revision FROM knowledge_projects WHERE id=?", (self.project_id,)
        ).fetchone()
        require(row is not None, "project_uninitialized", 409)
        return row["revision"]

    def blocks(self, reference):
        return self.evidence.reference(reference)

    def observe(self, reference):
        """Record the expectation for one unit, answering whether it is currently valid.

        `blocks` is the strict check a write needs: a unit that is not current raises. A read
        needs the opposite shape — it must be able to describe a citation that has expired
        without failing the whole request — but it still has to make the capture phase record
        what it saw. This returns that answer instead of raising, and only ever for the citation
        kinds the phase can judge; the caller decides what an expired citation means.
        """
        try:
            self.blocks(reference)
        except Fault:
            return False
        return True

    def revision_unchanged(self):
        """Whether the project revision is still the one this context captured.

        A narrow public read for callers that verify their own evidence units but must still
        confirm that no other project write happened in between. It reads one row and performs
        no external I/O, so it is safe in a phase that must not touch the filesystem.
        """
        return self._revision() == self.revision

    def current_hash(self, document_id, version):
        """The current hash of one referenced document version, by source kind.

        A `file` document is re-read from disk and refused if the bytes changed. A `url`
        document is a snapshot taken at the last explicit import: its authority is the stored
        version plus the caller's project still registering that exact URL. Its locator is
        never handed to the file reader and no network request is made here, so composing or
        reading an experience never becomes an unrequested fetch.
        """
        document = self.lookup.document(self.db, self.project_id, document_id)
        require(document["version"] == version, "stale_evidence", 409)
        stored = self.db.execute(
            "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
            (document["id"], document["version"]),
        ).fetchone()
        require(stored is not None, "stale_evidence", 409)
        if document["kind"] == "url":
            require(
                isinstance(self.project, dict) and document["locator"] in self.project["urls"],
                "stale_evidence",
                409,
            )
            return stored["hash"]
        require(self.read(document, stored["hash"]), "stale_evidence", 409)
        return self.reader.hash_of(document["locator"])

    def verify(self, references):
        """Verify source blocks and re-check that the project did not change meanwhile."""
        verified = [self.blocks(reference) for reference in references]
        require(self._revision() == self.revision, "project_conflict", 409)
        return verified

    def lesson(self, reference, *, phase):
        """Load and verify one lesson version, returning the row and its content hash.

        Every phase verifies the current document version and tombstone state. The final
        `serve` phase additionally compares the fingerprint built from the hashes that are
        current now against the recorded `reference["hash"]`, for every source kind: a URL
        snapshot yields its stored digest, so the comparison must not depend on whether any
        file happened to be read. Only `capture` defers that comparison, because it runs
        before the external phase reads the files.
        """
        row = self.db.execute(
            "SELECT * FROM lessons WHERE id=? AND project_id=?",
            (reference["lesson_id"], self.project_id),
        ).fetchone()
        require(row is not None and row["version"] == reference["version"], "stale_evidence", 409)
        payload = json.loads(row["payload"])
        views = self.verify(payload["evidence"])
        hashes = [
            self.current_hash(view["reference"]["document_id"], view["reference"]["version"])
            for view in views
        ]
        require(payload["project_id"] == self.project_id, "stale_evidence", 409)
        if phase != "capture":
            require(lesson_hash(payload, hashes) == reference["hash"], "stale_evidence", 409)
        return row, payload


class LessonReader(FileReader):
    """Project file freshness with the lesson domain's hash projection."""

    def hash_of(self, locator):
        """The hash this phase established. Capture performs no file I/O."""
        return self.cache.get(locator)


class LessonBook:
    # -- validation --------------------------------------------------------------

    @staticmethod
    def blocks(sources, db, project_id, references, *, phase):
        """Validate source blocks in one phase, returning their views."""
        return sources.context(db, project_id, phase).verify(references)

    @staticmethod
    def lesson_view(sources, db, project_id, reference, *, phase):
        """Validate one lesson version, including the current state of its sources."""
        return sources.context(db, project_id, phase).lesson(reference, phase=phase)

    # -- writes ------------------------------------------------------------------

    def record(self, db, application, sources, project_id, args, *, phase):
        required = "key expected_version lesson"
        exact_fields(args, f"{required} dedupe" if "dedupe" in args else required)
        application.string(args["key"], 128)
        application.integer(args["expected_version"], 0)
        validate_lesson(args["lesson"])
        require(len(canonical(args["lesson"]).encode()) <= 16384, "lesson_too_large", 413)
        identifier = lesson_id(project_id, args["key"])
        old = db.execute("SELECT * FROM lessons WHERE id=?", (identifier,)).fetchone()
        require((old["version"] if old else 0) == args["expected_version"], "version_conflict", 409)
        views = self.blocks(sources, db, project_id, args["lesson"]["evidence"], phase=phase)
        if phase == "capture":
            return None
        identity = args["key"]
        payload = self._payload(project_id, identifier, args["lesson"], views, identity)
        return self._store(db, application, project_id, identifier, payload, old, 1, "recorded")

    def revise(self, db, application, sources, project_id, args, *, phase):
        required = "key lesson_id expected_version lesson"
        exact_fields(args, f"{required} dedupe" if "dedupe" in args else required)
        application.string(args["key"], 128)
        application.string(args["lesson_id"], 128)
        application.integer(args["expected_version"], 1)
        validate_lesson(args["lesson"])
        require(len(canonical(args["lesson"]).encode()) <= 16384, "lesson_too_large", 413)
        old = db.execute(
            "SELECT * FROM lessons WHERE id=? AND project_id=?",
            (args["lesson_id"], project_id),
        ).fetchone()
        require(old is not None, "not_found", 404)
        require(old["state"] == "ready", "already_retired", 409)
        require(old["version"] == args["expected_version"], "version_conflict", 409)
        require(
            old["payload"] and lesson_of(json.loads(old["payload"])) == args["key"],
            "identity_conflict",
            409,
        )
        views = self.blocks(sources, db, project_id, args["lesson"]["evidence"], phase=phase)
        if phase == "capture":
            return None
        payload = self._payload(project_id, args["lesson_id"], args["lesson"], views, args["key"])
        return self._store(
            db,
            application,
            project_id,
            args["lesson_id"],
            payload,
            old,
            old["version"] + 1,
            "revised",
        )

    @staticmethod
    def _payload(project_id, identifier, lesson, views, key):
        return {
            **{field: lesson[field] for field in LESSON_FIELDS if field != "evidence"},
            "evidence": [view["reference"] for view in views],
            "project_id": project_id,
            "lesson_id": identifier,
            "key": key,
        }

    def _store(self, db, application, project_id, identifier, payload, old, version, status):
        hashes = []
        for block in payload["evidence"]:
            hashes.append(
                db.execute(
                    "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
                    (block["document_id"], block["version"]),
                ).fetchone()["hash"]
            )
        if old:
            db.execute(
                "UPDATE lessons SET version=?,state='ready',payload=? WHERE id=?",
                (version, canonical(payload), identifier),
            )
        else:
            db.execute(
                "INSERT INTO lessons VALUES (?,?,?,?,?)",
                (identifier, project_id, version, "ready", canonical(payload)),
            )
        db.execute(
            "INSERT INTO lesson_history VALUES (?,?,?)", (identifier, version, canonical(payload))
        )
        db.execute("DELETE FROM lesson_index WHERE lesson_id=?", (identifier,))
        db.execute(
            "INSERT INTO lesson_index VALUES (?,?,?)",
            (identifier, project_id, index_text(payload)),
        )
        application.bump(db, project_id)
        return {
            "status": status,
            "lesson_id": identifier,
            "version": version,
            "hash": lesson_hash(payload, hashes),
            "sharing": "project_only",
            "authority": "explicit_operator_lesson",
        }

    def retire(self, db, application, project_id, args, phase):
        exact_fields(args, "key lesson_id expected_version reason")
        application.string(args["key"], 128)
        application.string(args["lesson_id"], 128)
        application.integer(args["expected_version"], 1)
        text(args["reason"], 1000)
        old = db.execute(
            "SELECT * FROM lessons WHERE id=? AND project_id=?",
            (args["lesson_id"], project_id),
        ).fetchone()
        require(old is not None, "not_found", 404)
        require(old["version"] == args["expected_version"], "version_conflict", 409)
        require(old["state"] != "retired", "already_retired", 409)
        if phase == "capture":
            return None
        db.execute(
            "UPDATE lessons SET version=version+1,state='retired' WHERE id=?", (args["lesson_id"],)
        )
        db.execute("DELETE FROM lesson_index WHERE lesson_id=?", (args["lesson_id"],))
        application.bump(db, project_id)
        return {
            "status": "retired",
            "lesson_id": args["lesson_id"],
            "version": old["version"] + 1,
            "reason": args["reason"],
            "global_effect": "requires_review",
        }

    # -- reads -------------------------------------------------------------------

    @staticmethod
    def view(row, payload, digest):
        return {
            "lesson_id": row["id"],
            "project_id": row["project_id"],
            "version": row["version"],
            "state": row["state"],
            "hash": digest,
            **payload,
        }

    def query(self, db, application, sources, project_id, args, *, phase):
        exact_fields(args, "text budget_bytes")
        application.string(args["text"], 1024)
        application.integer(args["budget_bytes"], 256, 32768)
        result = {
            "project_id": project_id,
            "lessons": [],
            "omissions": [],
            "retrieval": "lexical",
            "trust": "operator_statement_with_source_evidence",
        }
        require(
            len(canonical(dict(result, omissions=["budget", "stale_source"])).encode())
            <= args["budget_bytes"],
            "budget_too_small",
            400,
        )
        search = query_terms(args["text"])
        if not search:
            return result
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search)
        rows = db.execute(
            "SELECT l.id,l.project_id,l.version,l.state,l.payload FROM lesson_index "
            "JOIN lessons l ON l.id=lesson_index.lesson_id "
            "WHERE lesson_index MATCH ? AND lesson_index.project_id=? "
            "AND l.state='ready' ORDER BY rank,l.id LIMIT ?",
            (expression, project_id, MAX_CANDIDATES),
        ).fetchall()
        omitted = set()
        for row in rows:
            payload = json.loads(row["payload"])
            try:
                views = self.blocks(sources, db, project_id, payload["evidence"], phase=phase)
                hashes = [
                    db.execute(
                        "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
                        (view["reference"]["document_id"], view["reference"]["version"]),
                    ).fetchone()["hash"]
                    for view in views
                ]
            except (Fault, TypeError):
                omitted.add("stale_source")
                continue
            lesson = self.view(row, payload, lesson_hash(payload, hashes))
            candidate = dict(
                result,
                lessons=[*result["lessons"], lesson],
                omissions=["budget", "stale_source"],
            )
            if len(canonical(candidate).encode()) > args["budget_bytes"]:
                omitted.add("budget")
                continue
            result["lessons"].append(lesson)
        result["omissions"] = sorted(omitted)
        return result

    def recover(self, db, application, sources, project_id, project, args, seal_key, *, phase):
        exact_fields(args, "text budget_bytes")
        application.string(args["text"], 1024)
        application.integer(args["budget_bytes"], 1024, 32768)
        result = dict(
            self.query(db, application, sources, project_id, args, phase=phase),
            revision=db.execute(
                "SELECT revision FROM knowledge_projects WHERE id=?", (project_id,)
            ).fetchone()[0],
            registration=fingerprint(project),
            goal=None,
            unfinished=[],
            authority="explicit_operator_lesson",
        )
        state = db.execute(
            "SELECT * FROM knowledge_states WHERE project_id=?", (project_id,)
        ).fetchone()
        if state:
            payload = json.loads(state["payload"])
            result["goal"] = payload["goal"]
            result["unfinished"] = payload["unfinished"]
        while len(canonical(result).encode()) + 80 > args["budget_bytes"] and result["lessons"]:
            result["lessons"].pop()
            if "budget" not in result["omissions"]:
                result["omissions"].append("budget")
        result["seal"] = seal(result, seal_key)
        return result

    def check(self, db, application, sources, project_id, project, package, seal_key, *, phase):
        require(
            isinstance(package, dict) and len(canonical(package).encode()) <= 32768,
            "invalid_input",
            400,
        )
        body = {key: value for key, value in package.items() if key != "seal"}
        valid = isinstance(package.get("seal"), str) and hmac.compare_digest(
            package["seal"], seal(body, seal_key)
        )
        reachable = package.get("project_id") == project_id
        valid = (
            valid
            and reachable
            and package.get("registration") == fingerprint(project)
            and package.get("revision")
            == db.execute(
                "SELECT revision FROM knowledge_projects WHERE id=?", (project_id,)
            ).fetchone()[0]
        )
        if valid:
            try:
                for lesson in package.get("lessons", []):
                    self.lesson_view(
                        sources,
                        db,
                        project_id,
                        {
                            "lesson_id": lesson["lesson_id"],
                            "version": lesson["version"],
                            "hash": lesson["hash"],
                        },
                        phase=phase,
                    )
            except (Fault, KeyError, TypeError):
                valid = False
        # A package for another project cannot be tested here; report it without leaking state.
        if not reachable:
            valid = False
        return {"valid": bool(valid), "reason": "current" if valid else "stale_or_tampered"}


def seal(package, seal_key):
    body = {key: value for key, value in package.items() if key != "seal"}
    return hmac.new(seal_key.encode(), canonical(body).encode(), hashlib.sha256).hexdigest()


class ExperienceBook:
    # -- promotion ---------------------------------------------------------------

    @staticmethod
    def _references(db, entry_id):
        return [
            {
                "project_id": row["project_id"],
                "lesson_id": row["lesson_id"],
                "version": row["version"],
                "hash": row["hash"],
                "state": row["state"],
            }
            for row in db.execute(
                "SELECT * FROM experience_lesson_refs WHERE entry_id=? ORDER BY lesson_id",
                (entry_id,),
            )
        ]

    @staticmethod
    def _policy(references):
        return sorted(
            [
                reference["project_id"],
                reference["lesson_id"],
                reference["version"],
                reference["hash"],
            ]
            for reference in references
        )

    @staticmethod
    def approval(entry_id, version, references, seal_key):
        body = [entry_id, version, "explicit_promotion", ExperienceBook._policy(references)]
        return hmac.new(seal_key.encode(), canonical(body).encode(), hashlib.sha256).hexdigest()

    def promote(self, db, application, sources, project_id, args, seal_key, *, phase):
        required = "key expected_version entry"
        exact_fields(args, f"{required} dedupe" if "dedupe" in args else required)
        application.string(args["key"], 128)
        application.integer(args["expected_version"], 0)
        validate_entry(args["entry"])
        require(len(canonical(args["entry"]).encode()) <= 16384, "entry_too_large", 413)
        references = args["entry"]["evidence"]
        # Two different projects must contribute a lesson; repeated logging never qualifies.
        validate_references(references, MINIMUM_EVIDENCE_PROJECTS)
        require(
            len({reference["project_id"] for reference in references}) >= MINIMUM_EVIDENCE_PROJECTS,
            "insufficient_evidence",
            400,
        )
        require(
            len({(reference["project_id"], reference["lesson_id"]) for reference in references})
            >= MINIMUM_EVIDENCE_PROJECTS,
            "insufficient_evidence",
            400,
        )
        expected_version = args["expected_version"]
        identifier = experience_id(project_id, args["key"])
        old = db.execute("SELECT * FROM experience_entries WHERE id=?", (identifier,)).fetchone()
        require((old["version"] if old else 0) == expected_version, "version_conflict", 409)
        if expected_version:
            # The evidence set is authored exactly once: approval may only bless that set.
            stored = [
                {
                    "project_id": reference["project_id"],
                    "lesson_id": reference["lesson_id"],
                    "version": reference["version"],
                    "hash": reference["hash"],
                }
                for reference in self._references(db, identifier)
            ]
            require(stored == references, "evidence_changed", 409)
        for reference in references:
            row, _ = LessonBook.lesson_view(
                sources, db, reference["project_id"], reference, phase=phase
            )
            require(row["state"] == "ready", "stale_evidence", 409)
        if phase == "capture":
            return None
        return self._approve(
            db, application, project_id, identifier, args, references, old, seal_key
        )

    def _approve(self, db, application, project_id, identifier, args, references, old, seal_key):
        entry = args["entry"]
        references = sorted(references, key=lambda reference: reference["lesson_id"])
        version = (old["version"] if old else 0) + 1
        payload = {
            **{field: entry[field] for field in ENTRY_FIELDS if field != "evidence"},
            "evidence": references,
            "project_id": project_id,
            "promoted_by": application.client,
        }
        if old:
            db.execute(
                "UPDATE experience_entries SET version=?,state='active',payload=? WHERE id=?",
                (version, canonical(payload), identifier),
            )
        else:
            db.execute(
                "INSERT INTO experience_entries VALUES (?,?,?,?,?,?)",
                (identifier, version, "active", project_id, canonical(payload), ""),
            )
        db.execute("DELETE FROM experience_lesson_refs WHERE entry_id=?", (identifier,))
        for reference in references:
            db.execute(
                "INSERT INTO experience_lesson_refs VALUES (?,?,?,?,?,?)",
                (
                    identifier,
                    reference["lesson_id"],
                    reference["project_id"],
                    reference["version"],
                    reference["hash"],
                    "ready",
                ),
            )
        record = canonical(
            {
                "approval": self.approval(identifier, version, references, seal_key),
                "entry_id": identifier,
                "version": version,
                "state": "active",
                "payload": payload,
                "evidence": references,
            }
        )
        db.execute("UPDATE experience_entries SET record=? WHERE id=?", (record, identifier))
        db.execute(
            "INSERT INTO experience_history VALUES (?,?,?,?,?,?)",
            (identifier, version, "active", project_id, canonical(payload), record),
        )
        db.execute("DELETE FROM experience_index WHERE entry_id=?", (identifier,))
        db.execute(
            "INSERT INTO experience_index VALUES (?,?,?)",
            (identifier, project_id, entry_text(payload)),
        )
        return {
            "status": "promoted" if not old else "reapproved",
            "entry_id": identifier,
            "version": version,
            "evidence_projects": sorted({reference["project_id"] for reference in references}),
            "sharing": "explicit_operator_approval",
            "reusable": True,
        }

    # -- review ------------------------------------------------------------------

    def withdraw(self, db, application, project_id, args, phase):
        exact_fields(args, "key entry_id expected_version lesson_id reason")
        application.string(args["key"], 128)
        application.string(args["entry_id"], 128)
        application.string(args["lesson_id"], 128)
        application.integer(args["expected_version"], 1)
        text(args["reason"], 1000)
        entry = db.execute(
            "SELECT * FROM experience_entries WHERE id=?", (args["entry_id"],)
        ).fetchone()
        require(entry is not None, "not_found", 404)
        require(entry["version"] == args["expected_version"], "version_conflict", 409)
        # Only the project that recorded the lesson may stop sharing it. Another project's
        # lesson is unresolvable here, not merely refused, so nothing is disclosed about it.
        owned = db.execute(
            "SELECT 1 FROM lessons WHERE id=? AND project_id=?",
            (args["lesson_id"], project_id),
        ).fetchone()
        require(owned is not None, "not_found", 404)
        linked = db.execute(
            "SELECT 1 FROM experience_lesson_refs WHERE entry_id=? AND lesson_id=? "
            "AND project_id=?",
            (args["entry_id"], args["lesson_id"], project_id),
        ).fetchone()
        require(linked is not None, "forbidden", 403)
        if phase == "capture":
            return None
        db.execute(
            "UPDATE experience_lesson_refs SET state='withdrawn' WHERE entry_id=? AND lesson_id=?",
            (args["entry_id"], args["lesson_id"]),
        )
        return {
            "status": "withdrawn",
            "entry_id": args["entry_id"],
            "lesson_id": args["lesson_id"],
            "version": entry["version"],
            "reason": args["reason"],
            "effect": "unavailable",
        }

    def revoke(self, db, application, project_id, args, phase):
        exact_fields(args, "key entry_id expected_version reason")
        application.string(args["key"], 128)
        application.string(args["entry_id"], 128)
        application.integer(args["expected_version"], 1)
        text(args["reason"], 1000)
        entry = db.execute(
            "SELECT * FROM experience_entries WHERE id=?", (args["entry_id"],)
        ).fetchone()
        require(entry is not None, "not_found", 404)
        require(entry["version"] == args["expected_version"], "version_conflict", 409)
        if phase == "capture":
            return None
        version = entry["version"] + 1
        db.execute(
            "UPDATE experience_entries SET version=?,state='revoked' WHERE id=?",
            (version, args["entry_id"]),
        )
        db.execute("DELETE FROM experience_index WHERE entry_id=?", (args["entry_id"],))
        db.execute(
            "INSERT INTO experience_history VALUES (?,?,?,?,?,?)",
            (
                args["entry_id"],
                version,
                "revoked",
                entry["project_id"],
                entry["payload"],
                canonical({"revoked_by": application.client, "reason": args["reason"]}),
            ),
        )
        return {
            "status": "revoked",
            "entry_id": args["entry_id"],
            "version": version,
            "reason": args["reason"],
            "reusable": False,
        }

    # -- reads -------------------------------------------------------------------

    def effect(self, db, application, sources, entry_id, *, phase):
        """Recompute whether a promoted entry is still reusable, in one dispatch phase.

        Nothing is copied into global text and nothing is background-rewritten: the effect is
        derived at read time from the registered project lessons, the **current** document
        versions, tombstones and file contents, and the caller's permission state. Phase 1
        records the expectations of every source file and reads nothing; phase 2 reads those
        files outside any transaction; phase 3 answers from the recorded results, so a source
        changed in between is reported as stale rather than as a live fact.
        """
        references = self._references(db, entry_id)
        if not references:
            return "unavailable"
        if any(reference["state"] == "withdrawn" for reference in references):
            if not {reference["project_id"] for reference in references} <= set(
                application.authorized_projects
            ):
                return "unavailable"
            return "withdrawn"
        if not {reference["project_id"] for reference in references} <= set(
            application.authorized_projects
        ):
            # Never disclose which source project is missing.
            return "unavailable"
        for reference in references:
            stored = db.execute(
                "SELECT * FROM lessons WHERE id=?", (reference["lesson_id"],)
            ).fetchone()
            if (
                stored is None
                or stored["state"] != "ready"
                or stored["version"] != reference["version"]
            ):
                # The lesson this entry was approved for was retired or revised.
                return "superseded"
            try:
                row, _ = LessonBook.lesson_view(
                    sources,
                    db,
                    reference["project_id"],
                    {
                        "lesson_id": reference["lesson_id"],
                        "version": reference["version"],
                        "hash": reference["hash"],
                    },
                    phase=phase,
                )
                require(row["state"] == "ready", "stale_evidence", 409)
            except Fault as error:
                # A replaced or deleted document, a changed file and a moved project revision
                # all stop reuse; none of them may report a live fact.
                if error.code == "project_conflict":
                    return "unavailable"
                return "stale_source"
        return "live"

    def status(self, db, application, sources, entry_id, *, phase):
        row = db.execute("SELECT * FROM experience_entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            return "unavailable", None
        if not {reference["project_id"] for reference in self._references(db, entry_id)} <= set(
            application.authorized_projects
        ):
            # Never disclose which source project is missing.
            return "unavailable", None
        if row["state"] == "revoked":
            return "revoked", row
        return self.effect(db, application, sources, entry_id, phase=phase), row

    def view(self, db, row, effect, scoped_project):
        """Render an entry. Only references from projects the caller asked about are shown."""
        entry = (
            row
            if "payload" in row.keys()
            else db.execute("SELECT * FROM experience_entries WHERE id=?", (row["id"],)).fetchone()
        )
        payload = json.loads(entry["payload"])
        evidence = "protected"
        if scoped_project is not None:
            evidence = [
                reference
                for reference in self._references(db, entry["id"])
                if reference["project_id"] == scoped_project
            ]
        return {
            "entry_id": entry["id"],
            "version": entry["version"],
            "state": "active" if entry["state"] == "active" else "revoked",
            "effect": effect,
            "title": payload["title"],
            "rule": payload["rule"],
            "applicability": payload["applicability"],
            "excludes": payload["excludes"],
            "counterexamples": payload["counterexamples"],
            "recheck_after": payload["recheck_after"],
            "evidence": evidence,
        }

    def query(self, db, application, sources, args, *, phase):
        exact_fields(args, "text budget_bytes project_id")
        text(args["text"], 1024)
        require(
            type(args["budget_bytes"]) is int and 256 <= args["budget_bytes"] <= 32768,
            "invalid_input",
            400,
        )
        if args["project_id"] is not None:
            text(args["project_id"], 128)
            require(args["project_id"] in set(application.authorized_projects), "forbidden", 403)
        result = {
            "entries": [],
            "omissions": [],
            "retrieval": "lexical",
            "trust": "approved_operator_rule_with_protected_citations",
            "sharing": "explicit_operator_approval",
        }
        require(
            len(canonical(dict(result, omissions=["budget"])).encode()) <= args["budget_bytes"],
            "budget_too_small",
            400,
        )
        search = query_terms(args["text"])
        if not search:
            return result
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search)
        # Scope filtering happens in SQL, before ranking, budget accounting and omission
        # bookkeeping: an entry whose sources this caller may not read is never a candidate,
        # so its own keyword hits can neither change the result nor displace a legal candidate.
        rows = db.execute(
            "SELECT e.id,e.version,e.state FROM experience_index "
            "JOIN experience_entries e ON e.id=experience_index.entry_id "
            "WHERE experience_index MATCH ? AND e.state='active' "
            "AND NOT EXISTS(SELECT 1 FROM experience_lesson_refs r WHERE r.entry_id=e.id "
            "AND r.project_id NOT IN (SELECT value FROM json_each(?))) "
            "ORDER BY rank,e.id LIMIT ?",
            (expression, canonical(sorted(application.authorized_projects)), MAX_CANDIDATES),
        ).fetchall()
        omitted = set()
        for row in rows:
            if args["project_id"] is not None and args["project_id"] not in {
                reference["project_id"] for reference in self._references(db, row["id"])
            }:
                continue
            effect = self.effect(db, application, sources, row["id"], phase=phase)
            if effect != "live":
                # The caller may read every source of this entry: reporting why it is no
                # longer reusable is a statement about data it is allowed to see.
                omitted.add(effect)
                continue
            view = self.view(db, row, effect, args["project_id"])
            candidate = dict(
                result,
                entries=[*result["entries"], view],
                omissions=sorted({"budget", *omitted}),
            )
            if len(canonical(candidate).encode()) > args["budget_bytes"]:
                omitted.add("budget")
                continue
            result["entries"].append(view)
        result["omissions"] = sorted(omitted)
        return result

    def check(self, db, application, sources, package, seal_key, entry_id, *, phase):
        require(
            isinstance(package, dict) and len(canonical(package).encode()) <= 32768,
            "invalid_input",
            400,
        )
        body = {key: value for key, value in package.items() if key != "seal"}
        effect, row = self.status(db, application, sources, entry_id, phase=phase)
        references = self._references(db, entry_id) if row is not None else []
        valid = isinstance(package.get("seal"), str) and row is not None
        if valid:
            expected = self.approval(entry_id, package.get("version"), references, seal_key)
            valid = (
                hmac.compare_digest(package["seal"], expected)
                and package.get("version") == row["version"]
                and body.get("effect") == effect
                and effect == "live"
            )
        return {
            "valid": bool(valid),
            "reason": "current" if valid else effect,
            "entry_id": entry_id,
            "version": row["version"] if row is not None else None,
            "effect": effect,
        }


def dispatch(
    application,
    db,
    operation,
    project_id,
    project,
    args,
    seal_key,
    _read,
    *,
    sources,
    phase,
):
    """Entry point used by KnowledgeApplication for every evidence-backed operation.

    `_read` occupies the same position as the project-knowledge dispatcher's reader so one
    call site can route both. Lesson and experience evidence read their files through the
    dispatch's own `sources` object instead, which keeps capture, read and serve distinct.
    """
    experience = ExperienceBook()
    if operation == "experience_query":
        return experience.query(db, application, sources, args, phase=phase)
    if operation == "experience_check":
        return experience.check(
            db,
            application,
            sources,
            args["package"],
            seal_key,
            args["entry_id"],
            phase=phase,
        )
    book = LessonBook()
    if operation == "experience_promote":
        return experience.promote(db, application, sources, project_id, args, seal_key, phase=phase)
    if operation == "experience_revoke":
        return experience.revoke(db, application, project_id, args, phase)
    if operation == "experience_withdraw":
        return experience.withdraw(db, application, project_id, args, phase)
    if operation == "lesson_record":
        return book.record(db, application, sources, project_id, args, phase=phase)
    if operation == "lesson_revise":
        return book.revise(db, application, sources, project_id, args, phase=phase)
    if operation == "lesson_retire":
        return book.retire(db, application, project_id, args, phase)
    if operation == "lesson_query":
        return book.query(db, application, sources, project_id, args, phase=phase)
    if operation == "lesson_recover":
        return book.recover(
            db, application, sources, project_id, project, args, seal_key, phase=phase
        )
    if operation == "lesson_check":
        return book.check(
            db, application, sources, project_id, project, args["package"], seal_key, phase=phase
        )
    raise Fault("invalid_input", 400)
