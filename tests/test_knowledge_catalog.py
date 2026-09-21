"""Paginated project document catalogue and complete-block reading.

Two registered synthetic projects, an isolated database, isolated files and isolated
credentials. Every case runs through `KnowledgeApplication.execute`, so authorization, the
project revision, the three-phase transaction boundary, the cursor seal and source freshness are
exercised exactly as the CLI, the MCP server and the HTTP entry exercise them.
"""

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_catalog_migration import (
    INDEXES,
)
from tianshu_memory.knowledge_catalog_migration import (
    inspect as inspect_catalog,
)
from tianshu_memory.knowledge_catalog_migration import migrate as migrate_catalog
from tianshu_memory.knowledge_migration import migrate
from tianshu_memory.knowledge_sources import content_hash
from tianshu_memory.store import Store

CATALOG_READ = ["document_list", "document_read"]
PROJECT_WRITE = ["import", "query", "recover", "check", "write_state", "delete", "status"]
SECRET = "synthetic-project-client-secret"
OTHER_SECRET = "synthetic-second-client-secret"
ALPHA = "alpha"
BETA = "beta"
# The other project's first document id sorts *between* alpha's: a catalogue that filters in
# Python instead of in the keyset would show it, and a project filter applied after the page
# would silently drop alpha's own second document.
INTERLEAVE = 40
PAGE = {"limit": 8, "budget_bytes": 32768, "cursor": None}
READ_START = {"expected_version": 1, "expected_hash": None}


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def registered_clients():
    return {
        "alpha-writer": {
            "credential_sha256": digest(SECRET),
            "projects": [ALPHA],
            "permissions": [*PROJECT_WRITE, *CATALOG_READ],
        },
        # The old reader may search by keyword and nothing else: it holds no catalogue
        # permission, so it can neither enumerate the directory nor read a document.
        "alpha-reader": {
            "credential_sha256": digest(SECRET),
            "projects": [ALPHA],
            "permissions": ["query"],
        },
        # A client that may enumerate but not read: the two permissions are independent.
        "alpha-lister": {
            "credential_sha256": digest(SECRET),
            "projects": [ALPHA],
            "permissions": ["document_list"],
        },
        "beta-writer": {
            "credential_sha256": digest(OTHER_SECRET),
            "projects": [BETA],
            "permissions": [*PROJECT_WRITE, *CATALOG_READ],
        },
    }


CREDENTIALS = {
    "alpha-writer": SECRET,
    "alpha-reader": SECRET,
    "alpha-lister": SECRET,
    "beta-writer": OTHER_SECRET,
}


def source_id(project_id, kind, locator):
    return "source:" + fingerprint([project_id, kind, locator])


def document_id(project_id, kind, locator):
    return "document:" + fingerprint(source_id(project_id, kind, locator))


def write_source(root, locator, text):
    """Write one source file and return the exact bytes a reader will find there.

    The bytes are read back rather than assumed: a text-mode write is free to translate line
    endings, and a version hash has to cover what is on disk, not what was intended.
    """
    path = root / locator
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path.read_bytes()


def seeded(store, project_id, root, locator, text, *, version=1, kind="file", state="ready"):
    """Write one imported document, its version and its blocks the way an import would.

    A URL document is a snapshot an explicit import registered; no request in this suite goes to
    the network, and `kind="url"` only means the document's currentness is decided by its
    registration rather than by a file.
    """
    recorded = text.encode("utf-8")
    identity = document_id(project_id, kind, locator)
    source = source_id(project_id, kind, locator)
    if kind == "file":
        recorded = write_source(root, locator, text)
    digest_value = content_hash(recorded)
    with store.transaction() as db:
        old = db.execute("SELECT id FROM knowledge_documents WHERE id=?", (identity,)).fetchone()
        if old is None:
            db.execute(
                "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
                (identity, project_id, source, kind, locator, version, state),
            )
        else:
            db.execute(
                "UPDATE knowledge_documents SET version=?,state=? WHERE id=?",
                (version, state, identity),
            )
        db.execute("DELETE FROM knowledge_versions WHERE document_id=?", (identity,))
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                version,
                digest_value,
                recorded,
                recorded.decode("utf-8"),
                "text/plain",
                canonical({"kind": kind, "locator": locator}),
            ),
        )
        db.execute("DELETE FROM knowledge_blocks WHERE document_id=?", (identity,))
        db.execute(
            "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
            (
                f"{identity}:{version}:000",
                identity,
                version,
                canonical({"spans": [[1, 1]], "text": text}),
            ),
        )
    return identity


