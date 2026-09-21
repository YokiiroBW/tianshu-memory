"""Paginated project document catalogue and complete semantic-block reading.

This module is the knowledge domain's own implementation of two operations:

- `document_list` walks one authorized project's imported documents by `document_id` in
  stable ascending keyset order and describes each one without reading any source file;
- `document_read` returns, page by page, the complete blocks of one document's *current*
  version, reusing the project domain's existing evidence check so historical text can never
  be served for a document whose file changed, whose version moved, or whose URL left the
  registered set.

Both operations are deliberately narrow. They add no keyword search (the existing lexical
`query` stays the only one), no directory walk, no second source library and no second
authentication path. They read exactly two tables — `knowledge_documents` and
`knowledge_blocks` — through statements whose keyset prefix is covered by the two indexes
`knowledge_catalog_migration` installs, and they refuse to run at all when those indexes are
absent or have the wrong shape, because a plan without them would silently scan every other
project and every historical version.

Identity, permissions, the three-phase transaction boundary, the project revision and the
per-dispatch seal key all come from the existing application: this module receives an already
authorized project, a `seal_key` derived by the domain's own snapshot routine, a `read`
callable for source freshness, and one narrow `Evidence` port. It never imports the HTTP
entry, never reads the private configuration, and never holds a lock across a file read.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re

from . import knowledge_catalog_migration as catalog_migration
from .domain import Fault, canonical, require
from .validate import exact, integer, string

# The keyset page bounds the card fixes. A page delivers at most `limit` elements and is decided
# from at most `limit + 1` candidates, so the next page needs no second query and no table-wide
# count.
MIN_LIMIT = 1
MAX_LIMIT = 32
MIN_BUDGET_BYTES = 1024
MAX_BUDGET_BYTES = 32768
# The whole serialized response — cursor, omissions and every multi-byte character included —
# must fit in the caller's byte budget. This is the only budget the operations have: there is no
# second per-element allowance that could add up to more than the caller asked for.
MAX_CURSOR_CHARS = 2048
TRUST = "source_material_not_instructions"
# A directory entry never claims its source is still valid: only `document_read` proves that,
# and it proves it for the current version only. `indexed_state` is the stored index state and
# is never advertised as "the current file is still valid".
SOURCE_VALIDATION = "not_checked"
# A hash in the same format the source library writes for an imported version.
HASH_FORMAT = re.compile(r"[0-9a-f]{64}\Z")
# `omissions` is either empty or exactly this, and only when the byte budget — never the element
# limit — is what stopped the page.
BUDGET = "budget"
# The seal purpose of a catalogue cursor. A cursor sealed for one purpose is refused for another,
# even when the same client, project and seal key produced it, so the two operations can never
# accept each other's tokens.
CURSOR_PURPOSE = "knowledge-catalog-cursor"
CURSOR_VERSION = 1
# The one page shape a caller may change between pages is where the page starts; the element
# limit and the byte budget are sealed, because a page assembled under one budget must not be
# replayed under another and then claimed to have honoured it. A cursor that changed either is
# refused as an invalid cursor rather than silently re-paged.
LIST_FIELDS = "limit budget_bytes cursor"
READ_FIELDS = "document_id expected_version expected_hash limit budget_bytes cursor"
ITEM_FIELDS = "document_id kind version indexed_state source_validation"
BLOCK_FIELDS = "reference text spans"
REFERENCE_FIELDS = "block_id document_id version hash"


def seal_cursor(seal_key, operation, client, project_id, revision, limit, budget_bytes, **bound):
    """Seal one page position: who asked, for what, at which revision, and where it stopped.

    `bound` carries what the next page must still agree on — the `last_id` where the previous
    page ended and, for a read, the document identity, version and hash the caller is
    continuing inside. The token is deterministic, versioned and length-bounded, and it is
    signed with the existing per-dispatch project seal key under this module's own purpose; no
    new database signing secret is introduced anywhere.
    """
    fields = {
        "operation": operation,
        "client": client,
        "project_id": project_id,
        "revision": revision,
        "limit": limit,
        "budget_bytes": budget_bytes,
        **bound,
    }
    require(len(canonical(fields).encode()) <= MAX_CURSOR_CHARS, "invalid_input", 400)
    body = canonical([CURSOR_VERSION, CURSOR_PURPOSE, [fields]])
    seal = hmac.new(seal_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(canonical([[fields], seal]).encode()).decode()
    require(len(token) <= MAX_CURSOR_CHARS, "invalid_input", 400)
    return token


def open_cursor(token, seal_key, operation, client, project_id, revision, limit, budget_bytes):
    """Verify one presented cursor and return the page position it seals.

    Every refusal here is `invalid_cursor`: a malformed token, a shape this version does not
    define, a seal that does not verify, a cursor sealed for another operation, client or
    project, or a page whose element limit or byte budget was changed after it was sealed. The
    project revision is the single exception — a cursor that verifies but was sealed at an older
    revision is `cursor_stale`, which tells the caller to restart from the first page instead of
    silently continuing against a catalog that has moved.
    """
    require(isinstance(token, str) and 0 < len(token) <= MAX_CURSOR_CHARS, "invalid_cursor", 400)
    try:
        padded = token.encode() + b"=" * (-len(token) % 4)
        outer = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise Fault("invalid_cursor", 400) from None
    require(isinstance(outer, list) and len(outer) == 2, "invalid_cursor", 400)
    fields, presented = outer
    require(
        isinstance(fields, list)
        and len(fields) == 1
        and isinstance(fields[0], dict)
        and isinstance(presented, str)
        and len(presented) == 64,
        "invalid_cursor",
        400,
    )
    body = canonical([CURSOR_VERSION, CURSOR_PURPOSE, fields])
    expected = hmac.new(seal_key.encode(), body.encode(), hashlib.sha256).hexdigest()
    require(hmac.compare_digest(expected, presented), "invalid_cursor", 400)
    bound = fields[0]
    require(
        bound.get("operation") == operation
        and bound.get("client") == client
        and bound.get("project_id") == project_id
        and bound.get("limit") == limit
        and bound.get("budget_bytes") == budget_bytes,
        "invalid_cursor",
        400,
    )
    require(type(bound.get("revision")) is int, "invalid_cursor", 400)
    # An authenticated cursor from an older revision is not a tamper: the catalog moved, so the
    # caller restarts from the first page rather than being handed the rest of a stale walk.
    require(bound["revision"] == revision, "cursor_stale", 409)
    return bound


def seal_key(metadata, client, secret):
    """The per-dispatch seal key, derived by the project domain's own rule."""
    return hmac.new(
        metadata["knowledge_seal_key"].encode(),
        canonical([client, secret]).encode(),
        hashlib.sha256,
    ).hexdigest()


