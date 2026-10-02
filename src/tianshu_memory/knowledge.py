"""Trusted project-domain application. Never calls chat receipt/profile workflows."""

import hashlib
import hmac
import http.client
import json
import re
from pathlib import Path

from . import knowledge_continuation as continuation
from . import knowledge_directories as directories
from . import knowledge_workdir as workdir
from . import validate as validation
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
from .knowledge_evidence import blocks
from .knowledge_ports import ContinuationReader, DirectoryWriter, EvidenceLookup
from .knowledge_sources import FileReader, content_hash, decode, fetch_url, read_file
from .store import Store

READ = {
    "query",
    "recover",
    "check",
    "status",
    "directory_scan",
    "continuation_recover",
    "continuation_check",
    "note_query",
    "note_recover",
    "note_status",
    "note_check",
    # The catalogue and the per-document reader are two operations of their own, each with its own
    # permission name. `query` never implies either: a client that may search by keyword is not
    # thereby allowed to enumerate a project's documents or to walk one document's blocks.
    "document_list",
    "document_read",
}
WRITE = {"import", "delete", "write_state", "directory_apply"}
# Continuation reads a registered working directory instead of a registered scan directory.
# A state write only reads one when the caller declares a commit-bound verification, so the
# section is validated for these operations and never guessed from the project root.
CONTINUATION_OPERATIONS = {"continuation_recover", "continuation_check"}
WORKTREE_OPERATIONS = CONTINUATION_OPERATIONS | {"write_state"}
# A single state write may bind verifications to at most this many checkouts.
MAX_BOUND_WORKTREES = 4
# Stamping a declared verification adds its observed commit, branch, dirtiness and timestamp.
STATE_LIMIT = 20480
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# A preview is read-only: it may describe an uninitialized project, because a directory
# preview and a checkout continuation both describe the operator's own files rather than
# recorded project data. Only a successful write ever creates the project row.
DIRECTORY_OPERATIONS = {"directory_scan", "directory_apply"}
PREVIEW_READS = {"directory_scan", "continuation_recover", "continuation_check"}
# The lesson book and the promoted experience book live in the same project domain but in
# a separate module. Global promotion/review is its own explicit permission.
LESSON_WRITE = {"lesson_record", "lesson_revise", "lesson_retire", "experience_withdraw"}
PROMOTE = {"experience_promote"}
# The versioned research-note book is its own module with its own operations. It reuses the
# project's registered sources and permissions instead of growing a second source library.
NOTE_OPERATIONS = {
    "note_record",
    "note_revise",
    "note_withdraw",
    "note_query",
    "note_recover",
    "note_status",
    "note_check",
}
NOTE_WRITE = {"note_record", "note_revise", "note_withdraw"}
# The paginated document catalogue and the complete-block reader: two read operations that page
# the project's own imported documents. They are their own family because they own their own
# module, their own schema gate and their own cursor rules, and because neither of them is a
# keyword search.
CATALOG_OPERATIONS = {"document_list", "document_read"}
EVIDENCE_OPERATIONS = {
    "lesson_record": "write",
    "lesson_revise": "write",
    "lesson_retire": "write",
    "lesson_query": "read",
    "lesson_recover": "read",
    "lesson_check": "read",
    "experience_withdraw": "withdraw",
    "experience_promote": "write",
    "experience_query": "read",
    "experience_check": "review",
    "experience_revoke": "review",
}
# Every operation that carries an idempotency key and records its result.
IDEMPOTENT = WRITE | LESSON_WRITE | PROMOTE | NOTE_WRITE
# Marker of a client/key binding that is persisted before the first side effect of a long
# operation. It is a claim, never a result: an identical request may resume it, and any other
# request under the same key is refused before it reads or writes anything.
CLAIM = "$claim"
# Global promotion and review need an independent explicit permission in addition to their
# own operation permission. Reusing a project's write permission is never enough.
EXTRA_PERMISSION = {
    "experience_promote": "promote",
    "experience_query": "review",
    "experience_check": "review",
    "experience_revoke": "review",
    "experience_withdraw": "review",
}


def exact(value, fields):
    return validation.exact(value, fields)


def integer(value, minimum=0, maximum=2**31):
    return validation.integer(value, minimum, maximum)


def string(value, maximum=2048):
    return validation.string(value, maximum)


class CatalogEvidence:
    """The one read the catalogue may make of this module's evidence capability.

    The catalogue pages blocks, and the project domain owns what a block reference proves. That
    capability is handed over as a port with a single method — `reference` — rather than by
    passing the whole mutable `KnowledgeApplication`, so the catalogue module can never reach
    the authorization context, the configuration, the store or any other private method of the
    module that owns these tables. The rule itself stays here: this adapter calls exactly the
    same `_reference` that `query`, `recover` and the lesson evidence already use.
    """

    __slots__ = ("_application", "_db", "_project_id", "_project", "_read")

    def __init__(self, application, db, project_id, project, read):
        self._application = application
        self._db = db
        self._project_id = project_id
        self._project = project
        self._read = read

    def reference(self, reference):
        """The resolved evidence for one block reference, or the domain's own refusal."""
        exact(reference, "block_id document_id version hash")
        string(reference["block_id"], 128)
        integer(reference["version"], 0)
        string(reference["hash"], 128)
        return self._application._reference(
            self._db, self._project_id, self._project, reference, self._read
        )