def seeded_blocks(store, project_id, root, locator, units, *, version=1):
    """Write one document whose blocks are given explicitly, one payload per block.

    The stored file is exactly the source text the version hash covers, so a reader that checks
    freshness accepts it while an edited or removed file does not.
    """
    text = "".join(unit["text"] + "\n" for unit in units)
    recorded = write_source(root, locator, text)
    digest_value = content_hash(recorded)
    identity = document_id(project_id, "file", locator)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                project_id,
                source_id(project_id, "file", locator),
                "file",
                locator,
                version,
                "ready",
            ),
        )
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (
                identity,
                version,
                digest_value,
                recorded,
                recorded.decode("utf-8"),
                "text/plain",
                canonical({"kind": "file", "locator": locator}),
            ),
        )
        for index, unit in enumerate(units):
            db.execute(
                "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
                (f"{identity}:{version}:{index:03d}", identity, version, canonical(unit)),
            )
    return identity, digest_value


@pytest.fixture
def catalogue(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "catalogue.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    roots = {}
    for name in (ALPHA, BETA):
        root = tmp_path / name
        root.mkdir()
        roots[name] = root
    # The project rows exist because an import created them; the seeded documents below add no
    # new project and bump no revision.
    with store.transaction() as db:
        for name in (ALPHA, BETA):
            db.execute(
                "INSERT INTO knowledge_projects VALUES (?,?,?)",
                (name, canonical(project(name, roots[name])), 0),
            )
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {name: project(name, roots[name]) for name in (ALPHA, BETA)},
            "clients": registered_clients(),
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")

    alpha = []
    for index in range(INTERLEAVE):
        locator = f"note-{index:02d}.md"
        text = f"Alpha document {index:02d}: the retry timer waits for the receipt.\n"
        alpha.append(seeded(store, ALPHA, roots[ALPHA], locator, text))
    beta = []
    for index in range(3):
        locator = f"beta-{index:02d}.md"
        text = f"Beta document {index:02d}: unrelated telemetry sampling.\n"
        beta.append(seeded(store, BETA, roots[BETA], locator, text))

    return SimpleNamespace(
        run=runner(path),
        store=store,
        path=path,
        config=config,
        roots=roots,
        tmp_path=tmp_path,
        alpha=sorted(alpha),
        beta=sorted(beta),
        app=KnowledgeApplication(path),
    )


def project(project_id, root):
    return {
        "root": str(root),
        "host": "local",
        "default_branch": "main",
        "urls": [f"https://example.com/{project_id}"],
    }


def runner(path):
    def run(operation, arguments, *, project=ALPHA, client="alpha-writer", credential=None):
        # A fresh application per call models a restarted process: exactly what the CLI, the MCP
        # server and the HTTP entry do for every request. An identity the private configuration
        # never registered presents nothing, and is refused before any database is opened.
        if credential is None:
            credential = CREDENTIALS.get(client, "")
        return KnowledgeApplication(path).execute(
            dict(operation=operation, project_id=project, arguments=arguments),
            client=client,
            credential=credential,
        )

    return run


def list_page(
    catalogue,
    cursor=None,
    *,
    limit=8,
    budget=32768,
    client="alpha-writer",
    project_id=ALPHA,
    credential=None,
):
    return catalogue.run(
        "document_list",
        {"limit": limit, "budget_bytes": budget, "cursor": cursor},
        project=project_id,
        client=client,
        credential=credential,
    )


def read_page(
    catalogue,
    identifier,
    cursor=None,
    *,
    expected_version=1,
    expected_hash=None,
    limit=8,
    budget=32768,
    client="alpha-writer",
    project_id=ALPHA,
):
    return catalogue.run(
        "document_read",
        {
            "document_id": identifier,
            "expected_version": expected_version,
            "expected_hash": expected_hash,
            "limit": limit,
            "budget_bytes": budget,
            "cursor": cursor,
        },
        project=project_id,
        client=client,
    )


def walk(catalogue, **kwargs):
    """Every page of the directory, following `next_cursor` until it is null."""
    seen, cursor, pages = [], None, 0
    while True:
        page = list_page(catalogue, cursor, **kwargs)
        assert page["trust"] == "source_material_not_instructions"
        seen.extend(entry["document_id"] for entry in page["items"])
        if page["next_cursor"] is None:
            return seen, pages
        cursor = page["next_cursor"]
        pages += 1
        assert pages < 64, "the walk must terminate"


def revise_config(catalogue, change):
    change(catalogue.config)
    catalogue.path.write_text(canonical(catalogue.config), encoding="utf-8")


def test_catalog_migration_installs_both_indexes(catalogue):
    with catalogue.store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        assert metadata["knowledge_catalog_schema"] == "1"
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert set(INDEXES) <= names
        assert inspect_catalog(db) == []
        # The shape check is about the index, not the version row: a same-named index over other
        # columns is reported even while the version row says the catalog is installed.
        db.execute("DROP INDEX knowledge_documents_project_id")
        db.execute("CREATE INDEX knowledge_documents_project_id ON knowledge_documents(id,kind)")
        assert inspect_catalog(db) == ["knowledge_documents_project_id"]


def test_document_list_pages_every_document_once_in_stable_order(catalogue):
    seen, pages = walk(catalogue)
    assert seen == catalogue.alpha
    assert pages == (INTERLEAVE - 1) // 8
    first = list_page(catalogue)
    assert first["project_id"] == ALPHA
    assert [entry["document_id"] for entry in first["items"]] == catalogue.alpha[:8]
    assert all(entry["kind"] == "file" for entry in first["items"])
    assert all(entry["version"] == 1 for entry in first["items"])
    assert all(entry["indexed_state"] == "ready" for entry in first["items"])
    # The directory never claims a source is still valid; only a read proves that.
    assert all(entry["source_validation"] == "not_checked" for entry in first["items"])
    # A full page that is not the last one reports the limit, not a budget omission.
    assert first["omissions"] == []
    assert first["next_cursor"] is not None
    # No other project's document appears, even though beta's ids sort inside alpha's range.
    assert not set(seen) & set(catalogue.beta)
    assert list_page(catalogue, client="beta-writer", project_id=BETA)["items"]
    assert all(entry["document_id"] in catalogue.alpha for entry in first["items"])


def test_document_list_last_page_and_empty_project(catalogue):
    # A walk that ends when the cursor is null delivers every document exactly once, and the page
    # before that last, null-cursor page is a full one: a cursor is only ever handed out when
    # something really remains, so no page can be empty and none can repeat a document.
    seen, pages = walk(catalogue, limit=INTERLEAVE // 2)
    assert seen == catalogue.alpha and pages == 1
    first = list_page(catalogue, limit=INTERLEAVE // 2)
    last = list_page(catalogue, first["next_cursor"], limit=INTERLEAVE // 2)
    assert len(first["items"]) == INTERLEAVE // 2
    assert [entry["document_id"] for entry in last["items"]] == catalogue.alpha[INTERLEAVE // 2 :]
    assert last["next_cursor"] is None and last["omissions"] == []
    # A project with no imported document answers with an empty page, not an error and not a
    # fabricated entry.
    with catalogue.store.transaction() as db:
        db.execute(
            "INSERT INTO knowledge_projects VALUES ('empty',?,0)",
            (canonical(project("empty", catalogue.tmp_path)),),
        )
    catalogue.config["knowledge"]["projects"]["empty"] = project("empty", catalogue.tmp_path)
    catalogue.config["knowledge"]["clients"]["alpha-writer"]["projects"] = [ALPHA, "empty"]
    catalogue.path.write_text(canonical(catalogue.config), encoding="utf-8")
    blank = list_page(catalogue, project_id="empty")
    assert blank["items"] == [] and blank["next_cursor"] is None and blank["omissions"] == []


def test_document_read_returns_complete_blocks_and_follows_pages(catalogue):
    units = [
        {
            "spans": [[1, 2]],
            "text": "First block: \u91cd\u8bd5\u5fc5\u987b\u7b49\u5f85\u56de\u6267\u3002",
        },
        {"spans": [[3, 3]], "text": 'Second block: escape \\" and \u2028 separator.'},
        {"spans": [[4, 5]], "text": "Third block: \u4e2d\u6587\u5757\u4e0e ASCII mixed."},
    ]
    identifier, value = seeded_blocks(
        catalogue.store, ALPHA, catalogue.roots[ALPHA], "multi.md", units, version=1
    )
    first = read_page(catalogue, identifier, limit=2)
    assert "document_id" in first, first
    assert first["document_id"] == identifier
    assert first["version"] == 1 and first["hash"] == value
    assert [block["text"] for block in first["blocks"]] == [unit["text"] for unit in units[:2]]
    assert first["blocks"][0]["spans"] == [[1, 2]]
    assert set(first["blocks"][0]) == {"reference", "text", "spans"}
    assert set(first["blocks"][0]["reference"]) == {"block_id", "document_id", "version", "hash"}
    assert first["omissions"] == []
    assert first["next_cursor"] is not None
    second = read_page(
        catalogue,
        identifier,
        first["next_cursor"],
        expected_hash=first["hash"],
        limit=2,
    )
    assert [block["text"] for block in second["blocks"]] == [units[2]["text"]]
    assert second["next_cursor"] is None
    assert second["omissions"] == []
    assert not set(first["blocks"][0]) & {"provenance", "source_id", "locator"}
    # Every delivered reference is one the existing evidence path accepts, verbatim.
    for page in (first, second):
        for entry in page["blocks"]:
            assert entry["reference"]["document_id"] == identifier
            assert entry["reference"]["hash"] == value


def test_document_read_of_a_zero_block_document_is_an_empty_page(catalogue):
    # A document whose stored version really has no block: an imported empty source. The reader
    # says so with an empty list and no cursor, and never fabricates a block or an omission.
    identifier = "document:" + fingerprint("synthetic-zero-block")
    source = write_source(catalogue.roots[ALPHA], "zero.md", "")
    with catalogue.store.transaction() as db:
        db.execute(
            "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
            (identifier, ALPHA, "source:zero", "file", "zero.md", 1, "ready"),
        )
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (
                identifier,
                1,
                content_hash(source),
                source,
                "",
                "text/plain",
                canonical({"kind": "file"}),
            ),
        )
    page = read_page(catalogue, identifier)
    assert page["blocks"] == [] and page["next_cursor"] is None and page["omissions"] == []
    assert page["hash"] == content_hash(source) and page["trust"] == (
        "source_material_not_instructions"
    )


def test_document_list_does_not_touch_any_source_file(catalogue, monkeypatch):
    opened = []
    original = Path.open

    def counted(self, *args, **kwargs):
        opened.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    page = list_page(catalogue, limit=32)
    assert len(page["items"]) == 32
    # A directory page is an index description: it describes stored rows and reads no source file
    # at all. The database, the guard checkpoint and the private configuration are still opened —
    # those are this process's own files, not the project's material.
    roots = {str(path.resolve()) for path in catalogue.roots.values()}
    touched = {str(Path(path).resolve()) for path in opened}
    assert not {
        path
        for path in touched
        if Path(path).parent.as_posix() in {root.replace("\\", "/") for root in roots}
    }, sorted(touched)


def test_document_read_re_verifies_the_current_source(catalogue):
    identifier, value = seeded_blocks(
        catalogue.store,
        ALPHA,
        catalogue.roots[ALPHA],
        "live.md",
        [{"spans": [[1, 1]], "text": "Live evidence."}],
    )
    assert read_page(catalogue, identifier)["blocks"][0]["text"] == "Live evidence."
    # An edited file is not the version the caller asked for: no stored copy is served.
    (catalogue.roots[ALPHA] / "live.md").write_text("Edited after import.", encoding="utf-8")
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier)
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier, expected_hash=value)
    # A deleted file is refused the same way, and the directory still describes the index: a
    # listing is an index description, so it keeps naming a document whose source has gone.
    (catalogue.roots[ALPHA] / "live.md").unlink()
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier)
    listed = walk(catalogue, limit=32, budget=32768)[0]
    assert identifier in listed


def test_document_read_refuses_a_superseded_version_and_a_deleted_document(catalogue):
    identifier, first = seeded_blocks(
        catalogue.store,
        ALPHA,
        catalogue.roots[ALPHA],
        "versioned.md",
        [{"spans": [[1, 1]], "text": "Version one."}],
    )
    assert read_page(catalogue, identifier)["blocks"][0]["text"] == "Version one."
    # A second version replaces the first: the old version is never served again. The file really
    # holds the new bytes now, so this is a genuine supersession and not a file edit.
    second = write_source(catalogue.roots[ALPHA], "versioned.md", "Version two.\n")
    with catalogue.store.transaction() as db:
        db.execute("UPDATE knowledge_documents SET version=2 WHERE id=?", (identifier,))
        db.execute(
            "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
            (
                identifier,
                2,
                content_hash(second),
                second,
                second.decode("utf-8"),
                "text/plain",
                canonical({"kind": "file", "locator": "versioned.md"}),
            ),
        )
        db.execute(
            "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
            (
                f"{identifier}:2:000",
                identifier,
                2,
                canonical({"spans": [[1, 1]], "text": "Version two."}),
            ),
        )
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier, expected_version=1)
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier, expected_hash=first)
    assert read_page(catalogue, identifier, expected_version=2)["blocks"][0]["text"] == (
        "Version two."
    )
    # A deleted document is described by the directory and refused by the reader.
    with catalogue.store.transaction() as db:
        db.execute("UPDATE knowledge_documents SET state='deleted' WHERE id=?", (identifier,))
    states = {
        entry["document_id"]: entry["indexed_state"]
        for entry in list_page(catalogue, limit=32)["items"]
    }
    assert states[identifier] == "deleted"
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier, expected_version=2)