def snapshot(db, project_id, project, client, secret, operation):
    """The catalogue's schema gate and per-dispatch seal key.

    The catalogue schema is a versioned metadata row, and the indexes it promises are checked by
    shape on every request rather than assumed from that row: a database whose index was renamed,
    dropped or rebuilt over other columns reports the dependency as unavailable instead of
    answering from a plan the card did not fix.
    """
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    require(
        metadata.get("knowledge_schema") == "1" and bool(metadata.get("knowledge_seal_key")),
        "dependency_unavailable",
        503,
    )
    require(
        metadata.get("knowledge_catalog_schema") == catalog_migration.VERSION,
        "dependency_unavailable",
        503,
    )
    require_catalog(db)
    registered = db.execute("SELECT * FROM knowledge_projects WHERE id=?", (project_id,)).fetchone()
    # A catalogue read never registers a project: a project that recorded no import is refused as
    # uninitialized rather than created by a read.
    require(registered is not None, "project_uninitialized", 409)
    require(registered["registration"] == canonical(project), "registration_changed", 409)
    return registered["revision"], seal_key(metadata, client, secret)


def require_catalog(db):
    """Whether the catalog indexes are present with the shape the pagination plans use.

    Used by the request path through `snapshot` and by `Catalog` itself; both refuse with
    `dependency_unavailable` rather than degrading to a scan of every project and version.
    """
    require(not catalog_migration.inspect(db), "dependency_unavailable", 503)


def page_arguments(args, fields):
    """The frozen page arguments every catalog operation shares."""
    exact(args, fields)
    integer(args["limit"], MIN_LIMIT, MAX_LIMIT)
    integer(args["budget_bytes"], MIN_BUDGET_BYTES, MAX_BUDGET_BYTES)
    require(args["cursor"] is None or isinstance(args["cursor"], str), "invalid_input", 400)
    return {"limit": args["limit"], "budget_bytes": args["budget_bytes"]}