class CatalogReader(FileReader):
    """The file evidence one catalogue read needs, shared by every phase of one dispatch.

    The catalogue is its own operation family, so it gets its own reader rather than recording a
    locator on the project reader the other families use, and one reader answers every phase of one
    dispatch: the capture phase records the single locator the page depends on, the external phase
    re-reads it outside the transaction, and the serving phase compares what was really on disk
    with the recorded version hash. A file that is missing, unreadable or changing while it is read
    answers `None` instead of raising, so a catalogue page reports one stable code for every way its
    source stopped being current rather than forwarding an error the caller would have to read.
    """

    def external(self):
        """Read every recorded locator, outside any transaction."""
        for locator in self.expected:
            self.read_locator(locator)


class Sources:
    """Owns one dispatch's evidence readers so every phase shares the same caches.

    Each phase installs its own bound method as the reader, because `Evidence` captures the
    callable rather than looking it up per call.
    """

    READERS = {"knowledge": FileReader, "lessons": None}

    def __init__(self, application, first_id, project):
        self.application = application
        self.first_id = first_id
        self.project = project
        self.readers = {}
        # The working directories this one dispatch may continue in, resolved once from the
        # private configuration; nothing here is shared between dispatches.
        self.worktrees = ()

    def reader(self, project_id):
        if project_id not in self.readers:
            # The envelope's own project comes from the authorized context; any other project
            # named as evidence must be separately registered before it is read.
            source = self.project if project_id == self.first_id else None
            if source is None:
                source = self.application.projects.get(project_id)
                require(isinstance(source, dict), "project_unregistered", 403)
            self.readers[project_id] = self._new_reader(source)
        return self.readers[project_id]

    def _new_reader(self, project):
        if self.application.lesson_domain:
            from .lessons import LessonReader

            return LessonReader(project)
        if self.application.catalog_domain:
            # The catalogue's pages hold a file expectation across the external phase, so it gets
            # a reader of its own rather than one whose locators the other families also record.
            return CatalogReader(project)
        return FileReader(project)

    def bind(self, project_id, phase):
        """The reader callable for one phase of one project."""
        return getattr(self.reader(project_id), phase)

    def external(self, project_id):
        """Read everything one project's reader recorded, outside any transaction."""
        self.reader(project_id).external()

    def context(self, db, project_id, phase):
        """The lesson evidence context for one phase of one project."""
        from .lessons import SourceContext

        return SourceContext(
            self.application.projects.get(project_id),
            EvidenceLookup(self.application._document, self.application._current),
            db,
            project_id,
            reader=self.reader(project_id),
            phase=phase,
        )


class ProjectAdapter:
    """The project rows a routed domain may read, and the one project write it may perform.

    This is the knowledge domain's own storage, so its reads and its version bump live here, next
    to the tables they touch, and are handed to a routed domain as a port. A domain module
    therefore never writes SQL against `knowledge_projects` or `knowledge_states` and never
    imports this module to get at them: it receives an object that answers three questions —
    what version is the project, what state did the project declare, advance the version — and
    the schema of those tables stays owned by whoever owns them.
    """

    def __init__(self, db, project_id):
        self.db = db
        self.project_id = project_id

    def revision(self):
        """The project version, or `project_uninitialized` when the project row is absent."""
        row = self.db.execute(
            "SELECT revision FROM knowledge_projects WHERE id=?", (self.project_id,)
        ).fetchone()
        require(row is not None, "project_uninitialized", 409)
        return row["revision"]

    def declared_state(self):
        """The goal and unfinished items the project itself declared, or `(None, [])`."""
        row = self.db.execute(
            "SELECT payload FROM knowledge_states WHERE project_id=?", (self.project_id,)
        ).fetchone()
        if row is None:
            return None, []
        payload = json.loads(row["payload"])
        return payload["goal"], payload["unfinished"]

    def bump(self):
        """Advance the project version, so a cached continuation package stops being current."""
        self.db.execute(
            "UPDATE knowledge_projects SET revision=revision+1 WHERE id=?", (self.project_id,)
        )