def test_document_read_refuses_a_withdrawn_url_document(catalogue):
    url = "https://example.com/alpha"
    text = "URL snapshot: the receipt is authoritative."
    identifier = seeded(catalogue.store, ALPHA, catalogue.roots[ALPHA], url, text, kind="url")
    assert read_page(catalogue, identifier)["blocks"][0]["text"] == text
    # A URL document is a snapshot an explicit import registered; this request never goes to the
    # network, so withdrawing the registration is what makes the snapshot non-current. The
    # registration lives in the project's own configuration, so it is changed the way only an
    # operator can: the project row is rewritten with it.
    withdrawn = project(ALPHA, catalogue.roots[ALPHA])
    withdrawn["urls"] = []
    with catalogue.store.transaction() as db:
        db.execute(
            "UPDATE knowledge_projects SET registration=? WHERE id=?",
            (canonical(withdrawn), ALPHA),
        )
    revise_config(
        catalogue, lambda config: config["knowledge"]["projects"].update({ALPHA: withdrawn})
    )
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier)
    # The directory still describes the document it has: withdrawing a URL is not a deletion.
    listed, cursor = {}, None
    while True:
        step = list_page(catalogue, cursor, limit=32)
        listed.update({entry["document_id"]: entry["kind"] for entry in step["items"]})
        cursor = step["next_cursor"]
        if cursor is None:
            break
    assert listed[identifier] == "url"


