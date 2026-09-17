"""Versioned research notes and explicit project decisions with source-version evidence.

Reuses the project identity, authorization, Store transaction, source-evidence checks and
byte budget of the project knowledge domain. A note keeps six things separate: the research
question, what each cited source actually argued, the operator's own inferences, what is still
open, and — only when explicitly recorded — a project decision together with the basis for it.
Nothing here calls a model, fetches a URL, watches a client or turns an inference into a
decision by itself.

Every citation binds one registered document version and one complete semantic unit by its
content hash; a title is never proof that anything was read. A note version is immutable: a
revision appends a new version and every earlier version, with the citation state it had, stays
readable.
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

# The scalar validators are shared by every domain and live in a neutral module, so a note
# argument is rejected by exactly the same rule as any other knowledge argument, without this
# module importing the application that routes the dispatch.
from .validate import integer, string

BLOCK_FIELDS = "block_id document_id version hash"
NOTE_REFERENCE_FIELDS = "note_id version hash"
STATEMENT_FIELDS = "kind statement source"
DECISION_FIELDS = "summary basis"
NOTE_REQUIRED = "question source_statements inferences open_questions"
NOTE_OPTIONAL = ("decision",)
MAX_CANDIDATES = 128
# A citation chain is walked at most this many levels while a note is recorded or revised.
# A chain that has not ended within the bound is refused instead of being walked further, so a
# cycle can never hide behind the bound by making the walk give up.
MAX_CITATION_DEPTH = 64
# Total node visits one dependency walk may spend. A note graph is a DAG whose shared ancestors
# are revisited along each path, so a pathological graph could otherwise cost exponential time;
# spending the budget is refused rather than silently answering from a partial walk.
MAX_GRAPH_WORK = 4096
# State of one citation, derived at read time from the current state of what it names.
CITATION_LIVE = "live"
CITATION_EXPIRED = "expired"
CITATION_NOTE_UNAVAILABLE = "note_unavailable"


def text(value, maximum=2000):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)
    return value


def listing(value, maximum=2000, count=16):
    require(isinstance(value, list) and len(value) <= count, "invalid_input", 400)
    for item in value:
        text(item, maximum)
    return value


def exact_fields(value, fields, code="invalid_input"):
    require(isinstance(value, dict) and set(value) == set(fields.split()), code, 400)


def optional_fields(value, required, optional, code="invalid_input"):
    """Validate a mapping whose optional keys may be absent but never unknown."""
    require(isinstance(value, dict), code, 400)
    require(
        set(value) == set(required.split()) | {key for key in optional if key in value}, code, 400
    )


def note_id(project_id, key):
    return "note:" + fingerprint([project_id, key])


def note_of(payload):
    """The stable identity key a stored note version was recorded under."""
    return payload.get("key")


def validate_block(reference, code="evidence_required"):
    exact_fields(reference, BLOCK_FIELDS, code)
    text(reference["block_id"], 2048)
    text(reference["document_id"], 2048)
    require(type(reference["version"]) is int and reference["version"] >= 1, "invalid_input", 400)
    text(reference["hash"], 64)


def validate_note_reference(reference, code="evidence_required"):
    exact_fields(reference, NOTE_REFERENCE_FIELDS, code)
    text(reference["note_id"], 128)
    require(type(reference["version"]) is int and reference["version"] >= 1, "invalid_input", 400)
    text(reference["hash"], 64)


def validate_statements(statements):
    """Every source statement is a complete unit citation plus the operator's own summary."""
    require(isinstance(statements, list) and 0 < len(statements) <= 16, "evidence_required", 400)
    for statement in statements:
        exact_fields(statement, STATEMENT_FIELDS, "evidence_required")
        text(statement["kind"], 64)
        text(statement["statement"], 2000)
        validate_block(statement["source"])


def validate_basis(basis):
    """A decision basis is an explicit list of source units or recorded note versions."""
    require(isinstance(basis, list) and 0 < len(basis) <= 16, "evidence_required", 400)
    for entry in basis:
        exact_fields(entry, "kind reference", "evidence_required")
        text(entry["kind"], 64)
        if entry["kind"] == "source":
            validate_block(entry["reference"])
        else:
            require(entry["kind"] == "note", "invalid_input", 400)
            validate_note_reference(entry["reference"])


