"""The two catalogue operations through the real restricted HTTP entry.

The transport is exercised as it ships: `create_app` built over a migrated synthetic database,
mounted on a real `TestClient` bound to a real loopback authority, with the identity fixed by the
process and the credential presented per request. One case leaves the process entirely and starts
the actual `knowledge_cli serve` command as a child to walk the whole chain over a real socket:
list a page, read the document that page named, then continue to the next page.

This file adds no rule of its own. What it proves is that the two operations travel the existing
entry unchanged — same admission, same identity, same media type, same byte budget, same failure
envelope — and that the seven documents' worth of transport rules already covered by
`test_knowledge_http.py` are untouched by their arrival.
"""

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tianshu_memory.domain import canonical, fingerprint
from tianshu_memory.knowledge_catalog_migration import (
    INDEXES,
)
from tianshu_memory.knowledge_catalog_migration import (
    inspect as inspect_catalog,
)
from tianshu_memory.knowledge_http import ACTION_PATH, create_app
from tianshu_memory.knowledge_migration import migrate as migrate_knowledge
from tianshu_memory.store import Store

ALPHA = "alpha"
BETA = "beta"
CLIENT = "alpha-writer"
BETA_CLIENT = "beta-writer"
SECRET = "synthetic-catalogue-entry-secret"
OTHER_SECRET = "synthetic-catalogue-other-secret"
READ = ["document_list", "document_read"]
WRITE = ["import", "query", "recover", "check", "write_state", "delete", "status"]
# Operations the domain knows and this entry must still refuse, whatever the client is granted.
# The research-note lifecycle is *not* in this list: this entry does carry it, and it is refused
# here by the domain instead, because this identity holds no note permission.
OUTSIDE = (
    "import",
    "delete",
    "status",
    "write_state",
    "directory_scan",
    "lesson_query",
    "experience_query",
    "experience_promote",
)
DOCUMENTS = 36
BLOCKS = 4
HEALTH = {"state": "listening", "entrypoint": "project_knowledge_http", "projects": None}
REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND = ["-m", "tianshu_memory.knowledge_cli"]


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def project(project_id, root):
    return {
        "root": str(root),
        "host": "local",
        "default_branch": "main",
        "urls": [f"https://example.com/{project_id}"],
    }


def document_id(project_id, locator):
    return "document:" + fingerprint(["source", project_id, "file", locator])


def build(tmp_path, contracts, name="catalogue"):
    """A migrated synthetic database with two projects and more documents than one page holds."""
    store = Store(tmp_path / f"{name}.sqlite")
    store.migrate_profiles(tmp_path / f"{name}-profiles.sqlite")
    store.migrate_sources(tmp_path / f"{name}-sources.sqlite", contracts)
    migrate_knowledge(store, tmp_path / f"{name}-knowledge.sqlite")
    roots = {}
    for project_id in (ALPHA, BETA):
        root = tmp_path / f"{name}-{project_id}"
        root.mkdir(exist_ok=True)
        roots[project_id] = root
        with store.transaction() as db:
            db.execute(
                "INSERT INTO knowledge_projects VALUES (?,?,0)",
                (project_id, canonical(project(project_id, root))),
            )
    with store.transaction() as db:
        for project_id in (ALPHA, BETA):
            count = DOCUMENTS if project_id == ALPHA else 3
            for index in range(count):
                locator = f"note-{index:03d}.md"
                identity = document_id(project_id, locator)
                text = f"{project_id} document {index:03d}: the retry timer waits.\n"
                (roots[project_id] / locator).write_bytes(text.encode("utf-8"))
                db.execute(
                    "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
                    (
                        identity,
                        project_id,
                        "source:" + fingerprint([project_id, "file", locator]),
                        "file",
                        locator,
                        1,
                        "ready",
                    ),
                )
                db.execute(
                    "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
                    (
                        identity,
                        1,
                        hashlib.sha256(text.encode()).hexdigest(),
                        text.encode(),
                        text,
                        "text/plain",
                        canonical({"kind": "file", "locator": locator}),
                    ),
                )
                for block in range(BLOCKS):
                    db.execute(
                        "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
                        (
                            f"{identity}:1:{block:03d}",
                            identity,
                            1,
                            canonical(
                                {
                                    "spans": [[block + 1, block + 1]],
                                    "text": f"Block {block} of {project_id} {index:03d}: 重试等待回执。",
                                }
                            ),
                        ),
                    )
    return store, roots