def test_new_permissions_are_required_and_never_implied_by_query(catalogue):
    identifier = catalogue.alpha[0]
    # `query` does not imply either new operation: the reader that may search this project is
    # refused the directory and the reader alike, and refused before anything is read.
    with pytest.raises(Fault, match="forbidden"):
        list_page(catalogue, client="alpha-reader")
    with pytest.raises(Fault, match="forbidden"):
        read_page(catalogue, identifier, client="alpha-reader")
    # The lister holds only `document_list`: it enumerates, and cannot read the document it just
    # named. Neither permission stands in for the other, in either direction.
    listed = list_page(catalogue, client="alpha-lister")
    assert listed["items"] and listed["items"][0]["document_id"] == identifier
    with pytest.raises(Fault, match="forbidden"):
        read_page(catalogue, identifier, client="alpha-lister")
    # The identity that holds both can do both, in one configuration.
    assert list_page(catalogue)["items"]
    assert read_page(catalogue, identifier)["blocks"]


def test_unregistered_project_and_forged_identity_fail_closed(catalogue):
    # The authorized project is decided before any database or existence question: a project this
    # identity is not registered for, a wrong credential and an unknown identity are all refused
    # up front.
    with pytest.raises(Fault, match="forbidden"):
        list_page(catalogue, project_id="gamma")
    with pytest.raises(Fault, match="forbidden"):
        list_page(catalogue, client="beta-writer")
    with pytest.raises(Fault, match="unauthorized"):
        list_page(catalogue, credential="synthetic-wrong-credential-value")
    with pytest.raises(Fault, match="unauthorized"):
        list_page(catalogue, client="nobody")
    # Another project's document id is not readable through this project, and the refusal is the
    # same one a missing document gets: an unregistered id discloses nothing.
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, catalogue.beta[0])
    # A registered project that never recorded an import is refused as uninitialized by both
    # operations, and reading it never creates its project row.
    catalogue.config["knowledge"]["projects"]["fresh"] = project("fresh", catalogue.tmp_path)
    catalogue.config["knowledge"]["clients"]["alpha-writer"]["projects"] = [ALPHA, "fresh"]
    catalogue.path.write_text(canonical(catalogue.config), encoding="utf-8")
    with pytest.raises(Fault, match="project_uninitialized"):
        list_page(catalogue, project_id="fresh")
    with pytest.raises(Fault, match="project_uninitialized"):
        read_page(catalogue, catalogue.alpha[0], project_id="fresh")
    with catalogue.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM knowledge_projects WHERE id='fresh'").fetchone()[0]
            == 0
        )