def read_arguments(args):
    """The frozen `document_read` arguments, or a refusal.

    A caller may not name a source path, a locator or a URL: the document identity is the only
    handle, and everything about where its bytes came from stays in the domain.

    The first page may leave `expected_hash` open — the caller does not know the digest until the
    first page tells it. Every page after that must carry the digest it is continuing, which is a
    condition on the request itself and is therefore decided here, with the other input checks,
    before any cursor is opened or any source is read.
    """
    page = page_arguments(args, READ_FIELDS)
    string(args["document_id"], 128)
    integer(args["expected_version"], 1, 2**31)
    require(
        args["expected_hash"] is None
        or (isinstance(args["expected_hash"], str) and HASH_FORMAT.match(args["expected_hash"])),
        "invalid_input",
        400,
    )
    if args["cursor"] is not None:
        require(args["expected_hash"] is not None, "invalid_input", 400)
    return page


def item(document, indexed):
    """One directory entry: identity and stored index state, never a source claim."""
    return {
        "document_id": document["id"],
        "kind": document["kind"],
        "version": document["version"],
        "indexed_state": indexed,
        "source_validation": SOURCE_VALIDATION,
    }


def block(row, unit):
    """One complete semantic block: the caller's own reference plus the whole stored unit."""
    return {
        "reference": {
            "block_id": row["id"],
            "document_id": row["document_id"],
            "version": row["version"],
            "hash": row["hash"],
        },
        "text": unit["text"],
        "spans": unit["spans"],
    }


def unit_of(row, evidence):
    """The stored unit of one block row, verified as current evidence when a port is wired.

    The knowledge domain owns what a block reference proves, so when an evidence port is present
    every block on the page is put through that same check before any of it is returned. A block
    whose proof no longer holds is not skipped — skipping it would advance the cursor past
    content the caller never saw — so the whole page is refused as stale evidence and one
    omission kind stays the only one this operation reports.
    """
    if evidence is not None:
        evidence.reference(
            {
                "block_id": row["id"],
                "document_id": row["document_id"],
                "version": row["version"],
                "hash": row["hash"],
            }
        )
    return json.loads(row["payload"])


def envelope(base, payload, next_cursor, omissions):
    """The one response shape both operations return, with exactly the card's own keys.

    Every document field comes from `base` — the page's identity and revision, plus, for a read,
    the document id, version and hash — and `payload` carries only the delivered elements. The two
    are kept apart on purpose: a page must never be able to overwrite the identity it is reporting
    with something that came out of the paging loop.

    The response names the frozen fields and nothing else. The page's element limit is part of the
    request and of the signed cursor, but it is not part of the answer: a caller that wants to know
    what it asked for already knows, and a wire field the card did not fix is a field a later
    reader would have to treat as contractual.
    """
    return {
        "project_id": base["project_id"],
        "project_revision": base["revision"],
        **base["extra"],
        **payload,
        "next_cursor": next_cursor,
        "omissions": omissions,
        "trust": TRUST,
    }


def fits(body, budget_bytes):
    return len(canonical(body).encode("utf-8")) <= budget_bytes