def validate_note(note):
    optional_fields(note, NOTE_REQUIRED, NOTE_OPTIONAL)
    text(note["question"], 2000)
    validate_statements(note["source_statements"])
    listing(note["inferences"], 2000)
    listing(note["open_questions"], 2000)
    if "decision" in note:
        exact_fields(note["decision"], DECISION_FIELDS)
        text(note["decision"]["summary"], 2000)
        validate_basis(note["decision"]["basis"])


def note_text(payload):
    """The searchable text of one note version: every field, still clearly labelled."""
    parts = [payload["question"]]
    for statement in payload["source_statements"]:
        parts.extend([statement["kind"], statement["statement"]])
    parts.extend(payload["inferences"])
    parts.extend(payload["open_questions"])
    if payload.get("decision"):
        parts.append(payload["decision"]["summary"])
    return " ".join(terms(" ".join(parts)))


def note_hash(payload, source_hashes, note_hashes):
    """Content fingerprint of one note version, bound to its citations' content hashes.

    Re-importing identical bytes keeps the fingerprint; editing or deleting a cited source, or
    revising a cited note, changes it. That is what a read compares before it reports a stored
    conclusion as current, and what a citing note binds when it cites this version.
    """
    return fingerprint([payload, list(source_hashes), list(note_hashes)])


def citation_hash(payload, citations):
    """The recorded fingerprint of one note version, rebuilt from its own citation rows.

    The order is the order the citations were recorded in: the source statements first, then
    the decision basis. It is the single definition of a note version's content hash, used both
    when the version is written and when a later read or a citing note re-checks it.
    """
    sources, notes = [], []
    for row in citations:
        if row["kind"] == "source":
            sources.append(row["hash"])
        else:
            notes.append(row["hash"])
    return note_hash(payload, sources, notes)


def snapshot(db, project_id, project, client, secret, operation):
    """Knowledge/research-note schema gate plus the per-dispatch seal key."""
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    require(
        metadata.get("knowledge_schema") == "1" and bool(metadata.get("knowledge_seal_key")),
        "dependency_unavailable",
        503,
    )
    require(metadata.get("research_notes_schema") == "1", "dependency_unavailable", 503)
    seal_key = hmac.new(
        metadata["knowledge_seal_key"].encode(),
        canonical([client, secret]).encode(),
        hashlib.sha256,
    ).hexdigest()
    registered = db.execute("SELECT * FROM knowledge_projects WHERE id=?", (project_id,)).fetchone()
    if registered:
        require(registered["registration"] == canonical(project), "registration_changed", 409)
    else:
        # Only the operations that can create the project row may run before it exists. A read
        # of a project that never recorded anything is refused as uninitialized, not as missing.
        require(operation == "note_record", "project_uninitialized", 409)
    return (registered["revision"] if registered else None), seal_key


def seal(package, seal_key):
    body = {key: value for key, value in package.items() if key != "seal"}
    return hmac.new(seal_key.encode(), canonical(body).encode(), hashlib.sha256).hexdigest()


class NoteActor:
    """The frozen identity of one dispatch, resolved before any phase runs.

    A note capability needs to know who is acting, but it does not need the application that
    authorized the request. The credential stays with the authorization code: it reaches this
    module only through the already-derived package seal key, never as a value to re-check.
    The identity is immutable — a second assignment raises — so no rule can quietly rewrite who
    is acting halfway through a dispatch.
    """

    __slots__ = ("_client",)

    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __setattr__(self, name, value):
        raise AttributeError("NoteActor is immutable")

    def __delattr__(self, name):
        raise AttributeError("NoteActor is immutable")

    @property
    def client(self):
        return self._client

    def __repr__(self):
        return f"NoteActor(client={self._client!r})"


class ProjectPort:
    """What the note domain is allowed to ask of the project domain, and nothing more.

    This is a contract, not an implementation: the object the application injects is built by
    whichever domain owns the project tables, so the note module never writes SQL against
    `knowledge_projects` or `knowledge_states` and never imports the routing module to reach
    them. The note domain calls these three methods and no others.
    """

    def revision(self):
        """The project version, or `project_uninitialized` when the project row is absent."""
        raise NotImplementedError

    def declared_state(self):
        """The goal and unfinished items the project itself declared, or `(None, [])`."""
        raise NotImplementedError

    def bump(self):
        """Advance the project version, so a cached continuation package stops being current."""
        raise NotImplementedError