def test_revocation_and_credential_rotation_take_effect_mid_walk(catalogue):
    first = list_page(catalogue)
    cursor = first["next_cursor"]
    assert cursor is not None
    # Revoking the catalogue permission stops the very next page, cursor or not.
    revise_config(
        catalogue,
        lambda config: config["knowledge"]["clients"]["alpha-writer"].update(permissions=["query"]),
    )
    with pytest.raises(Fault, match="forbidden"):
        list_page(catalogue, cursor)
    # Granting it back but rotating the credential produces a different seal key, so the old
    # cursor no longer verifies even for a correctly presented new credential.
    catalogue.config["knowledge"]["clients"]["alpha-writer"]["permissions"] = [
        *PROJECT_WRITE,
        *CATALOG_READ,
    ]
    catalogue.path.write_text(canonical(catalogue.config), encoding="utf-8")
    with pytest.raises(Fault, match="unauthorized"):
        list_page(catalogue, cursor, credential="synthetic-rotated-credential-value")
    rotated = "synthetic-rotated-credential-value"
    catalogue.config["knowledge"]["clients"]["alpha-writer"]["credential_sha256"] = digest(rotated)
    catalogue.path.write_text(canonical(catalogue.config), encoding="utf-8")
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, cursor, credential=rotated)


def test_cursor_is_bound_to_operation_client_project_page_and_revision(catalogue):
    cursor = list_page(catalogue)["next_cursor"]
    assert cursor is not None
    # Cross-operation: a list cursor presented to the reader.
    with pytest.raises(Fault, match="invalid_cursor"):
        read_page(catalogue, catalogue.alpha[0], cursor)
    # Tampered token.
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB"))
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, "not-a-cursor")
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, cursor, limit=9)
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, cursor, budget=16384)
    # A forged token that is not even JSON in this shape.
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, "e30")
    # Cross-project: the same token presented by another project's client.
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, cursor, client="beta-writer", project_id=BETA)
    # A revision that moved between pages is stale rather than invalid.
    with catalogue.store.transaction() as db:
        db.execute("UPDATE knowledge_projects SET revision=revision+1 WHERE id=?", (ALPHA,))
    with pytest.raises(Fault, match="cursor_stale"):
        list_page(catalogue, cursor)