class Plan:
    """One dispatch, expressed as three phases over the same evidence readers."""

    @classmethod
    def build(cls, application, operation, project_id, project, args, sources):
        return cls(application, operation, project_id, project, args, sources)

    def __init__(self, application, operation, project_id, project, args, sources):
        self.application = application
        self.operation = operation
        self.project_id = project_id
        self.project = project
        self.args = args
        self.sources = sources
        self.pending = {}

    def __call__(self, db, phase, seal_key=None, *, prepared=None, client=None):
        try:
            if self.application.catalog_domain:
                from .knowledge_catalog import dispatch as catalog_dispatch

                # The catalogue owns the page's field projection and every rule of paging; this
                # module hands it the authorized project, the per-dispatch seal key, the phase's
                # source reader and the narrow evidence port. It receives no application.
                return catalog_dispatch(
                    db,
                    self.operation,
                    self.project_id,
                    self.project,
                    self.args,
                    seal_key,
                    client,
                    self.sources.bind(self.project_id, phase),
                    CatalogEvidence(
                        self.application,
                        db,
                        self.project_id,
                        self.project,
                        self.sources.bind(self.project_id, phase),
                    ),
                    phase,
                )
            if self.application.note_domain:
                from .research_notes import NoteActor
                from .research_notes import dispatch as notes_dispatch

                return notes_dispatch(
                    db,
                    self.operation,
                    self.project_id,
                    self.project,
                    self.args,
                    seal_key,
                    self.sources,
                    phase,
                    # The identity is resolved once here, and the note domain's only access to
                    # project storage is the port below, implemented by the domain that owns
                    # those tables.
                    actor=NoteActor(client),
                    project_port=ProjectAdapter(db, self.project_id),
                )
            if self.application.lesson_domain:
                return self._lessons(db, phase, seal_key)
            return self.application._dispatch(
                db,
                self.operation,
                self.project_id,
                self.project,
                self.args,
                seal_key,
                self.sources.bind(self.project_id, phase),
                prepared=prepared,
                preview=phase == "capture",
                worktrees=self.sources.worktrees,
            )
        finally:
            # The captured expectations are what the external phase must read.
            reader = self.sources.reader(self.project_id)
            self.pending.update(getattr(reader, "expected", {}))

    def _lessons(self, db, phase, seal_key):
        from .lessons import dispatch as lessons_dispatch

        return lessons_dispatch(
            self.application,
            db,
            self.operation,
            self.project_id,
            self.project,
            self.args,
            seal_key,
            None,
            sources=self.sources,
            phase=phase,
        )

    def external(self, project):
        """Read every referenced evidence file outside the transaction.

        An import has already fetched its source in `_prepare_import`, so its reader has no
        captured expectations and this is a no-op. The catalogue records its one expectation on
        that same reader, so the read happens here — outside the lock — for it exactly as for the
        other families' captures.
        """
        for reader in self.sources.readers.values():
            for locator in set(self.pending) | set(getattr(reader, "expected", {})):
                reader.read_locator(locator)
        # The catalogue records its one expectation on its own reader, which knows how to turn a
        # missing or changing file into a comparison result rather than an operation error.
        if self.application.catalog_domain:
            self.sources.external(self.project_id)