def page(base, payload_key, elements, cursor_for, budget_bytes, more_candidates):
    """Assemble the largest prefix of `elements` whose *actual* response fits the byte budget.

    `cursor_for(index)` seals the position after `elements[index]`, so a cursor only ever names an
    element that was not delivered: no element can be skipped between two pages and no page can
    deliver one twice. The budget is measured on the response that would really be sent, never on a
    hypothetical one. Three outcomes are distinguished, in the card's own words:

    - the first element of a non-empty candidate list does not fit while a response without it
      does: `budget_too_small`, with no cursor at all, so the caller must raise the budget instead
      of silently losing the head of the catalogue;
    - the budget stopped the page with an element left over: `next_cursor` plus the single omission
      `budget`;
    - the element limit stopped the page, or the candidate list ended inside it: `next_cursor` only
      when something really remains, and `omissions` stays empty — a full page is not a budget
      omission and is never reported as one.

    What the last two lines distinguish is *what the page really is*, not what a pessimal version of
    it would cost. The candidate list here is `limit + 1` rows at most, so "there is another page"
    and "this is the last page" are both already known before a single byte is measured:

    - when the caller's limit already covers every candidate, the page is the last one and carries
      no cursor. Charging it the cost of a cursor or of a `budget` omission would refuse a response
      that fits — a complete final page of one block is the case this exists for;
    - when a candidate was held back, every prefix but the last needs a cursor for the page after
      it, and the last one needs a cursor sealed exactly where the element limit would have sealed
      it. Either way the cursor is real and is counted.

    The work stays bounded by the page: at most `limit + 1` candidates are serialized, the loop
    stops at the first size that fits, and no element is ever truncated or skipped.
    """
    if not elements:
        return envelope(base, {payload_key: []}, None, [])
    limit = base["limit"]
    last = min(limit, len(elements)) - 1
    # The complete page, as it would really be sent. When no candidate was held back, this is the
    # last page of the walk and the response that needs no cursor is the one to measure.
    if not more_candidates:
        body = envelope(base, {payload_key: elements[: last + 1]}, None, [])
        if fits(body, budget_bytes):
            return body
    stopped = None
    for candidate in range(last + 1):
        # A cursor exists after this candidate unless it is the last element the walk can reach and
        # nothing was held back — the one case where the next page does not exist. The omission
        # follows the cursor: it says the budget is what stood between this page and the next one,
        # which can only be true while a further element was in reach.
        further = candidate < last
        sealed = cursor_for(candidate) if further or more_candidates else None
        body = envelope(
            base, {payload_key: elements[: candidate + 1]}, sealed, [BUDGET] if further else []
        )
        if not fits(body, budget_bytes):
            stopped = candidate
            break
    if stopped is None:
        return envelope(
            base,
            {payload_key: elements[: last + 1]},
            cursor_for(last) if more_candidates else None,
            [],
        )
    if stopped == 0:
        # Even the first element does not fit beside the cursor the rest of the walk needs. The page
        # is still deliverable when those candidates are covered by the element limit, in which case
        # its real response carries no cursor; otherwise the caller must raise the budget rather
        # than lose the head of the catalogue.
        if more_candidates or last > 0:
            raise Fault("budget_too_small", 400)
        require(
            fits(envelope(base, {payload_key: elements[:1]}, None, []), budget_bytes),
            "budget_too_small",
            400,
        )
        return envelope(base, {payload_key: elements[:1]}, None, [])
    # The budget held an element back, so the cursor is real and the omission follows from what
    # stopped the page: fewer elements than the element limit could have delivered.
    return envelope(
        base,
        {payload_key: elements[:stopped]},
        cursor_for(stopped - 1),
        [BUDGET] if stopped < min(limit, len(elements)) else [],
    )


def unavailable(document, project):
    """Whether a document cannot be readable at all, whatever its file says.

    This is the part of the project domain's evidence rule that needs no I/O: a document that is
    not `ready` is not readable, and a URL document is readable only while its exact URL is still
    registered. A URL document is a snapshot an explicit import recorded; the reader never goes to
    the network, so removing the registration — not a failed fetch — is what makes it non-current.
    """
    return document["state"] != "ready" or (
        document["kind"] == "url" and document["locator"] not in project["urls"]
    )


def fresh(read, document, expected):
    """The project domain's own freshness check for one file-backed version, once read."""
    try:
        return bool(read(document, expected))
    except (Fault, OSError, ValueError):
        return False