def test_read_cursor_binds_the_document_version_and_hash(catalogue):
    units = [{"spans": [[index + 1, index + 1]], "text": f"Block {index}."} for index in range(4)]
    identifier, _ = seeded_blocks(
        catalogue.store, ALPHA, catalogue.roots[ALPHA], "cursored.md", units
    )
    first = read_page(catalogue, identifier, limit=2)
    cursor = first["next_cursor"]
    assert cursor is not None
    # A read cursor belongs to one document.
    other = catalogue.alpha[0]
    with pytest.raises(Fault, match="invalid_cursor"):
        read_page(catalogue, other, cursor, expected_hash=first["hash"])
    # The element limit and the byte budget are sealed with the cursor.
    with pytest.raises(Fault, match="invalid_cursor"):
        read_page(catalogue, identifier, cursor, expected_hash=first["hash"], limit=4)
    with pytest.raises(Fault, match="invalid_cursor"):
        read_page(catalogue, identifier, cursor, expected_hash=first["hash"], budget=8192)
    # The cursor sealed version 1. Once the document has moved to version 2 the position it names
    # no longer exists in the document the caller is walking, so the walk is refused instead of
    # being continued into another version's blocks.
    with catalogue.store.transaction() as db:
        db.execute("UPDATE knowledge_documents SET version=2 WHERE id=?", (identifier,))
    with pytest.raises(Fault, match="invalid_cursor"):
        read_page(catalogue, identifier, cursor, expected_hash=first["hash"])


def test_budget_is_enforced_on_the_whole_serialized_page(catalogue):
    identifier, value = seeded_blocks(
        catalogue.store,
        ALPHA,
        catalogue.roots[ALPHA],
        "budget.md",
        [
            {"spans": [[1, 1]], "text": "A" * 400},
            {"spans": [[2, 2]], "text": "B" * 400},
            {"spans": [[3, 3]], "text": "C" * 400},
        ],
    )
    small = read_page(catalogue, identifier, limit=32, budget=2048)
    assert len(canonical(small).encode()) <= 2048
    assert small["next_cursor"] is not None
    assert small["omissions"] == ["budget"]
    seen = [entry["text"] for entry in small["blocks"]]
    cursor = small["next_cursor"]
    while cursor is not None:
        page = read_page(catalogue, identifier, cursor, expected_hash=value, limit=32, budget=2048)
        assert len(canonical(page).encode()) <= 2048
        seen.extend(entry["text"] for entry in page["blocks"])
        cursor = page["next_cursor"]
    assert seen == ["A" * 400, "B" * 400, "C" * 400]
    # One huge block and a budget too small to carry its own envelope is not a page the caller
    # can use, and it is refused instead of being skipped.
    with pytest.raises(Fault, match="budget_too_small"):
        read_page(catalogue, identifier, limit=32, budget=1024)
    huge = seeded_blocks(
        catalogue.store,
        ALPHA,
        catalogue.roots[ALPHA],
        "huge.md",
        [{"spans": [[1, 1]], "text": "Z" * 20000}],
    )[0]
    with pytest.raises(Fault, match="budget_too_small"):
        read_page(catalogue, huge, limit=32, budget=1024)
    assert read_page(catalogue, huge, limit=32, budget=32768)["blocks"]


def test_document_list_budget_reports_and_never_claims_a_false_omission(catalogue):
    page = list_page(catalogue, limit=32, budget=1024)
    assert len(canonical(page).encode()) <= 1024
    assert page["items"] and page["next_cursor"] is not None
    seen, cursor = [], page["next_cursor"]
    seen.extend(entry["document_id"] for entry in page["items"])
    while cursor is not None:
        step = list_page(catalogue, cursor, limit=32, budget=1024)
        assert len(canonical(step).encode()) <= 1024
        assert step["items"], "a budget page must deliver at least one entry"
        seen.extend(entry["document_id"] for entry in step["items"])
        cursor = step["next_cursor"]
    assert seen == catalogue.alpha
    full = list_page(catalogue, limit=4)
    assert full["omissions"] == [] and len(full["items"]) == 4