class ResearchNotes:
    """Record, revise, withdraw, query and inspect versioned research notes."""

    def __init__(self, db, project_id, *, phase, actor, project_port, sources):
        self.db = db
        self.project_id = project_id
        self.phase = phase
        self.actor = actor
        self.projects = project_port
        self.sources = sources
        # One dispatch reads each stored citation list at most once. The caches are dropped with
        # the object, so a later dispatch can never answer from an earlier phase.
        self._rows = {}
        self._source_cache = {}

    # -- storage helpers ---------------------------------------------------------

    def _context(self):
        """The lesson evidence context of this project in this phase, reused unchanged."""
        if self._source_cache.get("context") is None:
            self._source_cache["context"] = self.sources.context(
                self.db, self.project_id, self.phase
            )
        return self._source_cache["context"]

    def _note_row(self, identifier):
        return self.db.execute(
            "SELECT * FROM research_notes WHERE id=? AND project_id=?",
            (identifier, self.project_id),
        ).fetchone()

    def _citations(self, identifier, version):
        """Every stored citation of one note version, in the order it was recorded."""
        key = (identifier, version)
        if key not in self._rows:
            self._rows[key] = self.db.execute(
                "SELECT * FROM research_note_citations WHERE note_id=? AND version=? "
                "ORDER BY ordinal",
                (identifier, version),
            ).fetchall()
        return self._rows[key]

    def _stored(self, identifier, version):
        """One stored note version: its row, its payload and its recorded fingerprint."""
        row = self.db.execute(
            "SELECT * FROM research_notes WHERE id=? AND project_id=?",
            (identifier, self.project_id),
        ).fetchone()
        require(row is not None, "not_found", 404)
        stored = self.db.execute(
            "SELECT payload FROM research_note_history WHERE note_id=? AND version=?",
            (identifier, version),
        ).fetchone()
        require(stored is not None, "not_found", 404)
        payload = json.loads(stored["payload"])
        return row, payload, citation_hash(payload, self._citations(identifier, version))

    # -- citation graph ----------------------------------------------------------

    def _walk(self, roots):
        """Every cited note version reachable from `roots`, keyed by identity.

        A citation binds one **recorded** note version, and that is the version this walk
        follows: a note that has since been revised does not become the evidence a historical
        citation named, so the current version is never substituted for a recorded one.

        **Every edge is verified before anything is deduplicated.** Two edges may name the same
        note at different versions — one current, one superseded — and deduplicating by identity
        first would let the second edge ride on the first edge's check. So each edge is checked on
        its own recorded `(note_id, version, hash)`: the cited version must still be a current,
        unretracted version whose recorded fingerprint matches the one the citing note bound. Only
        after that does a note already verified on another branch stop the walk from expanding its
        descendants again, and the map this returns holds exactly the verified versions.

        A cycle is a back edge: a note version reached again **while it is still on the path
        being walked**. A diamond is not a cycle — two studies that both rest on the same
        foundational note may legitimately be combined — so a version already finished on another
        branch is deduplicated rather than refused. Two bounds keep the walk honest: a root counts
        as depth 1 and the path is capped at `MAX_CITATION_DEPTH`, and the total edge visits at
        `MAX_GRAPH_WORK`. Exceeding either is refused rather than answered from a partial walk, so
        a cycle can never hide behind a bound by making the walk give up. A cited note of another
        project is unresolvable here instead of being followed.
        """
        found = {}
        work = [0]

        def descend(reference, path, depth):
            identifier = reference["note_id"]
            require(identifier not in path, "citation_cycle", 400)
            require(depth <= MAX_CITATION_DEPTH, "citation_cycle", 400)
            work[0] += 1
            require(work[0] <= MAX_GRAPH_WORK, "citation_cycle", 400)
            # This edge is checked here, on its own recorded version, before the identity is
            # deduplicated: a superseded, retracted or rewritten version is refused no matter
            # which other version of the same note was already reached.
            self._note_evidence(reference)
            if identifier in found:
                return
            found[identifier] = reference
            for child in self._cited_notes(identifier, reference["version"]):
                descend(child, path | {identifier}, depth + 1)

        for root in roots:
            descend(root, frozenset(), 1)
        return found

    @staticmethod
    def _note_references(note):
        """The note versions one candidate note cites, as recorded references."""
        return [
            entry["reference"]
            for entry in note.get("decision", {}).get("basis", [])
            if entry["kind"] == "note"
        ]

    def _cited_notes(self, identifier, version):
        """The note references one stored version cites, as they were recorded.

        Each reference keeps the version and fingerprint that version had when the citing note
        was written, so a later revision of the cited note cannot be walked in its place.
        """
        return [
            {
                "note_id": citation["cited_note_id"],
                "version": citation["cited_note_version"],
                "hash": citation["hash"],
            }
            for citation in self._citations(identifier, version)
            if citation["kind"] == "note"
        ]

    def _cited_sources(self, identifier, version):
        """The source units one stored version cites, as evidence references."""
        return [
            self._citation_reference(citation)
            for citation in self._citations(identifier, version)
            if citation["kind"] == "source"
        ]

    # -- write validation --------------------------------------------------------

    def _refuse_cycles(self, identifier, note):
        """Refuse a citation chain that would make this note part of its own evidence."""
        roots = self._note_references(note)
        require(identifier not in [root["note_id"] for root in roots], "citation_cycle", 400)
        require(identifier not in self._walk(roots), "citation_cycle", 400)

    def _note_evidence(self, reference):
        """One cited note version as evidence, or `stale_evidence` when it is not usable.

        A conclusion can only be cited while it is still a current, unretracted version whose
        recorded fingerprint matches the one this citation binds. Resolving it here, before any
        write, is what stops a withdrawn or superseded conclusion from being adopted after the
        fact.
        """
        row, _, digest = self._stored(reference["note_id"], reference["version"])
        require(row["state"] == "ready", "stale_evidence", 409)
        require(row["version"] == reference["version"], "stale_evidence", 409)
        require(digest == reference["hash"], "stale_evidence", 409)
        return row

    def _dependencies(self, note):
        """Every source unit this note depends on, including through the notes it cites.

        A cited note is evidence only while the whole chain under it is current: a conclusion
        that rests on a source which has since changed is no longer that conclusion. The walk
        therefore follows every **recorded** note reference, verifying each edge's version,
        fingerprint and state, and collects the source units of exactly the versions it verified
        — never the units of a version the citing note did not name. Every collected unit is then
        verified exactly like a unit the new note cites directly. The walk is the same bounded,
        cycle-refusing walk the write path uses, so a stored ring is refused instead of being
        followed.
        """
        units = [statement["source"] for statement in note["source_statements"]] + [
            entry["reference"]
            for entry in note.get("decision", {}).get("basis", [])
            if entry["kind"] != "note"
        ]
        for identifier, reference in sorted(self._walk(self._note_references(note)).items()):
            units.extend(self._cited_sources(identifier, reference["version"]))
        return units

    def _check_citations(self, note):
        """Verify every source unit and every cited note version in this phase.

        A source unit goes through the project's existing evidence check, so a citation can only
        bind a registered document version whose bytes are still the ones that were read. A note
        citation is resolved inside this project and must be a current `ready` version whose
        recorded fingerprint still matches, **and every recorded note reference under it must
        still be current at the version it named**, with every source unit under that version
        still readable: the transitive dependencies are checked in the same phase as the direct
        ones, so a superseded or expired chain is refused before the new decision is written
        rather than being labelled unavailable after it committed.
        """
        context = self._context()
        units = self._dependencies(note)
        if self.phase == "capture":
            # Capture runs inside the transaction and must not touch the filesystem: it records
            # what has to be read, and the serving phase decides from the result.
            for unit in units:
                context.observe(unit)
        else:
            for unit in units:
                context.blocks(unit)
        require(context.revision_unchanged(), "project_conflict", 409)

    # -- writes ------------------------------------------------------------------

    def record(self, args):
        required = "key expected_version note"
        exact_fields(args, f"{required} dedupe" if "dedupe" in args else required)
        string(args["key"], 128)
        integer(args["expected_version"], 0)
        validate_note(args["note"])
        require(len(canonical(args["note"]).encode()) <= 16384, "note_too_large", 413)
        identifier = note_id(self.project_id, args["key"])
        old = self._note_row(identifier)
        # The version is checked first, exactly as the project's own import does, so a caller
        # that declares a stale version is told about the conflict even when its operation key is
        # fresh. A note identity is created once and can only grow through `note_revise`.
        require(args["expected_version"] == (old["version"] if old else 0), "version_conflict", 409)
        require(old is None, "already_recorded", 409)
        self._refuse_cycles(identifier, args["note"])
        self._check_citations(args["note"])
        if self.phase == "capture":
            return None
        payload = self._payload(identifier, args["key"], args["note"])
        return self._store(identifier, payload, None, 1, "recorded")

    def revise(self, args):
        required = "key note_id expected_version note"
        exact_fields(args, f"{required} dedupe" if "dedupe" in args else required)
        string(args["key"], 128)
        string(args["note_id"], 128)
        integer(args["expected_version"], 1)
        validate_note(args["note"])
        require(len(canonical(args["note"]).encode()) <= 16384, "note_too_large", 413)
        old = self._note_row(args["note_id"])
        require(old is not None, "not_found", 404)
        require(old["state"] == "ready", "already_withdrawn", 409)
        require(old["version"] == args["expected_version"], "version_conflict", 409)
        # A note belongs to the identity that recorded it: another identity of the same project
        # may read it, and may not rewrite it or its citations.
        require(old["owner"] == self.actor.client, "forbidden", 403)
        require(
            old["payload"] and note_of(json.loads(old["payload"])) == args["key"],
            "identity_conflict",
            409,
        )
        self._refuse_cycles(args["note_id"], args["note"])
        self._check_citations(args["note"])
        if self.phase == "capture":
            return None
        payload = self._payload(args["note_id"], args["key"], args["note"])
        return self._store(args["note_id"], payload, old, old["version"] + 1, "revised")

    def _payload(self, identifier, key, note):
        payload = {
            **{field: note[field] for field in NOTE_REQUIRED.split()},
            "project_id": self.project_id,
            "note_id": identifier,
            "key": key,
        }
        if "decision" in note:
            payload["decision"] = note["decision"]
        return payload

    def _store(self, identifier, payload, old, version, status):
        """Write one note version with its immutable citation rows.

        Each row records the identity of exactly what the operator cited: a source unit as its
        document version and content hash, a cited note as its version and that version's own
        recorded fingerprint. The version's own hash is then rebuilt from those same rows, so a
        later read compares against the citations that were actually stored.
        """
        rows = []
        for statement in payload["source_statements"]:
            rows.append(("source", statement["source"]))
        for entry in payload.get("decision", {}).get("basis", []):
            rows.append((entry["kind"], entry["reference"]))
        for ordinal, (kind, reference) in enumerate(rows):
            if kind == "source":
                self._citation(identifier, version, ordinal, "source", reference)
            else:
                _, _, digest = self._stored(reference["note_id"], reference["version"])
                self._citation(identifier, version, ordinal, "note", reference, digest)
        citations = self._citations(identifier, version)
        require(len(citations) == len(rows), "invalid_input", 400)
        if old:
            self.db.execute(
                "UPDATE research_notes SET version=?,state='ready',payload=? WHERE id=?",
                (version, canonical(payload), identifier),
            )
        else:
            self.db.execute(
                "INSERT INTO research_notes VALUES (?,?,?,?,?,?,?)",
                (
                    identifier,
                    self.project_id,
                    payload["key"],
                    self.actor.client,
                    version,
                    "ready",
                    canonical(payload),
                ),
            )
        self.db.execute(
            "INSERT INTO research_note_history VALUES (?,?,?)",
            (identifier, version, canonical(payload)),
        )
        self.db.execute("DELETE FROM research_note_index WHERE note_id=?", (identifier,))
        self.db.execute(
            "INSERT INTO research_note_index VALUES (?,?,?)",
            (identifier, self.project_id, note_text(payload)),
        )
        self.projects.bump()
        return {
            "status": status,
            "note_id": identifier,
            "version": version,
            "hash": citation_hash(payload, citations),
            "citations": len(citations),
            "source_units": len([row for row in citations if row["kind"] == "source"]),
            "cited_notes": len([row for row in citations if row["kind"] == "note"]),
            "sharing": "project_only",
            "authority": "explicit_operator_research_note",
        }

    def _citation(self, identifier, version, ordinal, kind, reference, digest=None):
        """Append one immutable citation row. A source row keeps the unit's own hash."""
        if kind == "source":
            self.db.execute(
                "INSERT INTO research_note_citations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    version,
                    ordinal,
                    "source",
                    reference["block_id"],
                    reference["document_id"],
                    reference["version"],
                    reference["hash"],
                    None,
                    None,
                ),
            )
            return
        self.db.execute(
            "INSERT INTO research_note_citations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                version,
                ordinal,
                "note",
                None,
                None,
                None,
                digest,
                reference["note_id"],
                reference["version"],
            ),
        )

    def withdraw(self, args):
        """Retract a note: a new version records the retraction and the reason.

        Every earlier version stays in history with the citation state it had, and the note
        leaves the query index, so a withdrawn conclusion is never returned as current.
        """
        exact_fields(
            args,
            "key note_id expected_version reason dedupe"
            if "dedupe" in args
            else "key note_id expected_version reason",
        )
        string(args["key"], 128)
        string(args["note_id"], 128)
        integer(args["expected_version"], 1)
        text(args["reason"], 1000)
        old = self._note_row(args["note_id"])
        require(old is not None, "not_found", 404)
        require(old["version"] == args["expected_version"], "version_conflict", 409)
        require(old["state"] != "withdrawn", "already_withdrawn", 409)
        require(old["owner"] == self.actor.client, "forbidden", 403)
        if self.phase == "capture":
            return None
        payload = json.loads(old["payload"])
        stored = {
            **payload,
            "withdrawn": {"version": old["version"], "reason": args["reason"]},
        }
        self.db.execute(
            "UPDATE research_notes SET version=?,state='withdrawn',payload=? WHERE id=?",
            (old["version"] + 1, canonical(stored), args["note_id"]),
        )
        self.db.execute(
            "INSERT INTO research_note_history VALUES (?,?,?)",
            (args["note_id"], old["version"] + 1, canonical(stored)),
        )
        self.db.execute("DELETE FROM research_note_index WHERE note_id=?", (args["note_id"],))
        self.projects.bump()
        return {
            "status": "withdrawn",
            "note_id": args["note_id"],
            "version": old["version"] + 1,
            "reason": args["reason"],
            "effect": "unavailable",
        }

    # -- reads -------------------------------------------------------------------

    def _source_status(self, row):
        """Whether one stored source citation can still be shown as current evidence."""
        try:
            self._context().blocks(
                {
                    "block_id": row["block_id"],
                    "document_id": row["document_id"],
                    "version": row["document_version"],
                    "hash": row["hash"],
                }
            )
        except Fault:
            return CITATION_EXPIRED
        return CITATION_LIVE

    def _note_status(self, row, source_cache, note_cache):
        """Whether one stored note citation is still a current, unretracted version.

        This only ever answers with a state. A cited note that was retracted, revised away or
        whose own citations expired ends as a state; none of its text is returned here, so a
        reader that cannot re-check a citation also cannot read it back through this note.
        """
        key = (row["cited_note_id"], row["cited_note_version"])
        if key in note_cache:
            return note_cache[key]
        # A placeholder stops a malformed stored ring from recursing without bound; the write
        # path refuses cycles, so this is a fail-closed guard rather than a normal outcome.
        note_cache[key] = CITATION_NOTE_UNAVAILABLE
        note_cache[key] = self._resolve_note_status(row, source_cache, note_cache)
        return note_cache[key]

    def _resolve_note_status(self, row, source_cache, note_cache):
        stored = self.db.execute(
            "SELECT * FROM research_notes WHERE id=? AND project_id=?",
            (row["cited_note_id"], self.project_id),
        ).fetchone()
        if (
            stored is None
            or stored["state"] != "ready"
            or stored["version"] != row["cited_note_version"]
        ):
            return CITATION_NOTE_UNAVAILABLE
        history = self.db.execute(
            "SELECT payload FROM research_note_history WHERE note_id=? AND version=?",
            (row["cited_note_id"], row["cited_note_version"]),
        ).fetchone()
        if history is None:
            return CITATION_NOTE_UNAVAILABLE
        payload = json.loads(history["payload"])
        citations = self._citations(row["cited_note_id"], row["cited_note_version"])
        if citation_hash(payload, citations) != row["hash"]:
            return CITATION_NOTE_UNAVAILABLE
        for citation in citations:
            if self._citation_status(citation, source_cache, note_cache) != CITATION_LIVE:
                return CITATION_NOTE_UNAVAILABLE
        return CITATION_LIVE

    def _citation_status(self, row, source_cache, note_cache):
        if row["kind"] == "note":
            return self._note_status(row, source_cache, note_cache)
        key = (row["block_id"], row["document_id"], row["document_version"], row["hash"])
        if key not in source_cache:
            source_cache[key] = self._source_status(row)
        return source_cache[key]

    @staticmethod
    def _citation_reference(citation):
        if citation["kind"] == "source":
            return {
                "block_id": citation["block_id"],
                "document_id": citation["document_id"],
                "version": citation["document_version"],
                "hash": citation["hash"],
            }
        return {
            "note_id": citation["cited_note_id"],
            "version": citation["cited_note_version"],
            "hash": citation["hash"],
        }

    def _view(self, payload, version, source_cache=None, note_cache=None):
        """One note version with the current state of every citation it carries.

        A citation is never dropped and never trimmed: each one is reported with the state it
        has now, so an expired or unavailable citation is visible as such instead of the note
        quietly losing the evidence it was recorded with.
        """
        source_cache = {} if source_cache is None else source_cache
        note_cache = {} if note_cache is None else note_cache
        citations = [
            {
                "ordinal": citation["ordinal"],
                "kind": citation["kind"],
                "reference": self._citation_reference(citation),
                "status": self._citation_status(citation, source_cache, note_cache),
            }
            for citation in self._citations(payload["note_id"], version)
        ]
        expired = sorted({item["status"] for item in citations if item["status"] != CITATION_LIVE})
        return {
            "note_id": payload["note_id"],
            "project_id": payload["project_id"],
            "version": version,
            "state": "withdrawn" if payload.get("withdrawn") else "ready",
            "hash": citation_hash(payload, self._citations(payload["note_id"], version)),
            "current": not expired,
            "citation_states": expired,
            "citations": citations,
            "question": payload["question"],
            "source_statements": payload["source_statements"],
            "inferences": payload["inferences"],
            "open_questions": payload["open_questions"],
            "decision": payload.get("decision"),
            "withdrawn": payload.get("withdrawn"),
        }

    def _ready_notes(self, text_value):
        """Rank this project's candidate notes, recording the state of every cited file.

        A citation can only be answered from a phase that has already established the state of
        the file it names, so the candidate notes are checked through the project's own evidence
        port before they are ranked. That call is what makes the capture phase record the
        expectation for each cited file, and what makes the serving phase compare against the
        result read outside the transaction instead of assuming the file never moved.
        """
        search = query_terms(text_value)
        if not search:
            return []
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search)
        rows = self.db.execute(
            "SELECT n.id,n.version,n.payload FROM research_note_index "
            "JOIN research_notes n ON n.id=research_note_index.note_id "
            "WHERE research_note_index MATCH ? AND research_note_index.project_id=? "
            "AND n.state='ready' AND n.project_id=? ORDER BY rank,n.id LIMIT ?",
            (expression, self.project_id, self.project_id, MAX_CANDIDATES),
        ).fetchall()
        self._observe_citations([(row["id"], row["version"]) for row in rows])
        return rows

    def _observe_citations(self, versions):
        """Record, in the capture phase, the state of every file these notes cite.

        A citation can only be answered from a phase that has already established the state of
        the file it names, so this runs while the transaction is open and performs no I/O of its
        own: the project's evidence port records the expectation for each cited file, and the
        serving phase later compares that expectation with the result read outside the
        transaction. The walk follows cited notes as well, because a note's citation is only
        current while that note's own citations are. Later phases do nothing here: they answer
        from the results the earlier ones recorded.
        """
        if self.phase != "capture":
            return
        context = self._context()
        pending, seen = list(versions), set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            for citation in self._citations(*current):
                if citation["kind"] == "source":
                    # A read describes an expired citation; it does not fail because of one. The
                    # observation still records what this phase saw, so the serving phase can
                    # answer from the recorded result rather than calling everything expired.
                    context.observe(self._citation_reference(citation))
                    continue
                nested = (citation["cited_note_id"], citation["cited_note_version"])
                row = self.db.execute(
                    "SELECT state,version FROM research_notes WHERE id=? AND project_id=?",
                    (citation["cited_note_id"], self.project_id),
                ).fetchone()
                # A citation that cannot be resolved, or that is no longer ready, is not walked:
                # it is reported as an unavailable citation later, from this same stored state.
                if row is not None and row["state"] == "ready" and row["version"] == nested[1]:
                    pending.append(nested)
        require(context.revision_unchanged(), "project_conflict", 409)

    def query(self, args):
        """Search this project's current notes; a note and all its citations are one unit."""
        exact_fields(args, "text budget_bytes")
        string(args["text"], 1024)
        integer(args["budget_bytes"], 256, 32768)
        result = {
            "project_id": self.project_id,
            "notes": [],
            "omissions": [],
            "retrieval": "lexical",
            "trust": "operator_research_note_with_source_versions",
            # Notes never restate the checkout: a continuation package's working-directory
            # facts stay authoritative over anything recorded here.
            "project_workdir": "authoritative_over_notes",
        }
        # An empty answer still names what would be omitted, so a caller can size its budget.
        require(
            len(canonical(dict(result, omissions=["budget"])).encode()) <= args["budget_bytes"],
            "budget_too_small",
            400,
        )
        # The candidate set is resolved in every phase. The capture phase needs it to record
        # which files its citations name, and the serving phase needs it to answer: an operation
        # that skipped the capture step would have no recorded expectation to compare against and
        # would have to call every citation expired.
        rows = self._ready_notes(args["text"])
        if self.phase == "capture":
            return result
        source_cache, note_cache, omissions = {}, {}, set()
        for row in rows:
            payload = json.loads(row["payload"])
            view = self._view(payload, row["version"], source_cache, note_cache)
            candidate = dict(
                result, notes=[*result["notes"], view], omissions=sorted({"budget", *omissions})
            )
            if len(canonical(candidate).encode()) > args["budget_bytes"]:
                # The note is left out whole rather than returned without part of its evidence.
                omissions.add("budget")
                continue
            result["notes"].append(view)
            omissions |= set(view["citation_states"])
        result["omissions"] = sorted(omissions)
        return result

    def recover(self, args, project, seal_key):
        """A short, sealed package of the current state and this project's current notes."""
        exact_fields(args, "text budget_bytes")
        string(args["text"], 1024)
        integer(args["budget_bytes"], 1024, 32768)
        goal, unfinished = self.projects.declared_state()
        result = dict(
            self.query(args),
            revision=self.projects.revision(),
            registration=fingerprint(project),
            goal=goal,
            unfinished=unfinished,
            authority="explicit_operator_research_note",
        )
        # Notes are dropped whole from the end until the package fits; the state section is never
        # split and no note is ever returned with part of its citation list missing.
        while len(canonical(result).encode()) + 80 > args["budget_bytes"] and result["notes"]:
            result["notes"].pop()
            if "budget" not in result["omissions"]:
                result["omissions"].append("budget")
        result["seal"] = seal(result, seal_key)
        return result

    def status(self, args):
        """Read one stored note version, current or historical, with its citation state."""
        exact_fields(args, "note_id version")
        string(args["note_id"], 128)
        integer(args["version"], 1)
        row, payload, _ = self._stored(args["note_id"], args["version"])
        self._observe_citations([(args["note_id"], args["version"])])
        view = self._view(payload, args["version"])
        view["current_version"] = row["version"]
        return view

    def check(self, package, seal_key):
        """Validate an issued note package against the current sources and project revision."""
        require(
            isinstance(package, dict) and len(canonical(package).encode()) <= 32768,
            "invalid_input",
            400,
        )
        body = {key: value for key, value in package.items() if key != "seal"}
        valid = isinstance(package.get("seal"), str) and hmac.compare_digest(
            package["seal"], seal(body, seal_key)
        )
        valid = (
            valid
            and package.get("project_id") == self.project_id
            and package.get("revision") == self.projects.revision()
        )
        if not valid:
            return {"valid": False, "reason": "stale_or_tampered"}
        source_cache, note_cache = {}, {}
        for note in package.get("notes", []):
            try:
                row = self._note_row(note["note_id"])
                require(row is not None and row["version"] == note["version"], "stale", 409)
                require(
                    self._view(
                        json.loads(
                            self.db.execute(
                                "SELECT payload FROM research_note_history "
                                "WHERE note_id=? AND version=?",
                                (note["note_id"], note["version"]),
                            ).fetchone()["payload"]
                        ),
                        note["version"],
                        source_cache,
                        note_cache,
                    )["hash"]
                    == note["hash"],
                    "stale",
                    409,
                )
                for citation in self._citations(note["note_id"], note["version"]):
                    require(
                        self._citation_status(citation, source_cache, note_cache) == CITATION_LIVE,
                        "stale",
                        409,
                    )
            except (Fault, KeyError, TypeError):
                return {"valid": False, "reason": "stale_or_tampered"}
        return {"valid": True, "reason": "current"}


def dispatch(
    db, operation, project_id, project, args, seal_key, sources, phase, *, actor, project_port
):
    """Entry point used by KnowledgeApplication for every research-note operation.

    The caller resolves the identity and the project port before any phase runs; this module
    never reaches back into the application that authorized the request.
    """
    book = ResearchNotes(
        db, project_id, phase=phase, actor=actor, project_port=project_port, sources=sources
    )
    if operation == "note_record":
        return book.record(args)
    if operation == "note_revise":
        return book.revise(args)
    if operation == "note_withdraw":
        return book.withdraw(args)
    if operation == "note_query":
        return book.query(args)
    if operation == "note_recover":
        return book.recover(args, project, seal_key)
    if operation == "note_status":
        return book.status(args)
    if operation == "note_check":
        return book.check(args["package"], seal_key)
    raise Fault("invalid_input", 400)