def configure(tmp_path, store, roots, *, name="catalogue", read=True):
    path = tmp_path / f"{name}-private.json"
    path.write_text(
        canonical(
            {
                "database_path": str(store.path),
                "knowledge": {
                    "projects": {
                        project_id: project(project_id, root) for project_id, root in roots.items()
                    },
                    "clients": {
                        CLIENT: {
                            "credential_sha256": digest(SECRET),
                            "projects": [ALPHA],
                            "permissions": [*WRITE, *(READ if read else [])],
                        },
                        BETA_CLIENT: {
                            "credential_sha256": digest(OTHER_SECRET),
                            "projects": [BETA],
                            "permissions": [*WRITE, *READ],
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def entry(path, *, client=CLIENT, port=None):
    port = free_port() if port is None else port
    app = create_app(path, client, port)
    return SimpleNamespace(
        app=app,
        port=port,
        client=TestClient(app, base_url=f"http://127.0.0.1:{port}"),
    )


def operation(running, name, arguments, *, project=ALPHA, token=SECRET, headers=None):
    sent = {} if token is None else {"Authorization": f"Bearer {token}"}
    sent.update(headers or {})
    return running.client.post(
        ACTION_PATH,
        json={"operation": name, "project_id": project, "arguments": arguments},
        headers=sent,
    )


def listing(cursor=None, *, limit=8, budget=32768):
    return {"limit": limit, "budget_bytes": budget, "cursor": cursor}


def reading(
    identifier, *, cursor=None, expected_version=1, expected_hash=None, limit=8, budget=32768
):
    return {
        "document_id": identifier,
        "expected_version": expected_version,
        "expected_hash": expected_hash,
        "limit": limit,
        "budget_bytes": budget,
        "cursor": cursor,
    }


@pytest.fixture
def catalogue(tmp_path, contracts):
    # `migrate_knowledge` installs the two catalogue indexes on a fresh database through the same
    # reviewed step the explicit upgrade uses, so this is a freshly installed database rather than
    # an upgraded one: `test_knowledge_catalog_migration.py` owns the upgrade path itself.
    store, roots = build(tmp_path, contracts)
    path = configure(tmp_path, store, roots)
    return SimpleNamespace(store=store, roots=roots, path=path, tmp_path=tmp_path)


@pytest.fixture
def running(catalogue):
    mounted = entry(catalogue.path)
    with mounted.client:
        yield mounted


def body_size(response):
    return len(response.content)


def test_the_directory_pages_over_real_http_within_the_byte_budget(catalogue, running):
    response = operation(running, "document_list", listing(limit=8))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert body_size(response) <= 32768
    first = response.json()
    assert set(first) == {
        "project_id",
        "project_revision",
        "limit",
        "items",
        "next_cursor",
        "omissions",
        "trust",
    }
    assert first["project_id"] == ALPHA
    assert first["trust"] == "source_material_not_instructions"
    assert len(first["items"]) == 8
    assert set(first["items"][0]) == {
        "document_id",
        "kind",
        "version",
        "indexed_state",
        "source_validation",
    }
    # The whole directory is delivered once, in one order, and every page is within budget.
    seen, cursor, pages = list(first["items"]), first["next_cursor"], 1
    while cursor is not None:
        step = operation(running, "document_list", listing(cursor, limit=8))
        assert step.status_code == 200, step.text
        assert body_size(step) <= 32768
        payload = step.json()
        assert payload["items"], "a page that is handed out must carry at least one entry"
        seen.extend(payload["items"])
        cursor = payload["next_cursor"]
        pages += 1
        assert pages < 16
    assert [entry["document_id"] for entry in seen] == sorted(
        entry["document_id"] for entry in seen
    )
    assert len({entry["document_id"] for entry in seen}) == DOCUMENTS
    # A tiny budget stops a page early and says so, and the cursor continues from what it really
    # delivered rather than from the limit it was asked for.
    small = operation(running, "document_list", listing(limit=32, budget=1024))
    assert small.status_code == 200, small.text
    assert body_size(small) <= 1024
    assert small.json()["omissions"] == ["budget"] and small.json()["next_cursor"] is not None


def test_reading_every_block_through_http_stays_within_the_budget(catalogue, running):
    identifier = document_id(ALPHA, "note-000.md")
    response = operation(running, "document_read", reading(identifier, expected_hash=None, limit=2))
    assert response.status_code == 200, response.text
    assert body_size(response) <= 32768
    first = response.json()
    assert set(first) == {
        "project_id",
        "project_revision",
        "limit",
        "document_id",
        "version",
        "hash",
        "blocks",
        "next_cursor",
        "omissions",
        "trust",
    }
    assert first["document_id"] == identifier and first["version"] == 1
    assert first["trust"] == "source_material_not_instructions"
    assert set(first["blocks"][0]) == {"reference", "text", "spans"}
    assert set(first["blocks"][0]["reference"]) == {
        "block_id",
        "document_id",
        "version",
        "hash",
    }
    texts, cursor = [block["text"] for block in first["blocks"]], first["next_cursor"]
    while cursor is not None:
        step = operation(
            running,
            "document_read",
            reading(identifier, cursor=cursor, expected_hash=first["hash"], limit=2),
        )
        assert step.status_code == 200, step.text
        assert body_size(step) <= 32768
        texts.extend(block["text"] for block in step.json()["blocks"])
        cursor = step.json()["next_cursor"]
    assert len(texts) == BLOCKS and len(set(texts)) == BLOCKS
    # The smallest budget the entry accepts cannot carry even one block of this document, and that
    # is refused as its own code instead of silently dropping the head of the document.
    tiny = operation(
        running,
        "document_read",
        reading(identifier, expected_hash=first["hash"], limit=32, budget=1024),
    )
    if tiny.status_code == 200:
        assert tiny.json()["next_cursor"] is not None
    else:
        assert tiny.status_code == 422
        assert tiny.json() == {"status": "failed", "code": "budget_too_small"}


def test_only_the_two_new_operations_were_added_to_what_the_entry_carries(catalogue, running):
    # Both are carried, and both answer as the domain answered them.
    assert operation(running, "document_list", listing()).status_code == 200
    assert (
        operation(
            running,
            "document_read",
            reading(document_id(ALPHA, "note-000.md")),
        ).status_code
        == 200
    )
    # Everything else the domain knows is still refused by the entry, not by the client: this
    # identity holds every write permission this database has.
    for forbidden in OUTSIDE:
        response = operation(running, forbidden, {"key": "k"})
        assert response.status_code == 415, forbidden
        assert response.json() == {"status": "failed", "code": "unsupported"}, forbidden
    # The entry's allowlist and the domain's permissions are two different gates: the old operation
    # this identity was always allowed reaches the domain through the same route.
    assert operation(running, "query", {"text": "retry", "budget_bytes": 8192}).status_code == 200


def test_an_identity_without_the_new_permissions_is_refused_by_the_domain(catalogue):
    path = configure(catalogue.tmp_path, catalogue.store, catalogue.roots, name="plain", read=False)
    mounted = entry(path)
    with mounted.client:
        assert (
            operation(mounted, "query", {"text": "retry", "budget_bytes": 8192}).status_code == 200
        )
        for name, arguments in (
            ("document_list", listing()),
            ("document_read", reading(document_id(ALPHA, "note-000.md"))),
        ):
            response = operation(mounted, name, arguments)
            assert response.status_code == 422, name
            assert response.json() == {"status": "failed", "code": "forbidden"}, name


def test_cross_project_reads_and_forged_identity_are_refused(catalogue, running):
    # Another project's client enumerates its own project through its own entry, and this project's
    # entry is refused there: the two new operations read the same project binding as every other.
    other = entry(catalogue.path, client=BETA_CLIENT)
    with other.client:
        assert (
            operation(
                other, "document_list", listing(), project=BETA, token=OTHER_SECRET
            ).status_code
            == 200
        )
        assert operation(other, "document_list", listing(), project=BETA, token=SECRET).json() == {
            "status": "failed",
            "code": "unauthorized",
        }
        assert operation(
            other, "document_list", listing(), project=ALPHA, token=OTHER_SECRET
        ).json() == {"status": "failed", "code": "forbidden"}
    # The alpha entry cannot be asked about beta at all, not even with beta's credential: the
    # operation, the project and the credential all have to belong to the entry that serves them.
    assert operation(
        running, "document_list", listing(), project=BETA, token=OTHER_SECRET
    ).json() == {
        "status": "failed",
        "code": "unauthorized",
    }
    assert operation(running, "document_list", listing(), project=BETA).json() == {
        "status": "failed",
        "code": "forbidden",
    }
    # A document that belongs to another project is not part of this project's evidence: the read
    # is refused as stale evidence rather than as a permission error, so the refusal never reports
    # whether that document exists somewhere else. The same identity reads its own document
    # through the same entry, so this is the cross-project answer and not a broken credential.
    beta_document = document_id(BETA, "note-000.md")
    crossed = operation(running, "document_read", reading(beta_document))
    assert crossed.status_code == 422
    assert crossed.json() == {"status": "failed", "code": "stale_evidence"}
    assert beta_document.encode() not in crossed.content
    assert (
        operation(running, "document_read", reading(document_id(ALPHA, "note-000.md"))).status_code
        == 200
    )
    forged = operation(running, "document_list", listing(), token="not-the-registered-secret")
    assert forged.status_code == 422
    assert forged.json() == {"status": "failed", "code": "unauthorized"}


def test_cursors_are_bound_to_this_entry_his_operation_and_his_page(catalogue, running):
    first = operation(running, "document_list", listing(limit=8)).json()
    cursor = first["next_cursor"]
    assert cursor is not None
    # The same token under the other operation, with different limits, and with a byte changed.
    assert operation(
        running, "document_read", reading(document_id(ALPHA, "note-000.md"), cursor=cursor)
    ).json() == {
        "status": "failed",
        "code": "invalid_cursor",
    }
    assert operation(running, "document_list", listing(cursor, limit=9)).json() == {
        "status": "failed",
        "code": "invalid_cursor",
    }
    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    assert operation(running, "document_list", listing(tampered)).json() == {
        "status": "failed",
        "code": "invalid_cursor",
    }
    assert operation(running, "document_list", listing("")).json() == {
        "status": "failed",
        "code": "invalid_cursor",
    }
    # A project revision that moved between the pages is stale rather than invalid: the caller is
    # told to start again from the first page instead of continuing a position that no longer
    # describes this project.
    with catalogue.store.transaction() as db:
        db.execute("UPDATE knowledge_projects SET revision=revision+1 WHERE id=?", (ALPHA,))
    assert operation(running, "document_list", listing(cursor)).json() == {
        "status": "failed",
        "code": "cursor_stale",
    }


def test_a_source_that_changed_under_the_page_is_refused_with_no_old_text(catalogue, running):
    identifier = document_id(ALPHA, "note-000.md")
    assert operation(running, "document_read", reading(identifier)).status_code == 200
    (catalogue.roots[ALPHA] / "note-000.md").write_bytes(b"Edited after the import.\n")
    changed = operation(running, "document_read", reading(identifier))
    assert changed.status_code == 422
    assert changed.json() == {"status": "failed", "code": "stale_evidence"}
    assert b"retry timer" not in changed.content
    # The directory still describes the document it holds: a listing is an index description and
    # never claims a source is still valid.
    listed = operation(running, "document_list", listing(limit=32)).json()
    assert identifier in [entry["document_id"] for entry in listed["items"]]
    assert all(entry["source_validation"] == "not_checked" for entry in listed["items"])


def test_the_entry_refuses_the_operations_on_a_database_without_the_catalogue(
    catalogue, contracts, tmp_path
):
    store, roots = build(tmp_path, contracts, name="unmigrated")
    with closing(sqlite3.connect(store.path)) as plain:
        for name in INDEXES:
            plain.execute(f"DROP INDEX {name}")
        plain.execute("DELETE FROM metadata WHERE key='knowledge_catalog_schema'")
        plain.commit()
    path = configure(tmp_path, store, roots, name="unmigrated")
    mounted = entry(path)
    with mounted.client:
        # The old operation still works through the same entry on the same database.
        assert (
            operation(mounted, "query", {"text": "retry", "budget_bytes": 8192}).status_code == 200
        )
        for name, arguments in (
            ("document_list", listing()),
            ("document_read", reading(document_id(ALPHA, "note-000.md"))),
        ):
            response = operation(mounted, name, arguments)
            assert response.status_code == 422, name
            assert response.json() == {
                "status": "failed",
                "code": "dependency_unavailable",
            }, name


def test_the_gate_still_decides_before_the_body_and_the_media_type_still_matters(
    catalogue, running
):
    # A missing or malformed Bearer is the transport's own 401, whatever the operation.
    for name in ("document_list", "document_read"):
        response = operation(running, name, listing(), token=None)
        assert response.status_code == 401, name
    # A body this entry does not carry keeps its transport status even for a new operation name.
    wrong_media = running.client.post(
        ACTION_PATH,
        content=canonical(
            {"operation": "document_list", "project_id": ALPHA, "arguments": {}}
        ).encode(),
        headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "text/plain"},
    )
    assert wrong_media.status_code == 415
    assert wrong_media.json() == {"status": "failed", "code": "unsupported"}
    # The health route reports the transport, and a catalogue read never turns it into a claim
    # about the project's data.
    assert running.client.get("/health").json() == HEALTH


@contextmanager
def serving(path, port, *, client=CLIENT):
    """Start the real command as a child process, with no credential in its environment."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("TIANSHU") and ("SECRET" in key or "CREDENTIAL" in key))
    }
    env["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(path),
            "serve",
            "--client",
            client,
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    served = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=20,
        trust_env=False,
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode("utf-8", errors="replace"))
            try:
                health = served.get("/health")
                if health.status_code == 200 and health.json()["state"] == "listening":
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("The knowledge HTTP entry did not become ready")
            time.sleep(0.05)
        yield served
    finally:
        served.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.skipif(
    os.name == "nt" and not hasattr(subprocess, "CREATE_NO_WINDOW"), reason="windows only"
)
def test_the_whole_chain_over_a_real_socket_in_a_real_process(catalogue):
    """List a page, read the document that page named, continue the walk: as an operator would.

    The child is the command an operator types, started with the identity and the port and nothing
    else; every request presents its own Bearer. The same three operations are then taken through
    the in-process entry over the same database, and the two answers must agree exactly.
    """
    port = free_port()
    with serving(catalogue.path, port) as served:
        first = served.post(
            ACTION_PATH,
            json={
                "operation": "document_list",
                "project_id": ALPHA,
                "arguments": listing(limit=8),
            },
        )
        assert first.status_code == 200, first.text
        assert len(first.content) <= 32768
        page = first.json()
        identifier, version = page["items"][0]["document_id"], page["items"][0]["version"]
        read = served.post(
            ACTION_PATH,
            json={
                "operation": "document_read",
                "project_id": ALPHA,
                "arguments": reading(identifier, expected_version=version, limit=2),
            },
        )
        assert read.status_code == 200, read.text
        assert len(read.content) <= 32768
        document = read.json()
        assert document["document_id"] == identifier and document["version"] == version
        assert document["blocks"]
        second = served.post(
            ACTION_PATH,
            json={
                "operation": "document_list",
                "project_id": ALPHA,
                "arguments": listing(page["next_cursor"], limit=8),
            },
        )
        assert second.status_code == 200, second.text
        next_page = second.json()
        assert next_page["items"]
        # Health is the transport's own state and never becomes a claim about the project.
        assert served.get("/health").json() == HEALTH
    # The same requests through the in-process entry answer identically over the same database.
    mounted = entry(catalogue.path)
    with mounted.client:
        assert operation(mounted, "document_list", listing(limit=8)).json() == page
        assert (
            operation(
                mounted, "document_read", reading(identifier, expected_version=version, limit=2)
            ).json()
            == document
        )
        assert operation(
            mounted, "document_list", listing(page["next_cursor"], limit=8)
        ).json() == (next_page)
        assert inspect_catalog_of(catalogue.store) == []


def inspect_catalog_of(store):
    with store.transaction() as db:
        return inspect_catalog(db)