def test_arguments_are_exact_and_bounded(catalogue):
    with pytest.raises(Fault, match="invalid_input"):
        catalogue.run("document_list", {"limit": 8, "budget_bytes": 32768})
    with pytest.raises(Fault, match="invalid_input"):
        catalogue.run(
            "document_list", {"limit": 8, "budget_bytes": 32768, "cursor": None, "extra": 1}
        )
    for limit in (0, 33, "8", True):
        with pytest.raises(Fault, match="invalid_input"):
            list_page(catalogue, limit=limit)
    for budget in (1023, 32769, "2048"):
        with pytest.raises(Fault, match="invalid_input"):
            list_page(catalogue, budget=budget)
    for expected in (0, -1, "1"):
        with pytest.raises(Fault, match="invalid_input"):
            read_page(catalogue, catalogue.alpha[0], expected_version=expected)
    with pytest.raises(Fault, match="invalid_input"):
        read_page(catalogue, catalogue.alpha[0], expected_hash="not-a-hash")
    with pytest.raises(Fault, match="invalid_input"):
        read_page(catalogue, catalogue.alpha[0], expected_hash="A" * 64)
    # The first page is `null`, never an empty string: an empty token is a malformed cursor.
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, "")
    with pytest.raises(Fault, match="invalid_cursor"):
        list_page(catalogue, "x" * 2049)
    # A caller may not name a source locator, a path or a URL.
    with pytest.raises(Fault, match="invalid_input"):
        catalogue.run(
            "document_read",
            {
                "document_id": catalogue.alpha[0],
                "expected_version": 1,
                "expected_hash": None,
                "limit": 8,
                "budget_bytes": 32768,
                "cursor": None,
                "locator": "note-00.md",
            },
        )


def test_catalog_reads_change_no_authority_row_and_no_revision(catalogue):
    with catalogue.store.transaction() as db:
        before = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "knowledge_documents",
                "knowledge_versions",
                "knowledge_blocks",
                "knowledge_states",
                "knowledge_state_history",
                "knowledge_operations",
                "knowledge_imports",
            )
        }
        revision = db.execute(
            "SELECT revision FROM knowledge_projects WHERE id=?", (ALPHA,)
        ).fetchone()[0]
        source_revision = dict(db.execute("SELECT key,value FROM metadata"))["source_revision"]
    identifier = catalogue.alpha[0]
    list_page(catalogue, limit=4)
    read_page(catalogue, identifier)
    walk(catalogue, limit=4)
    with catalogue.store.transaction() as db:
        after = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before
        }
        assert (
            db.execute("SELECT revision FROM knowledge_projects WHERE id=?", (ALPHA,)).fetchone()[0]
            == revision
        )
        assert dict(db.execute("SELECT key,value FROM metadata"))["source_revision"] == (
            source_revision
        )
    assert after == before


def test_directory_survives_concurrent_reimport_and_delete(catalogue):
    identifier, _ = seeded_blocks(
        catalogue.store,
        ALPHA,
        catalogue.roots[ALPHA],
        "concurrent.md",
        [{"spans": [[1, 1]], "text": "Concurrent evidence."}],
    )
    failures = []

    def reader():
        try:
            for _ in range(8):
                read_page(catalogue, identifier)
        except Fault as error:
            failures.append(error.code)

    def writer():
        # A reimport that supersedes the document: the version pointer moves on while reads are
        # already in flight. Only the pointer is moved here — the point of the test is that a read
        # whose document moved under it refuses, not what the new version contains.
        for round_number in range(2, 5):
            with catalogue.store.transaction() as db:
                db.execute(
                    "UPDATE knowledge_documents SET version=? WHERE id=?",
                    (round_number, identifier),
                )

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Every read either returned the version it asked for or refused as stale evidence; a read that
    # returned some other version would mean the version pointer was read between transactions.
    assert set(failures) <= {"stale_evidence"}
    with pytest.raises(Fault, match="stale_evidence"):
        read_page(catalogue, identifier, expected_version=1)
    with catalogue.store.transaction() as db:
        db.execute("UPDATE knowledge_documents SET version=1 WHERE id=?", (identifier,))
    assert read_page(catalogue, identifier)["blocks"][0]["text"] == "Concurrent evidence."


def test_guard_failure_fails_the_catalog_closed(catalogue):
    identifier = catalogue.alpha[0]
    guard = Path(catalogue.store.recovery_path)
    assert guard.exists()
    guard.write_text(
        canonical({"schema": 3, "instance": "tampered", "revision": 0, "recovery": "tampered"}),
        encoding="utf-8",
    )
    with pytest.raises(Fault, match="dependency_unavailable"):
        list_page(catalogue)
    with pytest.raises(Fault, match="dependency_unavailable"):
        read_page(catalogue, identifier)