class KnowledgeApplication:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.project_id = None
        self.client = None
        self.projects = {}
        self.authorized_projects = []
        self.lesson_domain = False
        # One dispatch belongs to exactly one domain module; the planner sets both flags before
        # any phase runs, and the plan reads them rather than re-deciding from the operation.
        self.note_domain = False
        self.catalog_domain = False

    def string(self, value, maximum):
        string(value, maximum)

    @staticmethod
    def integer(value, minimum=0, maximum=2**31):
        integer(value, minimum, maximum)

    @staticmethod
    def bump(db, project_id):
        KnowledgeApplication._bump(db, project_id)

    def _authorize(self, client, credential, project_id, operation, evidence_projects=()):
        """Read the private config and verify exactly what this dispatch is allowed to do.

        Returns the registered project, the credential digest, the registered scan
        directories and the working directories this client may continue in. No database is
        opened here, so a long apply can re-check authorization between its own transactions
        without taking the shared writer lock.
        """
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
        permissions = principal.get("permissions", [])
        projects = principal.get("projects", [])
        # Global promotion and review carry their own explicit permission on top of read
        # access, and every project whose data is read must be separately registered here.
        extra = EXTRA_PERMISSION.get(operation)
        require(
            operation in (READ | WRITE | NOTE_OPERATIONS | set(EVIDENCE_OPERATIONS))
            and operation in permissions
            and (extra is None or extra in permissions)
            and project_id in projects
            and set(evidence_projects) <= set(projects)
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
        for evidence_project in evidence_projects:
            registered = knowledge.get("projects", {}).get(evidence_project)
            require(isinstance(registered, dict), "project_unregistered", 403)
            exact(registered, "root host default_branch urls")
            require(
                Path(registered["root"]).is_absolute() and Path(registered["root"]).is_dir(),
                "project_unavailable",
                503,
            )
            require(
                isinstance(registered["urls"], list) and len(registered["urls"]) <= 256,
                "invalid_configuration",
                503,
            )
        # Only the directory operations read this section, so a malformed directory
        # registration cannot break the chat-adjacent project operations.
        scan = (
            directories.registration(knowledge, project_id)
            if operation in DIRECTORY_OPERATIONS
            else []
        )
        # Continuation and a commit-bound state write read the registered working directories.
        # An absent section simply means this project has none; a malformed one fails closed.
        worktrees = (
            workdir.registration(knowledge, project_id, client)
            if operation in WORKTREE_OPERATIONS
            else []
        )
        return config, knowledge, principal, project, secret, scan, worktrees

    def _context(self, client, credential, project_id, operation, evidence_projects=()):
        config, knowledge, principal, project, secret, scan, worktrees = self._authorize(
            client, credential, project_id, operation, evidence_projects
        )
        store = Store(
            config["database_path"],
            recovery_path=config.get("source_sync", {}).get("recovery_path"),
        )
        # Bound to this dispatch only: the lesson and experience modules read exactly the
        # projects, permissions and registered roots that were just authorized.
        from .lessons import authorized_projects

        self.project_id = project_id
        self.client = client
        self.projects = knowledge.get("projects", {})
        self.authorized_projects = authorized_projects(principal)
        return store, project, secret, scan, worktrees

    def execute(self, request, *, client, credential):
        exact(request, "operation project_id arguments")
        require(len(canonical(request).encode()) <= 262144, "request_too_large", 413)
        request = strict_json(canonical(request))
        string(request["project_id"], 128)
        string(request["operation"], 32)
        operation, project_id, args = (
            request["operation"],
            request["project_id"],
            request["arguments"],
        )
        # Evidence projects named by the caller are authorized before any project data is
        # read, so a refused operation cannot disclose whether a project or lesson exists.
        snapshot, factory, evidence = self._planner(operation)
        evidence_projects = tuple(evidence(args))
        store, project, secret, scan, worktrees = self._context(
            client, credential, project_id, operation, evidence_projects
        )
        if operation in DIRECTORY_OPERATIONS:
            # A directory preview and its confirmed apply re-verify every path themselves and
            # run their slow reads outside any transaction, so they own their own phases.
            return self._directory(
                request,
                operation,
                project_id,
                args,
                client,
                credential,
                store,
                project,
                secret,
                scan,
            )
        sources = Sources(self, project_id, project)
        sources.worktrees = worktrees
        plan = factory(self, operation, project_id, project, args, sources)

        # Phase 1 captures only project-related state, inside the transaction. No source I/O,
        # because that would hold the shared writer lock across the filesystem.
        with store.transaction() as db:
            revision, seal_key = snapshot(db, project_id, project, client, secret, operation)
            replay = self._replay(db, request, client)
            if replay is not None:
                return replay
            if operation == "status":
                exact(args, "key")
                string(args["key"], 128)
                row = db.execute(
                    "SELECT result FROM knowledge_imports "
                    "WHERE client=? AND key=? AND project_id=?",
                    (client, args["key"], project_id),
                ).fetchone()
                require(row is not None, "not_found", 404)
                return json.loads(row[0])
            captured = plan(db, "capture", seal_key, client=client)
        # Phase 2 does all slow work outside any transaction: imports fetch their source, every
        # referenced evidence file is re-read, and a registered working directory is observed
        # with bounded read-only Git calls. No lock is held here.
        prepared = None
        if operation == "import":
            prepared = self._prepare_import(project, args)
        elif operation in WORKTREE_OPERATIONS:
            prepared = self._observe(operation, captured, project_id, worktrees)
        plan.external(project)

        # Re-read permissions/config outside the lock, then compare project state atomically.
        current_store, current_project, current_secret, current_scan, current_worktrees = (
            self._context(client, credential, project_id, operation, evidence_projects)
        )
        require(
            current_store.path == store.path
            and current_store.recovery_path == store.recovery_path
            and current_project == project
            and current_secret == secret
            and current_scan == scan
            and current_worktrees == worktrees,
            "registration_changed",
            409,
        )
        with store.transaction() as db:
            current_revision, current_key = snapshot(
                db, project_id, project, client, secret, operation
            )
            replay = self._replay(db, request, client)
            if replay is not None:
                return replay
            require(
                current_revision == revision and current_key == seal_key, "project_conflict", 409
            )
            if revision is None and operation in WRITE:
                db.execute(
                    "INSERT INTO knowledge_projects(id,registration) VALUES (?,?)",
                    (project_id, canonical(project)),
                )
            result = plan(db, "serve", seal_key, prepared=prepared, client=client)
            if operation in IDEMPOTENT:
                db.execute(
                    "INSERT INTO knowledge_operations VALUES (?,?,?,?)",
                    (
                        client,
                        args.get("dedupe") if "dedupe" in args else args["key"],
                        fingerprint(request),
                        canonical(result),
                    ),
                )
                if operation == "import":
                    db.execute(
                        "INSERT INTO knowledge_imports VALUES (?,?,?,?,?)",
                        (client, args["key"], project_id, result["status"], canonical(result)),
                    )
            return result

    def _planner(self, operation):
        """Select the domain module, snapshot routine and evidence-project resolver.

        Each domain module owns one operation family and one schema gate; this method only
        chooses which of them a request is routed to. No rule of any family is restated here.
        """
        from . import knowledge_catalog, lessons, research_notes

        if operation in CATALOG_OPERATIONS:
            self.catalog_domain = True
            self.note_domain = False
            self.lesson_domain = False
            return knowledge_catalog.snapshot, Plan.build, lambda args: ()
        self.catalog_domain = False
        if operation in NOTE_OPERATIONS:
            self.lesson_domain = False
            self.note_domain = True
            return research_notes.snapshot, Plan.build, lambda args: ()
        self.note_domain = False
        if operation in EVIDENCE_OPERATIONS:
            self.lesson_domain = True
            return (
                lessons.snapshot,
                Plan.build,
                lessons.lesson_projects
                if operation == "experience_promote"
                else lessons.evidence_projects
                if operation in {"lesson_check", "experience_check"}
                else lambda args: (),
            )
        self.lesson_domain = False
        return self._snapshot, Plan.build, lambda args: ()

    @staticmethod
    def _seal_key(metadata, client, secret):
        """The per-dispatch cursor/seal key: the database's seal secret bound to this client.

        One derivation serves every family, so a package, a note citation and a catalogue cursor
        are all sealed under a key that no other client and no other credential can reproduce.
        The secret itself is generated once at migration and never leaves the transaction that
        reads it.
        """
        return hmac.new(
            metadata["knowledge_seal_key"].encode(),
            canonical([client, secret]).encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _snapshot(db, project_id, project, client, secret, operation):
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        require(
            metadata.get("knowledge_schema") == "1" and bool(metadata.get("knowledge_seal_key")),
            "dependency_unavailable",
            503,
        )
        seal_key = KnowledgeApplication._seal_key(metadata, client, secret)
        registered = db.execute(
            "SELECT * FROM knowledge_projects WHERE id=?", (project_id,)
        ).fetchone()
        if registered:
            require(registered["registration"] == canonical(project), "registration_changed", 409)
        else:
            require(operation in WRITE or operation in PREVIEW_READS, "project_uninitialized", 409)
        return (registered["revision"] if registered else None), seal_key

    @staticmethod
    def _replay(db, request, client):
        """The recorded result of this exact request, or None when it may still run.

        `knowledge_operations` is the one (client,key) authority for every idempotent
        operation, and a binding is persisted there before any side effect. A request whose
        key is bound to another operation, project or payload is therefore refused here, and a
        binding that never reached a result is not a result: the identical request may resume
        it instead of being told that it already succeeded.
        """
        if request["operation"] not in IDEMPOTENT:
            return None
        args = request["arguments"]
        require(isinstance(args, dict), "invalid_input", 400)
        # A revision names its lesson with `key`, so its own idempotency key is `dedupe`.
        key = args.get("dedupe") if "dedupe" in args else args.get("key")
        string(key, 128)
        old = db.execute(
            "SELECT * FROM knowledge_operations WHERE client=? AND key=?", (client, key)
        ).fetchone()
        if old is None:
            return None
        require(old["digest"] == fingerprint(request), "idempotency_conflict", 409)
        recorded = json.loads(old["result"])
        return None if CLAIM in recorded else dict(recorded, replayed=True)

    @staticmethod
    def _claim(db, operation, client, key, request):
        """Bind one client key to this exact request before any source is read or written.

        The binding lives in the same table that records results, so it also covers a key
        reused across operations, projects or plans: only a byte-identical request may pass,
        and a conflicting one fails here — before any external read and before any index row
        is written. A binding that never completed is only a claim, so the identical request
        can still resume the work instead of replaying a result that was never produced.
        """
        db.execute(
            "INSERT INTO knowledge_operations VALUES (?,?,?,?) ON CONFLICT(client,key) "
            "DO UPDATE SET digest=excluded.digest, result=excluded.result "
            "WHERE knowledge_operations.digest=excluded.digest",
            (client, key, fingerprint(request), canonical({CLAIM: operation})),
        )

    def _directory(
        self, request, operation, project_id, args, client, credential, store, project, secret, scan
    ):
        """Preview or confirm one registered directory; slow reads never hold the writer lock."""
        if operation == "directory_scan":
            return self._directory_scan(
                args, project_id, client, credential, store, project, secret, scan
            )
        return self._directory_apply(
            request, args, project_id, client, credential, store, project, secret, scan
        )

    def _reauthorize(self, client, credential, project_id, project, secret, scan, operation):
        """Re-read the private config outside the lock and refuse if authorization moved."""
        _, current_project, current_secret, current_scan, _ = self._context(
            client, credential, project_id, operation
        )
        require(
            current_project == project and current_secret == secret and current_scan == scan,
            "registration_changed",
            409,
        )

    @staticmethod
    def _ensure_project(db, project_id, project):
        """Register the project row on the first committed write; a preview never does this."""
        db.execute(
            "INSERT OR IGNORE INTO knowledge_projects(id,registration) VALUES (?,?)",
            (project_id, canonical(project)),
        )

    def _directory_scan(self, args, project_id, client, credential, store, project, secret, scan):
        """Capture the index, read the directory outside the lock, then record the preview.

        The walk reads twice and validates the decoded text with the existing importer rules,
        so the preview only offers sources an apply can really import. Only the plan itself is
        written, and an incomplete walk or an exhausted budget is recorded as such instead of
        being reported as a deletion.
        """
        directories.exact(args, "directory")
        entry = directories.require_registration(scan, args["directory"])
        with store.transaction() as db:
            revision, _ = self._snapshot(db, project_id, project, client, secret, "directory_scan")
            directories.require_schema(db)
            indexed = directories.indexed_documents(db, project_id)
        scanned = directories.scan(project, project_id, entry, indexed)
        self._reauthorize(client, credential, project_id, project, secret, scan, "directory_scan")
        with store.transaction() as db:
            current, _ = self._snapshot(db, project_id, project, client, secret, "directory_scan")
            require(current == revision, "project_conflict", 409)
            require(
                directories.versions(indexed)
                == directories.versions(directories.indexed_documents(db, project_id)),
                "project_conflict",
                409,
            )
            body = directories.plan(project_id, client, entry, project, revision, scanned)
            directories.store_plan(db, body)
            return body

    def _directory_apply(
        self, request, args, project_id, client, credential, store, project, secret, scan
    ):
        """Confirm one issued preview item by item, each item in its own transaction.

        The plan is re-checked against the recorded preview and the current registration, and
        the client key is bound to this exact request before any file is read. Every file is
        then re-read outside any transaction, and an item whose path, bytes, size or current
        index version moved becomes a conflict for that item alone, so a partial apply stays
        honest: content the preview never showed is never imported, already imported content is
        never written twice, and the recorded result can be read again after a restart.
        """
        directories.exact(args, "key plan tombstones")
        string(args["key"], 128)
        plan = directories.plan_shape(args["plan"])
        require(plan["project_id"] == project_id and plan["client"] == client, "invalid_plan", 400)
        require(plan["plan_id"] == directories.plan_fingerprint(plan), "invalid_plan", 400)
        entry = directories.require_registration(scan, plan["directory"])
        require(
            plan["limits"]["max_files"] <= entry["max_files"]
            and plan["limits"]["max_bytes"] <= entry["max_bytes"],
            "registration_changed",
            409,
        )
        approved = directories.tombstone_ids(args["tombstones"], plan)

        with store.transaction() as db:
            directories.require_schema(db)
            replay = self._replay(db, request, client)
            if replay is not None:
                return replay
            revision, _ = self._snapshot(db, project_id, project, client, secret, "directory_apply")
            row = directories.stored_plan(db, client, project_id, plan["directory"])
            require(row is not None, "plan_unissued", 404)
            require(
                row["plan_id"] == plan["plan_id"] and row["body"] == canonical(plan),
                "plan_superseded",
                409,
            )
            require(plan["registration"] == fingerprint(project), "registration_changed", 409)
            # Bind this client key to this exact request while nothing has been read or written
            # yet: a conflicting request is refused from here on, and the identical request can
            # resume a half-finished apply instead of replaying a result that was never produced.
            self._claim(db, "directory_apply", client, args["key"], request)
            self._progress(db, client, args["key"], project_id, entry, plan, None)

        observed = directories.observe(project, entry, plan)
        self._reauthorize(client, credential, project_id, project, secret, scan, "directory_apply")

        written = []
        for item in plan["items"]:
            # A long apply re-checks its own authorization before every write, so a config
            # revoked mid-run cannot keep importing after earlier items already committed.
            self._reauthorize(
                client, credential, project_id, project, secret, scan, "directory_apply"
            )
            with store.transaction() as db:
                written.append(
                    directories.import_item(
                        db,
                        DirectoryWriter(self._ensure_project, self._store_version, self._bump),
                        project_id,
                        project,
                        item,
                        observed,
                    )
                )
        tombstones = []
        for candidate in plan["missing"]:
            if candidate["document_id"] not in approved:
                # A disappearance is never a deletion unless the operator listed it explicitly.
                tombstones.append({**candidate, "outcome": "not_approved"})
                continue
            self._reauthorize(
                client, credential, project_id, project, secret, scan, "directory_apply"
            )
            with store.transaction() as db:
                tombstones.append(
                    directories.remove_source(
                        db,
                        DirectoryWriter(self._ensure_project, self._store_version, self._bump),
                        project_id,
                        candidate,
                        observed,
                    )
                )

        items = [item for item in written if item["outcome"] != "conflict"]
        conflicts = [item for item in written if item["outcome"] == "conflict"]
        self._reauthorize(client, credential, project_id, project, secret, scan, "directory_apply")
        with store.transaction() as db:
            directories.require_schema(db)
            replay = self._replay(db, request, client)
            if replay is not None:
                return replay
            found = db.execute(
                "SELECT revision FROM knowledge_projects WHERE id=?", (project_id,)
            ).fetchone()
            result = directories.applied_result(
                project_id,
                entry,
                plan,
                items,
                conflicts,
                tombstones,
                found["revision"] if found else 0,
            )
            db.execute(
                "INSERT INTO knowledge_operations VALUES (?,?,?,?) ON CONFLICT(client,key) "
                "DO UPDATE SET digest=excluded.digest, result=excluded.result",
                (client, args["key"], fingerprint(request), canonical(result)),
            )
            self._progress(db, client, args["key"], project_id, entry, plan, result)
            return result

    @staticmethod
    def _progress(db, client, key, project_id, entry, plan, result):
        """Record how far one claimed apply got, so `status` never has to guess.

        Before the first item the entry says `in_progress`; the same row is rewritten with the
        final result, so a cancelled or interrupted apply stays visibly unfinished and the
        identical request can resume it under the key it already owns.
        """
        recorded = result or directories.pending_result(project_id, entry, plan)
        db.execute(
            "INSERT INTO knowledge_imports VALUES (?,?,?,?,?) ON CONFLICT(client,key) "
            "DO UPDATE SET project_id=excluded.project_id, status=excluded.status, "
            "result=excluded.result",
            (client, key, project_id, recorded["status"], canonical(recorded)),
        )

    def _observe(self, operation, captured, project_id, worktrees):
        """Collect working-directory facts outside any transaction, after the capture phase.

        A continuation package and a commit-bound verification both need the checkout as it is
        now, so those reads happen here: between the two short transactions, never inside one.
        A checkout that cannot be observed at all turns a check into an explicit verdict rather
        than an error, while the same condition fails a recovery or a writeback closed.
        """
        if operation == "write_state":
            entries = {entry["id"]: entry for entry in worktrees}
            return {
                "worktrees": {
                    # The registered digests are read here on purpose: a recorded verification is
                    # stamped with the whole observable state of the checkout, including the
                    # files the operator registered, so a later change to any of them can be
                    # detected instead of being assumed away.
                    name: workdir.observe(entries[name])
                    for name in captured
                }
            }
        entry = workdir.require_registration(worktrees, captured["worktree"])
        try:
            return {
                "worktree": workdir.observe(entry, captured["indexed"]),
                "indexed": captured["indexed"],
            }
        except Fault as error:
            if operation != "continuation_check" or error.code not in continuation.UNOBSERVABLE:
                raise
            return {"settled": continuation.unavailable(project_id, entry["id"], error.code)}

    def _dispatch(
        self,
        db,
        operation,
        project_id,
        project,
        args,
        seal_key,
        read,
        *,
        prepared,
        preview,
        worktrees=(),
    ):
        if operation == "import":
            return self._import(db, project_id, project, args, prepared, preview)
        if operation == "delete":
            return self._delete(db, project_id, project, args, preview)
        if operation == "write_state":
            return self._write_state(
                db, project_id, project, args, read, preview, worktrees, prepared
            )
        if operation in CONTINUATION_OPERATIONS:
            handler = (
                continuation.recover if operation == "continuation_recover" else continuation.check
            )
            return handler(
                ContinuationReader(self._reference, self._query, self._seal),
                db,
                project_id,
                project,
                args,
                seal_key,
                worktrees,
                read,
                prepared,
                preview,
            )
        if operation in {"query", "recover"}:
            exact(args, "text budget_bytes")
            if operation == "query":
                return self._query(db, project_id, project, args, read)
            return self._recover(db, project_id, project, args, seal_key, read)
        if operation == "check":
            exact(args, "package")
            return self._check(db, project_id, project, args["package"], seal_key, read)
        raise Fault("invalid_input", 400)

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

    @staticmethod
    def _prepare_import(project, args):
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
            return {"error": error.code if isinstance(error, Fault) else "source_unavailable"}
        return {
            "raw": raw,
            "media": media,
            "resolved": resolved,
            "text": text,
            "units": units,
            "digest": digest,
            "index": [" ".join(terms(unit["text"])) for unit in units],
        }

    def _import(self, db, project_id, project, args, prepared, preview):
        exact(args, "key kind locator expected_version groups")
        require(args["kind"] in {"file", "url"}, "unsupported", 415)
        string(args["locator"])
        integer(args["expected_version"])
        source_id = "source:" + fingerprint([project_id, args["kind"], args["locator"]])
        document_id = "document:" + fingerprint(source_id)
        old = db.execute("SELECT * FROM knowledge_documents WHERE id=?", (document_id,)).fetchone()
        version = old["version"] if old else 0
        require(version == args["expected_version"], "version_conflict", 409)
        if preview:
            return None
        if "error" in prepared:
            code = prepared["error"]
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
        units, digest = prepared["units"], prepared["digest"]
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
        self._store_version(
            db,
            project_id,
            document_id,
            source_id,
            args["kind"],
            args["locator"],
            prepared,
            old,
            version,
        )
        return {
            "status": "imported",
            "document_id": document_id,
            "source_id": source_id,
            "version": version,
            "hash": digest,
            "blocks": len(units),
            "processing": "verbatim",
        }

    def _store_version(
        self, db, project_id, document_id, source_id, kind, locator, prepared, old, version
    ):
        """Write one imported version with its blocks and index rows.

        The caller owns the transaction. A directory apply reuses this so a confirmed preview
        and a single explicit import produce identically shaped versions, provenance and
        index rows; `old` is the current document row, or None for a first import.
        """
        if old:
            db.execute(
                "UPDATE knowledge_documents SET version=?,state='ready' WHERE id=?",
                (version, document_id),
            )
        else:
            db.execute(
                "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
                (document_id, project_id, source_id, kind, locator, version, "ready"),
            )
        provenance = {
            "kind": kind,
            "locator": locator,
            "resolved": prepared["resolved"],
            "imported_at": utc(now()),
            "processing": "verbatim",
            "citation_space": (
                "visible_text_lines" if prepared["media"] == "text/html" else "text_lines"
            ),
        }
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (
                document_id,
                version,
                prepared["digest"],
                prepared["raw"],
                prepared["text"],
                prepared["media"],
                canonical(provenance),
            ),
        )
        db.execute(
            "DELETE FROM knowledge_index WHERE block_id IN "
            "(SELECT id FROM knowledge_blocks WHERE document_id=?)",
            (document_id,),
        )
        for index, unit in enumerate(prepared["units"]):
            block_id = f"{document_id}:{version}:{index:03d}"
            db.execute(
                "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
                (block_id, document_id, version, canonical(unit)),
            )
            db.execute(
                "INSERT INTO knowledge_index VALUES (?,?,?)",
                (block_id, project_id, prepared["index"][index]),
            )
        self._bump(db, project_id)

    def _delete(self, db, project_id, project, args, preview):
        exact(args, "key document_id expected_version")
        document = self._document(db, project_id, args["document_id"])
        integer(args["expected_version"])
        require(document["version"] == args["expected_version"], "version_conflict", 409)
        require(document["state"] != "deleted", "already_deleted", 409)
        if preview:
            return None
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
    def _current(db, project, document, read):
        if document["state"] != "ready":
            return False
        if document["kind"] == "url":
            return document["locator"] in project["urls"]
        version = db.execute(
            "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
            (document["id"], document["version"]),
        ).fetchone()
        return read(document, version["hash"])

    def _reference(self, db, project_id, project, reference, read):
        exact(reference, "block_id document_id version hash")
        document = self._document(db, project_id, reference["document_id"])
        require(
            document["version"] == reference["version"]
            and self._current(db, project, document, read),
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

    def _query(self, db, project_id, project, args, read):
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
                unit = self._reference(db, project_id, project, reference, read)
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

    def _bound_worktrees(self, state, worktrees):
        """Validate every commit-bound verification and name the checkouts it refers to.

        A declared verification may name a registered working directory instead of being a bare
        sentence. That binding is checked here, before anything is written, so a state can never
        claim a checkout the caller was not authorized for; the observed commit itself is
        stamped in the serving phase, so a caller cannot declare a commit the service did not
        see as HEAD.
        """
        bound = []
        for item in state["recent_verification"]:
            if isinstance(item, str):
                continue
            exact(item, "summary worktree commit")
            string(item["summary"], 2000)
            string(item["worktree"], 64)
            require(
                item["commit"] is None
                or (isinstance(item["commit"], str) and COMMIT.match(item["commit"]) is not None),
                "invalid_input",
                400,
            )
            entry = workdir.require_registration(worktrees, item["worktree"])
            if entry["id"] not in bound:
                bound.append(entry["id"])
        require(len(bound) <= MAX_BOUND_WORKTREES, "too_many_worktrees", 400)
        return bound

    @staticmethod
    def _stamped_state(state, prepared):
        """Replace every declared binding with the commit, branch and dirtiness actually seen.

        The summary stays the operator's own words; the commit is never taken from the request.
        A declared commit that is not the observed HEAD is refused, because that is exactly the
        claim "this historical result verified this commit" that must never be recorded. The
        record also carries a fingerprint of the whole observable checkout state, so a later
        `continuation_check` can tell a second edit from the first instead of trusting HEAD, the
        dirty flag and two counts to have stayed equal.
        """
        observed = (prepared or {}).get("worktrees", {})
        stamped = []
        for item in state["recent_verification"]:
            if isinstance(item, str):
                stamped.append(item)
                continue
            facts = observed.get(item["worktree"])
            require(isinstance(facts, dict), "workdir_unavailable", 503)
            require(
                item["commit"] is None or item["commit"] == facts["head"],
                "workdir_conflict",
                409,
            )
            # A binding may only be recorded when the uncommitted state can be described
            # completely: an incomplete description could never be compared later, so the record
            # would claim more than the service can prove.
            require(facts["changes"]["complete"], "workdir_unproven", 409)
            stamped.append(
                {
                    "summary": item["summary"],
                    "worktree": item["worktree"],
                    "commit": facts["head"],
                    "branch": facts["branch"],
                    "dirty": facts["dirty"],
                    "declared_at": facts["collected_at"],
                    "facts": continuation.facts_fingerprint(facts),
                }
            )
        return {**state, "recent_verification": stamped}

    def _write_state(
        self, db, project_id, project, args, read, preview, worktrees=(), prepared=None
    ):
        """Store one explicit project state; a caller may bind a verification to a checkout."""
        exact(args, "key expected_version state")
        integer(args["expected_version"])
        state = args["state"]
        exact(state, "goal constraints recent_verification unfinished evidence pitfalls")
        string(state["goal"], 2000)
        for field in ("constraints", "unfinished"):
            require(
                isinstance(state[field], list) and len(state[field]) <= 16, "invalid_input", 400
            )
            for item in state[field]:
                string(item, 2000)
        require(
            isinstance(state["recent_verification"], list)
            and len(state["recent_verification"]) <= 16,
            "invalid_input",
            400,
        )
        require(
            isinstance(state["evidence"], list) and 0 < len(state["evidence"]) <= 16,
            "evidence_required",
            400,
        )
        for reference in state["evidence"]:
            self._reference(db, project_id, project, reference, read)
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
        bound = self._bound_worktrees(state, worktrees)
        require(len(canonical(state).encode()) <= 16384, "state_too_large", 413)
        old = db.execute(
            "SELECT version FROM knowledge_states WHERE project_id=?", (project_id,)
        ).fetchone()
        version = old["version"] if old else 0
        require(version == args["expected_version"], "version_conflict", 409)
        if preview:
            return bound
        stored = self._stamped_state(state, prepared)
        require(len(canonical(stored).encode()) <= STATE_LIMIT, "state_too_large", 413)
        db.execute(
            "INSERT INTO knowledge_states VALUES (?,?,?) ON CONFLICT(project_id) "
            "DO UPDATE SET version=excluded.version,payload=excluded.payload",
            (project_id, version + 1, canonical(stored)),
        )
        db.execute(
            "INSERT INTO knowledge_state_history VALUES (?,?,?)",
            (project_id, version + 1, canonical(stored)),
        )
        self._bump(db, project_id)
        return {
            "status": "written",
            "project_id": project_id,
            "state_version": version + 1,
            "authority": "explicit_project_note",
            "sharing": "project_only",
            "verified_worktrees": sorted(
                {
                    item["worktree"]
                    for item in stored["recent_verification"]
                    if isinstance(item, dict)
                }
            ),
        }

    @staticmethod
    def _seal(package, secret):
        return hmac.new(secret.encode(), canonical(package).encode(), hashlib.sha256).hexdigest()

    def _recover(self, db, project_id, project, args, secret, read):
        integer(args["budget_bytes"], 1024, 32768)
        result = self._query(db, project_id, project, args, read)
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
                    self._reference(db, project_id, project, r, read) for r in payload["evidence"]
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

    def _check(self, db, project_id, project, package, secret, read):
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
                    self._reference(db, project_id, project, block["reference"], read)
            except Fault:
                valid = False
        return {"valid": bool(valid), "reason": "current" if valid else "stale_or_tampered"}