class Catalog:
    """One catalog dispatch, as the same three phases every other operation goes through.

    `capture` runs inside the transaction and only reads rows; the one file a read depends on is
    re-read between the transactions by the reader this dispatch was handed; `serve` re-checks the
    catalog indexes, the project revision and the source freshness inside a final transaction and
    only then assembles the page. Nothing here writes a row, bumps a project revision, registers a
    project or touches an idempotency ledger: a list is an index description and a read is a
    projection of already-imported evidence.
    """

    def __init__(self, db, operation, project_id, project, args, seal_key, client, read, evidence):
        self.db = db
        self.operation = operation
        self.project_id = project_id
        self.project = project
        self.args = args
        self.seal_key = seal_key
        self.client = client
        self.read = read
        self.evidence = evidence
        self.base = None
        self.bound = None

    @property
    def reader(self):
        """The reader this dispatch records its one file expectation on.

        The catalogue reads its page's source freshness through this reader, which is the domain's
        own evidence reader for this family: it answers `None` for a file that is missing,
        unreadable or changing while read, so every way a source stops being current arrives here
        as one comparison result rather than as an error this module would have to interpret.
        """
        return self.read.__self__

    def external(self):
        """Read the locator this dispatch recorded, outside any transaction."""
        self.reader.external()

    def __call__(self, phase):
        """The catalogue's part in one phase: capture records, serve delivers, nothing else does.

        The project domain runs the same three phases for every family and hands the *serve*
        phase's value back to the caller. Nothing is assembled in the capture phase: it checks the
        schema, the page position and the project revision, and — for a read — records which one
        source must be re-read by the phase that runs outside the lock. A refusal raised there
        could not tell an edited file from one not yet read, and a page built there would be a page
        no phase of this module assembled for delivery.
        """
        shape = (
            page_arguments(self.args, LIST_FIELDS)
            if self.operation == "document_list"
            else read_arguments(self.args)
        )
        if self.base is None:
            self._capture(shape)
        if phase is not None and phase != "serve":
            if self.operation == "document_read":
                self._announce_read()
            return None
        if self.operation == "document_list":
            return self.list_documents(shape)
        return self.read_document(shape)

    def _continues(self, bound, document_id, document, version):
        """Whether this request really continues the page its cursor was sealed for.

        The cursor proves what the caller was reading; the request says what the caller is asking
        for now. Both have to name the same document, the same version and the same hash, and the
        request may not name a version or a hash of its own that disagrees — that is not a page to
        continue but a different request, refused as an invalid cursor. The request's own premise is
        checked against the document first, so a caller pointing at a version or a digest the
        document no longer has still gets `stale_evidence` rather than a cursor verdict.
        """
        require(
            bound.get("document_id") == document_id
            and bound.get("version") == document["version"]
            and bound.get("version") == self.args["expected_version"]
            and bound.get("hash") == version["hash"],
            "invalid_cursor",
            400,
        )
        # A first page may leave the hash open; a later one may not. Without it the caller would be
        # continuing a walk it never bound to a digest, which is the one thing the cursor is for.
        require(self.args["expected_hash"] is not None, "invalid_input", 400)

    def _announce_read(self):
        """Record the source this read must have re-read, so the external phase reads it.

        This is the catalogue's whole part in the phase between the two transactions. Only the
        current, structurally readable version of the named document is announced, and a document
        no read could ever serve is refused here rather than after a pointless file read. The page
        position is settled first: a request that contradicts the cursor it presented fails before
        anything is read, exactly as it would in the serving phase.
        """
        document = self._document(self.args["document_id"])
        version = None
        if document is not None:
            version = self.db.execute(
                "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
                (document["id"], document["version"]),
            ).fetchone()
        require(
            document is not None
            and version is not None
            and not unavailable(document, self.project)
            and document["version"] == self.args["expected_version"]
            and (
                self.args["expected_hash"] is None or self.args["expected_hash"] == version["hash"]
            ),
            "stale_evidence",
            409,
        )
        if self.bound is not None:
            self._continues(self.bound, self.args["document_id"], document, version)
        if document["kind"] == "file":
            self.reader.capture(document, version["hash"])

    def _cursor(self, page):
        """The verified position this page continues from, or None for the first page."""
        token = self.args["cursor"]
        if token is None:
            return None
        return open_cursor(
            token,
            self.seal_key,
            self.operation,
            self.client,
            self.project_id,
            self.base["revision"],
            page["limit"],
            page["budget_bytes"],
        )

    def _seal(self, last_id):
        return seal_cursor(
            self.seal_key,
            self.operation,
            self.client,
            self.project_id,
            self.base["revision"],
            self.args["limit"],
            self.args["budget_bytes"],
            last_id=last_id,
        )

    def _document(self, document_id):
        return self.db.execute(
            "SELECT * FROM knowledge_documents WHERE id=? AND project_id=?",
            (document_id, self.project_id),
        ).fetchone()

    def _capture(self, shape):
        """Phase one: the schema gate, the page position and the starting revision."""
        require_catalog(self.db)
        registered = self.db.execute(
            "SELECT revision FROM knowledge_projects WHERE id=?", (self.project_id,)
        ).fetchone()
        # A read never creates a project. A project that never recorded an import is refused as
        # uninitialized, exactly as every other read of an empty project is.
        require(registered is not None, "project_uninitialized", 409)
        self.base = {
            "project_id": self.project_id,
            "revision": registered["revision"],
            "extra": {},
            # The element limit is what the page assembler bounds itself by. It is a request
            # parameter, not a response field: `envelope` never emits it.
            "limit": shape["limit"],
        }
        self.bound = self._cursor(shape)

    def list_documents(self, shape):
        """One keyset page of directory entries, in stable ascending `document_id` order.

        Two things about this statement are deliberate, and both are about one page costing one
        page rather than one project:

        - the lower bound is always a string, the empty one for the first page, which no document id
          can be. Writing the bound as `(? IS NULL OR id>?)` reads the same to a caller and does not
          read the same to SQLite, which then uses the index for `project_id` alone and filters
          every later document of the project in turn;
        - the index is named. A keyset that starts near the beginning of the table leaves the
          planner free to prefer the `id` primary key, which it then walks across every other
          project to find this project's rows. The shape gate has already proved this index exists
          with these exact columns before any statement runs, so naming it states the plan the
          migration was reviewed for instead of hoping for it.
        """
        last_id = (self.bound or {}).get("last_id")
        require(last_id is None or isinstance(last_id, str), "invalid_cursor", 400)
        rows = self.db.execute(
            "SELECT id,kind,version,state FROM knowledge_documents INDEXED BY "
            "knowledge_documents_project_id WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
            (self.project_id, last_id or "", shape["limit"] + 1),
        ).fetchall()
        more = len(rows) > shape["limit"]
        elements = [item(row, row["state"]) for row in rows[: shape["limit"]]]
        return page(
            self.base,
            "items",
            elements,
            lambda index: self._seal(elements[index]["document_id"]),
            shape["budget_bytes"],
            more,
        )

    def read_document(self, shape):
        """One keyset page of the current version's complete blocks of one document.

        A continuation page carries the same premise the first page did — the caller names the
        version and the hash it is reading. The cursor repeats that premise so a page cannot be
        continued after the document moved; it does not replace it. A request whose own
        `expected_version`/`expected_hash` disagree with the cursor is a request that no longer
        describes the page it is asking for, and it is refused as an invalid cursor rather than
        answered with text the caller did not ask for.
        """
        document_id = self.args["document_id"]
        bound = self.bound
        document = self._document(document_id)
        # The caller's own version and hash binding, and any document that is not the current one,
        # are decided before a single block is assembled. A deleted document, a withdrawn URL, a
        # changed file and a superseded version all land here, so no historical text leaves.
        version = None
        if document is not None:
            version = self.db.execute(
                "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
                (document_id, document["version"]),
            ).fetchone()
        require(
            document is not None
            and version is not None
            and document["version"] == self.args["expected_version"]
            and (
                self.args["expected_hash"] is None or self.args["expected_hash"] == version["hash"]
            )
            and not unavailable(document, self.project),
            "stale_evidence",
            409,
        )
        if bound is not None:
            self._continues(bound, document_id, document, version)
        # A file-backed version is current only once its bytes have really been read and found to
        # hash to the recorded digest. The external phase re-read the locator this dispatch
        # announced, so this comparison is what turns that read into evidence: a file edited,
        # replaced or removed between the two transactions fails here and never yields text.
        structural = True
        if document["kind"] == "file":
            structural = fresh(self.read, document, version["hash"])
        require(structural, "stale_evidence", 409)
        last_id = (bound or {}).get("last_id")
        require(last_id is None or isinstance(last_id, str), "invalid_cursor", 400)
        # The same one-bound keyset as the directory, and the same named index for the same reason:
        # the first page starts from the empty string, which is below every block id, so all three
        # keyset columns are one index seek instead of a document walk with the id filtered after.
        rows = self.db.execute(
            "SELECT b.id,b.document_id,b.version,b.payload,v.hash FROM knowledge_blocks b "
            "INDEXED BY knowledge_blocks_document_version_id "
            "JOIN knowledge_versions v ON v.document_id=b.document_id AND v.version=b.version "
            "WHERE b.document_id=? AND b.version=? AND b.id>? "
            "ORDER BY b.id LIMIT ?",
            (document_id, document["version"], last_id or "", shape["limit"] + 1),
        ).fetchall()
        more = len(rows) > shape["limit"]
        units = [block(row, unit_of(row, self.evidence)) for row in rows[: shape["limit"]]]
        body = {
            **self.base,
            "limit": shape["limit"],
            "extra": {
                "document_id": document_id,
                "version": document["version"],
                "hash": version["hash"],
            },
        }
        return page(
            body,
            "blocks",
            units,
            lambda index: seal_cursor(
                self.seal_key,
                self.operation,
                self.client,
                self.project_id,
                self.base["revision"],
                self.args["limit"],
                self.args["budget_bytes"],
                last_id=units[index]["reference"]["block_id"],
                document_id=document_id,
                version=document["version"],
                hash=version["hash"],
            ),
            shape["budget_bytes"],
            more,
        )


def dispatch(db, operation, project_id, project, args, seal_key, client, read, evidence, phase):
    """Route one phase of one catalog operation into its own domain implementation."""
    catalog = Catalog(db, operation, project_id, project, args, seal_key, client, read, evidence)
    return catalog(phase)