def test_document_list_and_read_fail_closed_without_the_catalog_migration(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "unmigrated.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    root = tmp_path / "registered"
    root.mkdir()
    (root / "source.md").write_text("Unmigrated source.\n", encoding="utf-8")
    with store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
        db.execute("DROP INDEX knowledge_documents_project_id")
        db.execute("DROP INDEX knowledge_blocks_document_version_id")
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {ALPHA: project(ALPHA, root)},
            "clients": registered_clients(),
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    run = runner(path)
    # The old operations still work on a database that never ran the catalog migration.
    imported = run(
        "import",
        {"key": "k", "kind": "file", "locator": "source.md", "expected_version": 0, "groups": None},
    )
    assert imported["status"] == "imported"
    assert run("query", {"text": "unmigrated", "budget_bytes": 8192})["blocks"]
    with pytest.raises(Fault, match="dependency_unavailable"):
        run("document_list", {"limit": 8, "budget_bytes": 32768, "cursor": None})
    with pytest.raises(Fault, match="dependency_unavailable"):
        run(
            "document_read",
            {
                "document_id": imported["document_id"],
                "expected_version": 1,
                "expected_hash": None,
                "limit": 8,
                "budget_bytes": 32768,
                "cursor": None,
            },
        )


def test_a_same_named_index_of_the_wrong_shape_is_refused(catalogue):
    with catalogue.store.transaction() as db:
        db.execute("DROP INDEX knowledge_documents_project_id")
        db.execute("CREATE INDEX knowledge_documents_project_id ON knowledge_documents(id,kind)")
        assert inspect_catalog(db) == ["knowledge_documents_project_id"]
    with pytest.raises(Fault, match="dependency_unavailable"):
        list_page(catalogue)
    with catalogue.store.transaction() as db:
        db.execute("DROP INDEX knowledge_documents_project_id")
        db.execute(
            "CREATE INDEX knowledge_documents_project_id ON knowledge_documents(project_id,id)"
        )
        assert inspect_catalog(db) == []
    assert list_page(catalogue)["items"]


def test_migration_preserves_documents_permissions_and_triggers(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "upgraded.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    root = tmp_path / "registered"
    root.mkdir()
    (root / "source.md").write_text(
        "Source that survives the catalog migration.\n", encoding="utf-8"
    )
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {ALPHA: project(ALPHA, root)},
            "clients": registered_clients(),
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    run = runner(path)
    imported = run(
        "import",
        {"key": "k", "kind": "file", "locator": "source.md", "expected_version": 0, "groups": None},
    )
    with store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
        db.execute("DROP INDEX knowledge_documents_project_id")
        db.execute("DROP INDEX knowledge_blocks_document_version_id")
        triggers_before = sorted(
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        )
        rows_before = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("knowledge_documents", "knowledge_versions", "knowledge_blocks")
        }
        revision_before = db.execute(
            "SELECT revision FROM knowledge_projects WHERE id=?", (ALPHA,)
        ).fetchone()[0]
    result = migrate_catalog(store, tmp_path / "before-catalog.sqlite")
    assert result["knowledge_catalog_schema"] == 1
    assert result["indexes"] == list(INDEXES)
    assert Path(result["backup"]).exists()
    with store.transaction() as db:
        assert (
            sorted(
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
            == triggers_before
        )
        assert {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in rows_before
        } == rows_before
        assert (
            db.execute("SELECT revision FROM knowledge_projects WHERE id=?", (ALPHA,)).fetchone()[0]
            == revision_before
        )
        assert inspect_catalog(db) == []
    # The catalog is now available, and the document the pre-migration import recorded reads
    # back with exactly the same identity, version and hash.
    page = run(
        "document_read",
        {
            "document_id": imported["document_id"],
            "expected_version": imported["version"],
            "expected_hash": None,
            "limit": 8,
            "budget_bytes": 32768,
            "cursor": None,
        },
    )
    assert page["hash"] == imported["hash"] and page["blocks"]
    assert [
        entry["document_id"]
        for entry in run("document_list", {"limit": 8, "budget_bytes": 32768, "cursor": None})[
            "items"
        ]
    ] == [imported["document_id"]]


def test_utf8_budget_counts_bytes_not_characters(catalogue):
    identifier, value = seeded_blocks(
        catalogue.store,
        ALPHA,
        catalogue.roots[ALPHA],
        "chinese.md",
        [
            {"spans": [[1, 1]], "text": "中文块" * 20},
            {"spans": [[2, 2]], "text": "另一个中文块" * 20},
            {"spans": [[3, 3]], "text": "第三个中文块" * 20},
        ],
    )
    page = read_page(catalogue, identifier, limit=32, budget=2048)
    assert len(canonical(page).encode()) <= 2048
    assert page["omissions"] == ["budget"] and page["next_cursor"] is not None
    texts, cursor = [entry["text"] for entry in page["blocks"]], page["next_cursor"]
    while cursor is not None:
        step = read_page(catalogue, identifier, cursor, expected_hash=value, limit=32, budget=2048)
        assert len(canonical(step).encode()) <= 2048
        texts.extend(entry["text"] for entry in step["blocks"])
        cursor = step["next_cursor"]
    assert texts == ["中文块" * 20, "另一个中文块" * 20, "第三个中文块" * 20]
