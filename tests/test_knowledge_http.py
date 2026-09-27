"""Restricted HTTP entry: admission, identity, input bounds, isolation and fail-closed faults.

Every case drives the real ASGI application built by `create_app` against a real migrated
synthetic database: nothing here calls the domain directly to imitate the transport. Three
harnesses are used, in this order of fidelity:

- `drive` below speaks ASGI itself, so a test can hold a body open byte by byte and count what the
  application asked the server for. That is the only way to observe the admission rule the card
  fixes — refused *before* the body is read — rather than inferring it from timing;
- `TestClient` for complete requests over the real HTTP serialization;
- `app.state.observer`, which replaces the *call* while keeping the same body, credential,
  admission slot, error mapping and response path, for the cases that need several calls held
  inside their slots at once. Authorization, idempotency and the transaction boundary still run
  for real inside it.
"""

import ast
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from test_research_notes import (
    CREDENTIALS,
    SECRET,
    basis,
    decision,
    imported,
    note,
    unit,
    write_config,
)
from test_research_notes import (
    notes as notes,
)

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge_http import (
    ACTION_PATH,
    ALLOWED_OPERATIONS,
    MAX_ACTIVE_EXECUTES,
    MAX_BODY_BYTES,
    Refusal,
    create_app,
    transport,
)
from tianshu_memory.store import Store

ACTION = ACTION_PATH
BUDGET = {"budget_bytes": 8192}


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


class Entry:
    """One running entry plus a client bound to the exact authority it serves."""

    def __init__(self, app, port, client, *, client_name="alpha-writer"):
        self.app, self.port, self.client, self.client_name = app, port, client, client_name

    def request(self, method, path, body=None, *, token=SECRET, headers=None, raw=None, **kwargs):
        sent = {"Authorization": f"Bearer {token}"} if token is not None else {}
        if (
            raw is None
            and body is not None
            and not any(key.lower() == "content-type" for key in (headers or {}))
        ):
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        if raw is None and body is not None:
            raw = canonical(body).encode()
        return self.client.request(method, path, content=raw, headers=sent, **kwargs)

    def post(self, body=None, *, host=None, **kwargs):
        # A forged authority is sent as an explicit `Host` header: the test client puts a caller's
        # `Host` into the ASGI scope last, which is exactly where the entry reads it, so the Host
        # boundary is exercised without a socket and without bypassing a single guard.
        if host is not None:
            kwargs.setdefault("headers", {})
            kwargs["headers"] = {**kwargs["headers"], "Host": host}
        return self.request("POST", ACTION, body, **kwargs)

    def operation(self, operation, arguments, *, project="alpha", token=SECRET, **kwargs):
        return self.post(
            {"operation": operation, "project_id": project, "arguments": arguments},
            token=token,
            **kwargs,
        )


def start(notes, *, client="alpha-writer", port=None, body_timeout=None, execute_timeout=None):
    """Build and mount one entry for one fixed client identity.

    No credential is configured: the entry holds none, exactly as it ships.
    """
    port = free_port() if port is None else port
    app = create_app(
        notes.path,
        client,
        port,
        body_timeout=body_timeout,
        execute_timeout=execute_timeout,
    )
    return Entry(
        app, port, TestClient(app, base_url=f"http://127.0.0.1:{port}"), client_name=client
    )


@pytest.fixture
def entry(notes):
    running = start(notes)
    with running.client:
        yield running


def second_entry(notes, entry, *, client="beta-writer"):
    """A second entry for another identity on its own port.

    The identity is still only the process's fixed `--client`; the credential it presents comes
    from the request, as it does for every entry here.
    """
    return start(notes, client=client)


def twin_entry(notes, entry, *, body_timeout=None, execute_timeout=None):
    """A second entry for the same identity on its own port, with different phase limits.

    It serves the same client out of the same store, so the two ports behave exactly like two
    connections to one deployment while each can be given its own transport settings.
    """
    return start(
        notes,
        client=entry.client_name,
        body_timeout=body_timeout,
        execute_timeout=execute_timeout,
    )


class Driver:
    """A hand-written ASGI client: it decides exactly which body bytes arrive, and when.

    `receive` counts every call the application makes to the server for body bytes, so a test can
    assert that a refused request never asked for its body at all. A queue per request lets a test
    hold a body open mid-flight and keep the request inside its admission slot.
    """

    def __init__(self, app, entry, *, token=SECRET, headers=None):
        self.app, self.entry, self.token = app, entry, token
        self.headers = headers or {}
        self.queue = asyncio.Queue()
        self.received = 0
        self.sent = []

    def scope(self, *, host=None, content_type="application/json", path=ACTION):
        sent = [
            (b"host", (host or f"127.0.0.1:{self.entry.port}").encode()),
            (b"content-type", content_type.encode()),
        ]
        if self.token is not None:
            sent.append((b"authorization", f"Bearer {self.token}".encode()))
        for name, value in self.headers.items():
            sent.append((name.lower().encode(), value.encode()))
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": sent,
            "client": ("127.0.0.1", 51234),
            "server": ("127.0.0.1", self.entry.port),
        }

    def feed(self, chunk, *, more=True):
        self.queue.put_nowait({"type": "http.request", "body": chunk, "more_body": more})

    def close(self):
        """Hand the application a body that ended, as a complete request does."""
        self.queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    def abort(self):
        """Hand the application a peer that went away mid-body."""
        self.queue.put_nowait({"type": "http.disconnect"})

    async def receive(self):
        self.received += 1
        return await self.queue.get()

    async def send(self, message):
        self.sent.append(message)

    def response(self):
        start = next(item for item in self.sent if item["type"] == "http.response.start")
        body = b"".join(
            item.get("body", b"") for item in self.sent if item["type"] == "http.response.body"
        )
        return start["status"], json.loads(body) if body else None


async def run_driver(app, driver, scope):
    await app(scope, driver.receive, driver.send)
    return driver.response()


def drive(app, entry, body, *, token=SECRET, headers=None, host=None, timeout=30, **kwargs):
    """Send one complete body through the hand-written ASGI client."""
    driver = Driver(app, entry, token=token, headers=headers)
    if isinstance(body, bytes):
        raw = body
    elif isinstance(body, str):
        raw = body.encode()
    else:
        raw = canonical(body).encode()
    driver.feed(raw, more=False)
    return asyncio.run(
        asyncio.wait_for(run_driver(app, driver, driver.scope(host=host, **kwargs)), timeout)
    )


def counts(notes):
    with notes.store.transaction() as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("knowledge_operations", "research_notes", "knowledge_projects")
        }


def record_body(key, reference, *, summary="A recorded decision.", dedupe=None, project="alpha"):
    return {
        "operation": "note_record",
        "project_id": project,
        "arguments": {
            "key": key,
            "dedupe": dedupe or key,
            "expected_version": 0,
            "note": note([reference], decision=decision([reference], summary=summary)),
        },
    }


class Meeting:
    """A deterministic rendezvous for exactly `parties` holders of an admission slot.

    Every holder parks in the gate, so the calls are genuinely simultaneous when they are
    released — unlike a barrier, a fixed number of parties is not required to be the whole call
    count, and a test thread that forgets to arrive cannot silently strand the others.
    """

    def __init__(self, parties, timeout=30):
        self.parties, self.timeout = parties, timeout
        self.condition = threading.Condition()
        self.waiting, self.through = 0, 0

    def arrive(self, operation=lambda: None):
        with self.condition:
            self.waiting += 1
            self.condition.notify_all()
            deadline = time.monotonic() + self.timeout
            while self.through < self.parties:
                if not self.condition.wait(timeout=max(self.timeout, 1)):
                    raise AssertionError("the test never gathered every concurrent call")
                assert time.monotonic() < deadline, "the concurrent calls never met"
            return operation()

    def gather(self):
        """Block until exactly `parties` calls are parked, without entering the gate."""
        with self.condition:
            deadline = time.monotonic() + self.timeout
            while self.waiting < self.parties:
                assert self.condition.wait(timeout=max(self.timeout, 1)), (
                    "the concurrent calls never arrived"
                )
                assert time.monotonic() < deadline, "the concurrent calls never gathered"

    def release(self):
        with self.condition:
            self.through = self.parties
            self.condition.notify_all()


def observer(app, meeting=None, *, hold=None, entered=None, finished=None, failure=None):
    """A call observer that performs the real call once the test lets it through.

    It receives the same `(body, presented, application)` a production call passes, so
    authorization, idempotency and the transaction boundary still run for real: the gate changes
    when the call runs, never what it is allowed to do. `meeting` makes an exact number of calls
    run together, `entered`/`hold` hold an unknown number of calls, `failure` makes the call raise
    the way a broken dependency would, and `finished` records that the synchronous call itself
    returned rather than that a response was written.
    """

    def perform(body, presented, application):
        if failure is not None:
            raise failure
        result = application().execute(
            body, client=app.state.knowledge_client, credential=presented
        )
        if finished is not None:
            finished["finished"] = True
        return result

    def observed(body, presented, application):
        if entered is not None:
            entered.release()
        if hold is not None and not hold.is_set():
            # A one-shot latch, so only the first call is held and a later probe can time out
            # against it without waiting for a release that belongs to the first call.
            assert hold.wait(timeout=30), "the test never released the held call"
        if meeting is not None:
            return meeting.arrive(lambda: perform(body, presented, application))
        return perform(body, presented, application)

    return observed


# --------------------------------------------------------------------------- identity and input


def test_health_is_minimal_and_leaks_nothing(entry):
    response = entry.request("GET", "/health")
    assert response.status_code == 200
    assert response.json() == {
        "state": "listening",
        "entrypoint": "project_knowledge_http",
        "projects": None,
    }
    assert response.headers["cache-control"] == "no-store"
    blob = canonical(response.json())
    assert "sqlite" not in blob and "alpha" not in blob and SECRET not in blob


def test_health_answers_but_never_claims_data_is_readable(notes, tmp_path, monkeypatch):
    """Liveness only: a process whose knowledge tables were never migrated still answers
    `/health`, and every real operation then fails closed instead of reporting a false ready."""
    unmigrated = Store(tmp_path / "unmigrated.sqlite")
    root = tmp_path / "gamma"
    root.mkdir()
    (root / "source.md").write_text("Gamma source.\n", encoding="utf-8")
    config = {
        "database_path": unmigrated.path,
        "knowledge": {
            "projects": {
                "alpha": {"root": str(root), "host": "local", "default_branch": "main", "urls": []}
            },
            "clients": {
                "alpha-writer": {
                    "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
                    "projects": ["alpha"],
                    "permissions": ["query"],
                }
            },
        },
    }
    path = tmp_path / "unmigrated.json"
    path.write_text(canonical(config), encoding="utf-8")
    app = create_app(path, "alpha-writer", 18135)
    with TestClient(app, base_url="http://127.0.0.1:18135") as client:
        running = Entry(app, 18135, client)
        assert running.request("GET", "/health").json()["state"] == "listening"
        refused = running.operation("query", {"text": "gamma", **BUDGET})
    # The health route said "listening" while the real operation failed closed: the storage has no
    # knowledge tables, so the read is refused as a dependency, never answered from nothing.
    assert refused.status_code in {422, 503}
    assert refused.json()["code"] in {"project_uninitialized", "dependency_unavailable"}
    assert "gamma" not in refused.text


def test_missing_and_malformed_bearer_are_refused(entry):
    body = {"operation": "query", "project_id": "alpha", "arguments": {"text": "x", **BUDGET}}
    for headers in (
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic " + SECRET},
        {"Authorization": SECRET},
    ):
        response = entry.post(body, token=None, headers=headers)
        assert response.status_code == 401, headers
        assert response.json() == {"status": "failed", "code": "unauthorized"}
        assert response.headers["cache-control"] == "no-store"


def test_a_request_cannot_choose_its_identity(entry, notes):
    """The client comes from the process, so no body, query or header field can rebind it."""
    before = counts(notes)
    spoofed = {
        "operation": "note_query",
        "project_id": "alpha",
        "arguments": {"text": "receipt", **BUDGET},
        "client": "operator",
    }
    refused = entry.post(spoofed)
    # A body carrying a field this entry does not act on is malformed input, so it is 400 — the
    # entry's own verdict about the request text, not a domain refusal wearing the same code.
    assert refused.status_code == 400
    assert refused.json() == {"status": "failed", "code": "invalid_input"}
    without = {key: value for key, value in spoofed.items() if key != "client"}
    for path in (f"{ACTION}?client=operator", f"{ACTION}?client=operator&project_id=beta"):
        assert entry.request("POST", path, without).status_code == 422
    # A header naming another identity is simply not consulted. The identity stays the fixed one,
    # and its own project has recorded nothing yet, so the read fails closed as uninitialized.
    other = entry.operation(
        "note_query",
        {"text": "receipt", **BUDGET},
        headers={"X-Knowledge-Client": "operator", "X-Project-Id": "beta"},
    )
    assert other.status_code == 422
    assert other.json() == {"status": "failed", "code": "project_uninitialized"}
    assert counts(notes) == before


def test_reader_identity_cannot_write_notes(notes):
    """A writer credential presented under a read-only identity is a read-only identity."""
    running = start(notes, client="alpha-reader")
    imported(notes)
    reference = unit(notes)
    before = counts(notes)
    with running.client:
        refused = running.operation(
            "note_record",
            {
                "key": "reader-note",
                "dedupe": "reader-note",
                "expected_version": 0,
                "note": note([reference]),
            },
        )
    assert refused.status_code == 422
    assert refused.json() == {"status": "failed", "code": "forbidden"}
    assert counts(notes) == before


def test_operations_outside_the_allowlist_are_refused_even_with_permission(notes, entry):
    """The operator identity holds lesson, experience and import permissions and still cannot
    reach them here: the entry, not the client, decides which operations exist."""
    operator = second_entry(notes, entry, client="operator")
    with operator.client:
        for forbidden in (
            "import",
            "delete",
            "status",
            "write_state",
            "directory_scan",
            "lesson_query",
            "experience_query",
            "experience_promote",
        ):
            response = operator.operation(forbidden, {"key": "k"}, token=CREDENTIALS["operator"])
            assert response.status_code == 415, forbidden
            assert response.json() == {"status": "failed", "code": "unsupported"}


def test_the_allowlist_carries_twelve_existing_and_four_opt_in_reads(notes, entry):
    """The original twelve remain; four further reads still require a client HTTP grant."""
    assert ALLOWED_OPERATIONS == {
        "check",
        "query",
        "recover",
        "note_record",
        "note_query",
        "note_revise",
        "note_withdraw",
        "note_recover",
        "note_status",
        "note_check",
        "document_list",
        "document_read",
        "lesson_query",
        "experience_query",
        "continuation_recover",
        "continuation_check",
    }
    assert len(ALLOWED_OPERATIONS) == 16
    # A catalogue read through the entry is served on a project that was imported through the same
    # entry, so the whole route is exercised rather than a read of a fixture row.
    notes.config["knowledge"]["clients"]["alpha-writer"]["permissions"] = [
        *notes.config["knowledge"]["clients"]["alpha-writer"]["permissions"],
        "document_list",
        "document_read",
    ]
    write_config(notes)
    imported(notes)
    # The page arguments are exact: the cursor is required and explicit, so a page-1 call names it
    # as `null` rather than leaving it out.
    listed = entry.operation("document_list", {"limit": 8, "budget_bytes": 8192, "cursor": None})
    assert listed.status_code == 200, listed.text
    carried = listed.json()
    assert carried["project_id"] == "alpha"
    assert len(carried["items"]) == 1
    document = entry.operation(
        "document_read",
        {
            "document_id": carried["items"][0]["document_id"],
            "expected_version": carried["items"][0]["version"],
            "expected_hash": None,
            "limit": 8,
            "budget_bytes": 8192,
            "cursor": None,
        },
    )
    assert document.status_code == 200, document.text
    assert document.json()["blocks"], "a document read through the entry carries its blocks"
    # The block it hands out carries the same reference the existing query operation returns, so
    # the entry has not grown a second reading of the same evidence.
    assert document.json()["blocks"][0]["reference"] == unit(notes)


def test_cross_project_reads_and_writes_are_refused(entry, notes):
    imported(notes, "beta")
    imported(notes)
    before = counts(notes)
    beta_reference = unit(notes, project="beta", text="telemetry", client="beta-writer")
    read = entry.operation("query", {"text": "telemetry", **BUDGET}, project="beta")
    assert read.status_code == 422
    # The identity is refused before anything about the other project is disclosed: this client is
    # not registered for `beta`, so the refusal is the same one any other unauthorized read gets.
    assert read.json() == {"status": "failed", "code": "forbidden"}
    written = entry.operation(
        "note_record",
        {
            "key": "cross",
            "dedupe": "cross",
            "expected_version": 0,
            "note": note([beta_reference]),
        },
        project="beta",
    )
    assert written.status_code == 422
    assert written.json() == {"status": "failed", "code": "forbidden"}
    assert counts(notes) == before


def test_revoked_credential_and_revoked_permission_stop_working(entry, notes):
    imported(notes)
    reference = unit(notes)
    recorded = entry.post(record_body("revoke", reference))
    assert recorded.status_code == 200, recorded.text
    note_id_value = recorded.json()["note_id"]
    revision = {
        "operation": "note_revise",
        "project_id": "alpha",
        "arguments": {
            "key": "revoke",
            "dedupe": "revoke-2",
            "note_id": note_id_value,
            "expected_version": 1,
            "note": note([reference]),
        },
    }
    assert entry.post(revision).status_code == 200
    # Drop the credential digest: the registered principal may no longer present this secret.
    notes.config["knowledge"]["clients"]["alpha-writer"]["credential_sha256"] = "0" * 64
    notes.path.write_text(canonical(notes.config), encoding="utf-8")
    later = revision | {
        "arguments": revision["arguments"] | {"dedupe": "revoke-3", "expected_version": 2}
    }
    refused = entry.post(later)
    # The credential is presented correctly and the *domain* is what refuses it, so this is a
    # domain refusal: 422 with the domain's own code, not a transport 401.
    assert refused.status_code == 422
    assert refused.json() == {"status": "failed", "code": "unauthorized"}
    # The domain refuses the idempotent replay too, without replaying the recorded result.
    assert entry.post(revision).status_code == 422
    blocked = entry.post(record_body("blocked", reference))
    assert blocked.status_code == 422
    # A read that would still be permitted is refused as well: the credential itself is gone.
    assert entry.operation("note_query", {"text": "receipt", **BUDGET}).status_code == 422
    assert counts(notes)["research_notes"] == 1

    # Restore the credential but remove the note permissions: the same identity now reads only.
    notes.config["knowledge"]["clients"]["alpha-writer"]["credential_sha256"] = hashlib.sha256(
        SECRET.encode()
    ).hexdigest()
    notes.config["knowledge"]["clients"]["alpha-writer"]["permissions"] = ["query", "note_query"]
    notes.path.write_text(canonical(notes.config), encoding="utf-8")
    assert entry.operation("note_query", {"text": "receipt", **BUDGET}).status_code == 200
    denied = entry.post(later)
    assert denied.status_code == 422
    assert denied.json() == {"status": "failed", "code": "forbidden"}
    with notes.store.transaction() as db:
        row = db.execute(
            "SELECT version FROM research_notes WHERE id=?", (note_id_value,)
        ).fetchone()
    assert row["version"] == 2


def test_request_content_type_must_be_json(entry, notes):
    """The media type is a transport verdict, decided before the body is parsed or the domain runs.

    A recorded note is used on purpose: the request is otherwise valid, so a 415 can only come
    from the media check rather than from a domain refusal that happens to look like one. An empty
    header and an absent one are the same refusal, so only the header value varies here.
    """
    imported(notes)
    recorded = entry.post(record_body("media", unit(notes)))
    assert recorded.status_code == 200, recorded.text
    body = {
        "operation": "note_query",
        "project_id": "alpha",
        "arguments": {"text": "receipt", **BUDGET},
    }
    for content_type in (
        "",
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
        "application/json-seq",
        "application/jsonp",
    ):
        response = entry.post(body, headers={"Content-Type": content_type})
        assert response.status_code == 415, content_type
        assert response.json() == {"status": "failed", "code": "unsupported"}
    # The accepted media type, with a charset parameter, reaches the domain.
    assert (
        entry.post(body, headers={"Content-Type": "application/json; charset=utf-8"}).status_code
        == 200
    )
    # The accepted media type reaches the domain, so the guard did not reject a valid request.
    assert entry.post(body).status_code == 200


def test_non_json_and_wrongly_shaped_bodies_are_refused(entry):
    """A body this entry cannot read is refused with the transport status the card fixes for input.

    The media type already said the body is JSON, so every failure here is about the body's
    content: it is reported as input this entry will not act on — a 400 with one stable code —
    rather than leaking a parser message or a JSON pointer. The same code name is used by the
    domain for its own malformed-argument refusal; that one is a 422, because the layer is what
    decides the status, never the name.
    """
    cases = [
        b"",
        b"not json",
        b"[1,2,3]",
        b"null",
        b'"text"',
        b'{"operation":"note_query",',
        canonical({"operation": "note_query", "project_id": "alpha"}).encode(),
        canonical(
            {"operation": "note_query", "project_id": "alpha", "arguments": {}, "extra": 1}
        ).encode(),
        canonical({"operation": 7, "project_id": "alpha", "arguments": {}}).encode(),
        canonical({"operation": "note_query", "project_id": "", "arguments": {}}).encode(),
        canonical({"operation": "note_query", "project_id": "a" * 129, "arguments": {}}).encode(),
        canonical({"operation": "note_query", "project_id": "alpha", "arguments": []}).encode(),
        b'{"operation":"note_query","operation":"query","project_id":"alpha","arguments":{}}',
        b'{"operation":"note_query","project_id":"alpha","arguments":{"text":NaN}}',
        b"\xff\xfe\x00\x01",
    ]
    for raw in cases:
        response = entry.post(raw=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 400, raw
        assert response.json() == {"status": "failed", "code": "invalid_input"}
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("raw_length", [MAX_BODY_BYTES + 1, 400000])
def test_oversized_bodies_are_refused_by_the_bytes_that_arrive(entry, raw_length):
    raw = (
        b'{"operation":"note_query","project_id":"alpha","arguments":{"pad":"'
        + b" " * raw_length
        + b'"}}'
    )
    response = entry.post(raw=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"status": "failed", "code": "request_too_large"}


def test_a_false_content_length_cannot_smuggle_an_oversized_body(entry):
    """`Content-Length` is never believed: the count is made on the bytes that arrive."""
    raw = (
        b'{"operation":"note_query","project_id":"alpha","arguments":{"pad":"'
        + b" " * (MAX_BODY_BYTES + 1)
        + b'"}}'
    )
    response = entry.post(
        raw=raw, headers={"Content-Type": "application/json", "Content-Length": "10"}
    )
    assert response.status_code == 413
    assert response.json() == {"status": "failed", "code": "request_too_large"}


def test_the_same_code_name_is_reported_by_the_layer_that_raised_it(entry, notes):
    """`invalid_input` and `request_too_large` exist in both vocabularies, and neither is guessed.

    The wire status must say which layer refused the request. This entry's own verdict on a
    malformed body is a 400, the domain's refusal of a malformed argument is a 422, and the two
    are distinguished at the boundary where the domain raises — never by the name of the code.
    """
    imported(notes)
    # This entry's own verdicts on the request text.
    text = drive(entry.app, entry, b"not json")
    assert text == (400, {"status": "failed", "code": "invalid_input"})
    unsupported = drive(
        entry.app, entry, canonical({"operation": "import", "project_id": "alpha", "arguments": {}})
    )
    assert unsupported == (415, {"status": "failed", "code": "unsupported"})
    assert drive(entry.app, entry, b"x" * (MAX_BODY_BYTES + 1)) == (
        413,
        {"status": "failed", "code": "request_too_large"},
    )
    # The domain's refusal under the very same code name, reached through the real execute.
    domain = entry.operation("query", {"text": "receipt", "budget_bytes": 255})
    assert domain.status_code == 422
    assert domain.json() == {"status": "failed", "code": "invalid_input"}
    # Both came from the same reachable operation with the same code name, and the status differs
    # only because the layer differs.
    assert (text[0], domain.status_code) == (400, 422)
    # The two vocabularies are distinct types, so no code name can be mistaken for a layer: the
    # domain's refusal is marked where it leaves `execute`, and this entry's own verdict is not.
    assert isinstance(Refusal("invalid_input"), Fault)
    assert Refusal("invalid_input").status == 422
    assert isinstance(transport("invalid_input", 400), Fault)
    assert not isinstance(transport("invalid_input", 400), Refusal)


def test_a_domain_refusal_keeps_its_own_code_and_never_becomes_a_transport_verdict(entry, notes):
    """Codes the transport never produces itself still arrive as 422 with the domain's name.

    `stale_evidence` and `version_conflict` exist only in the domain's vocabulary. Neither is a
    transport verdict and neither gets a transport status, so the mapping is proven to be about
    the raising layer rather than about a list of known code names.
    """
    imported(notes)
    reference = unit(notes)
    recorded = entry.post(record_body("layer-version", reference))
    assert recorded.status_code == 200, recorded.text
    # A revision that expects a version the note never had: the domain's own conflict.
    conflict = entry.operation(
        "note_revise",
        {
            "key": "layer-version",
            "dedupe": "layer-version-2",
            "note_id": recorded.json()["note_id"],
            "expected_version": 2,
            "note": note([reference], inferences=["A competing inference."]),
        },
    )
    assert conflict.status_code == 422
    assert conflict.json() == {"status": "failed", "code": "version_conflict"}
    # A reference captured while the file was current becomes stale once the file is rewritten.
    (notes.roots["alpha"] / "source.md").write_text("Rewritten source.\n", encoding="utf-8")
    stale = entry.operation(
        "note_record",
        {
            "key": "layer-stale",
            "dedupe": "layer-stale",
            "expected_version": 0,
            "note": note([reference], decision=decision([reference])),
        },
    )
    assert stale.status_code == 422
    assert stale.json() == {"status": "failed", "code": "stale_evidence"}


def test_invalid_operation_name_is_refused_before_the_domain(entry, notes):
    before = counts(notes)
    response = entry.operation("note_query ", {"text": "x", **BUDGET})
    assert response.status_code == 415
    assert counts(notes) == before


# --------------------------------------------------------------------- host, origin and surface


def test_non_loopback_host_is_refused(entry):
    body = {"operation": "note_query", "project_id": "alpha", "arguments": {"text": "x", **BUDGET}}
    for host in (
        "example.invalid",
        "127.0.0.1",
        f"127.0.0.1:{entry.port + 1}",
        "0.0.0.0",
        "[::1]",
        "localhost.evil.invalid",
        "localhost",
        "",
    ):
        response = entry.post(body, host=host)
        assert response.status_code == 400, host
        assert response.json() == {"status": "failed", "code": "invalid_host"}
    # The exact authority this process was told to serve is accepted under either loopback name.
    for host in (f"127.0.0.1:{entry.port}", f"localhost:{entry.port}"):
        response = entry.post(body, host=host)
        assert response.status_code in {200, 422}, (host, response.text)
        assert response.json().get("code") != "invalid_host"


def test_health_also_checks_the_authority(entry):
    assert entry.request("GET", "/health", headers={"Host": "example.invalid"}).status_code == 400


def test_browser_origin_requests_are_refused_and_no_cors_surface_exists(entry):
    body = {"operation": "note_query", "project_id": "alpha", "arguments": {"text": "x", **BUDGET}}
    for headers in (
        {"Origin": "https://example.invalid"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = entry.post(body, headers=headers)
        assert response.status_code == 400
        assert response.json() == {"status": "failed", "code": "browser_origin_refused"}
        assert "access-control-allow-origin" not in response.headers
    preflight = entry.request(
        "OPTIONS",
        ACTION,
        headers={
            "Origin": "https://example.invalid",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code in {400, 405}
    assert "access-control-allow-origin" not in preflight.headers


def test_openapi_docs_and_unknown_routes_are_absent(entry):
    for path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect", "/"):
        assert entry.request("GET", path).status_code == 404, path
    for response in (
        entry.request("GET", ACTION),
        entry.request("POST", "/local/v1/project-knowledge/other"),
        entry.request("POST", f"{ACTION}/extra"),
    ):
        assert response.status_code in {404, 405}
        assert response.json()["status"] == "failed"
        assert response.json()["code"] in {"not_found", "method_not_allowed"}


def test_no_proxy_forwarding_header_is_trusted(entry):
    body = {"operation": "note_query", "project_id": "alpha", "arguments": {"text": "x", **BUDGET}}
    response = entry.post(
        body,
        host="public.example.invalid",
        headers={
            "X-Forwarded-Host": f"127.0.0.1:{entry.port}",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_host"


def test_database_failure_is_reported_as_unavailable(entry, notes):
    """A storage the domain cannot open fails closed with a code, never with a traceback."""
    notes.config["database_path"] = ""
    notes.path.write_text(canonical(notes.config), encoding="utf-8")
    response = entry.operation("note_query", {"text": "receipt", **BUDGET})
    assert response.status_code == 503
    assert response.json() == {"status": "failed", "code": "dependency_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert "Traceback" not in response.text and "sqlite" not in response.text


def test_the_process_refuses_to_start_without_a_usable_configuration(notes):
    with pytest.raises(Fault, match="invalid_configuration"):
        create_app(notes.tmp_path / "absent.json", "alpha-writer", 18135)
    broken = notes.tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(Fault, match="invalid_configuration"):
        create_app(broken, "alpha-writer", 18135)
    with pytest.raises(Fault, match="unregistered_client"):
        create_app(notes.path, "not-registered", 18135)
    for port in (0, 70000, "18135", None):
        with pytest.raises(Fault, match="invalid_configuration"):
            create_app(notes.path, "alpha-writer", port)


def test_the_entry_configures_no_credential_of_its_own(notes, monkeypatch):
    """Nothing about the entry depends on an environment variable.

    The credential is the one each request presents. A process with the client's secret removed
    from the environment — as a deployment that only ever receives Bearer values would be — builds
    the same application and answers a correct Bearer, because the factory reads no secret at all.
    """
    environment = [
        name
        for name in os.environ
        if "TIANSHU" in name and ("SECRET" in name or "CREDENTIAL" in name or "TOKEN" in name)
    ]
    for name in environment:
        monkeypatch.delenv(name, raising=False)
    running = start(notes)
    with running.client:
        assert running.app.state.active == 0
        assert not hasattr(running.app.state, "credential")
        imported(notes)
        body = {
            "operation": "note_query",
            "project_id": "alpha",
            "arguments": {"text": "receipt", **BUDGET},
        }
        # A correct Bearer works and a missing one is refused by the transport, with no secret
        # anywhere in the process environment.
        assert running.post(body).status_code == 200
        assert running.post(body, token=None).status_code == 401


# ------------------------------------------------------------------- concurrency and saturation


async def _wait(condition, *, timeout=30):
    """Wait for a test condition instead of a fixed sleep, so the assertions are about state."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the entry never reached the expected state"
        await asyncio.sleep(0.005)


async def start_driver(app, entry, *, token=SECRET, headers=None):
    """Begin one request that keeps its body open, and return its driver and its task."""
    driver = Driver(app, entry, token=token, headers=headers)
    return driver, asyncio.ensure_future(run_driver(app, driver, driver.scope()))


async def probe(app, entry, body, *, token=SECRET, headers=None, timeout=5):
    """One complete request that must be answered without ever being asked for its body."""
    driver = Driver(app, entry, token=token, headers=headers)
    driver.feed(body, more=False)
    status, payload = await asyncio.wait_for(
        run_driver(app, driver, driver.scope()), timeout=timeout
    )
    return status, payload, driver.received


async def settle(tasks, *, timeout=30):
    """Finish requests whose bodies were held open, and collect what they answered."""
    return [await asyncio.wait_for(task, timeout) for task in tasks]


def test_no_slow_body_and_no_slow_call_can_hold_a_fifth_slot(entry, notes):
    """The bound covers the body read and the synchronous call, and refusal precedes the body.

    The card's rule is observed directly: with every slot taken, the next request is answered 503
    without the application asking the server for a single body byte (`received == 0`) and without
    reaching `execute` (`executed` unchanged). Both ways of occupying the bound are exercised —
    four bodies still arriving, and four calls already running — and the guard proves the same
    guarantee in both. Releasing the holders restores the entry, so nothing leaked.
    """
    imported(notes)
    app = entry.app
    body = canonical(
        {"operation": "query", "project_id": "alpha", "arguments": {"text": "receipt", **BUDGET}}
    ).encode()

    async def scenario():
        # Phase one: four slow bodies are inside their slots, none of them has finished reading.
        holders = [await start_driver(app, entry) for _ in range(MAX_ACTIVE_EXECUTES)]
        for driver, _ in holders:
            driver.feed(body[:16])
        await _wait(lambda: app.state.active == MAX_ACTIVE_EXECUTES)
        assert app.state.active == MAX_ACTIVE_EXECUTES
        # Every holder is parked mid-body: it asked for more bytes and is waiting for them.
        assert all(driver.received >= 2 for driver, _ in holders)
        refused = await probe(app, entry, body)
        assert refused == (503, {"status": "failed", "code": "overloaded"}, 0)
        assert app.state.active == MAX_ACTIVE_EXECUTES
        # The refused request was never read, so no half-parsed body was left anywhere.
        for driver, _ in holders:
            driver.feed(body[16:], more=False)
        answers = await settle([task for _, task in holders])
        assert [status for status, _ in answers] == [200] * MAX_ACTIVE_EXECUTES
        assert all(payload["blocks"] for _, payload in answers)
        assert app.state.active == 0

        # Phase two: the same bound, occupied by four synchronous calls instead of four bodies.
        executed = []
        release = threading.Event()
        entered = threading.Semaphore(0)
        perform = observer(app, hold=release, entered=entered)

        def counting(body, presented, application):
            executed.append(1)
            return perform(body, presented, application)

        app.state.observer = counting
        running = [asyncio.ensure_future(probe(app, entry, body, timeout=60)) for _ in range(4)]
        # The acquire happens off the loop: the calls that release it run on the same loop.
        for _ in range(MAX_ACTIVE_EXECUTES):
            assert await asyncio.to_thread(entered.acquire, True, 30)
        await _wait(lambda: len(executed) == MAX_ACTIVE_EXECUTES)
        assert app.state.active == MAX_ACTIVE_EXECUTES
        refused = await probe(app, entry, body)
        assert refused == (503, {"status": "failed", "code": "overloaded"}, 0)
        # The guard neither read a body nor reached the synchronous call.
        assert len(executed) == MAX_ACTIVE_EXECUTES
        release.set()
        finished = await asyncio.wait_for(asyncio.gather(*running), timeout=60)
        assert [status for status, _, _ in finished] == [200] * 4
        await _wait(lambda: app.state.active == 0)
        app.state.observer = None

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    # Nothing leaked: the entry serves a normal request again.
    normal = entry.post(
        {"operation": "query", "project_id": "alpha", "arguments": {"text": "receipt", **BUDGET}}
    )
    assert normal.status_code == 200
    assert app.state.active == 0


def test_a_call_that_outlives_the_wait_keeps_its_slot_and_its_write(notes):
    """A call that outlives the *execute* wait keeps its slot and still finishes its own write.

    Both the deadline path and the slot release point are the production ones; only the probing
    entry's execute limit is shortened, so the assertions are about the transport's behaviour
    rather than about how long a database write happens to take. The 408 is the wait ending, never
    a claim that the operation was cancelled.
    """
    imported(notes)
    reference = unit(notes)
    # The first call is held by the test rather than by the clock, so its entry keeps the
    # production limits. A second entry on the same store and the same client carries a short
    # execute limit, and its probe arrives while the first call is still held.
    entry = start(notes)
    impatient = twin_entry(notes, entry, execute_timeout=0.5)
    release = threading.Event()
    entered = threading.Semaphore(0)
    done_flag = {"finished": False}
    entry.app.state.observer = observer(
        entry.app, hold=release, entered=entered, finished=done_flag
    )
    impatient.app.state.observer = observer(impatient.app, hold=release)
    body = record_body("slow", reference)
    with entry.client, impatient.client, ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(entry.post, body)
        assert entered.acquire(timeout=30)
        timed_out = impatient.post(body)
        assert timed_out.status_code == 408
        assert timed_out.json() == {"status": "failed", "code": "request_timeout"}
        # The response said nothing about cancellation, and the call keeps its slot: it had not
        # committed yet, and the slot is still counted as busy while it runs. The held entry has
        # exactly the one call in it, and that call is still running.
        with notes.store.transaction() as db:
            written = db.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0]
        assert written == 0
        assert entry.app.state.active == 1
        # The entry whose *wait* expired has already given its slot back: the wait ended, but its
        # call is the same held call the first entry is running, so the count there is one and the
        # work is accounted for once.
        assert impatient.app.state.active == 1
        release.set()
        done = running.result(timeout=30)
        assert done_flag["finished"] is True
    assert done.status_code == 200
    assert done.json()["status"] == "recorded"
    # Both entries released the slot when the call finished, not when a response was written.
    assert entry.app.state.active == 0
    assert impatient.app.state.active == 0
    # The caller replays the same idempotent request and gets the recorded result, not a rewrite.
    replay = entry.post(body)
    assert entry.app.state.active == 0
    assert replay.status_code == 200
    assert replay.json()["version"] == done.json()["version"]
    assert replay.json()["replayed"] is True


def test_a_slow_body_expires_on_its_own_phase_and_gives_its_slot_back(notes):
    """The read phase has its own 10 second bound, and a body that never finishes ends there.

    The body is held open by the test rather than by the clock: the application is genuinely
    parked waiting for bytes it will never receive, and only the read limit can end it. Its slot
    is given back because no synchronous call was ever started, so a slow body cannot consume the
    bound forever.
    """
    imported(notes)
    entry = start(notes, body_timeout=0.5)
    request = {
        "operation": "query",
        "project_id": "alpha",
        "arguments": {"text": "receipt", **BUDGET},
    }
    body = canonical(request).encode()

    async def scenario():
        driver, task = await start_driver(entry.app, entry)
        driver.feed(body[:16])
        await _wait(lambda: entry.app.state.active == 1)
        status, payload = await asyncio.wait_for(task, timeout=30)
        assert status == 408
        assert payload == {"status": "failed", "code": "request_timeout"}
        # The body was never completed and no call was made, so the slot is free again.
        await _wait(lambda: entry.app.state.active == 0)
        assert entry.app.state.active == 0

    with entry.client:
        asyncio.run(asyncio.wait_for(scenario(), timeout=60))
        assert entry.post(request).status_code == 200
        assert entry.app.state.active == 0


def test_an_abandoned_body_gives_its_slot_back_and_the_entry_recovers(notes):
    """A peer that disappears mid-body releases its slot, and the entry keeps serving.

    A disconnect is not a completed request and not a domain refusal: it is reported as malformed
    input, and — because no synchronous call had started — the slot goes back at once. The next
    request is served normally, so nothing was leaked by the abandoned one.
    """
    imported(notes)
    entry = start(notes)
    request = {
        "operation": "query",
        "project_id": "alpha",
        "arguments": {"text": "receipt", **BUDGET},
    }
    body = canonical(request).encode()

    async def scenario():
        driver, task = await start_driver(entry.app, entry)
        driver.feed(body[:16])
        await _wait(lambda: entry.app.state.active == 1)
        driver.abort()
        try:
            status, payload = await asyncio.wait_for(task, timeout=30)
        except ClientDisconnect:
            # The application propagated the peer's disappearance, exactly as a real server would
            # see it; what matters is that the slot came back and nothing was left running.
            status, payload = None, None
        if status is not None:
            assert status == 400
            assert payload == {"status": "failed", "code": "invalid_input"}
        await _wait(lambda: entry.app.state.active == 0)
        assert entry.app.state.active == 0

    with entry.client:
        asyncio.run(asyncio.wait_for(scenario(), timeout=60))
        assert entry.post(request).status_code == 200
        assert entry.app.state.active == 0


def test_a_cancelled_request_never_leaks_its_slot(notes):
    """A request cancelled while its body is still arriving gives its slot back.

    A server cancels a request frame when the connection goes away, and that cancellation is not an
    exception this module's own code ever sees. The slot is still released, because the ownership
    is one lease with one release rather than a decrement per code path. Twenty cancelled requests
    are issued in a row, so a single leak would leave the entry saturated and make the following
    real request fail.
    """
    imported(notes)
    entry = start(notes)
    request = {
        "operation": "query",
        "project_id": "alpha",
        "arguments": {"text": "receipt", **BUDGET},
    }
    body = canonical(request).encode()

    async def scenario():
        for _ in range(20):
            driver, task = await start_driver(entry.app, entry)
            driver.feed(body[:16])
            await _wait(lambda: entry.app.state.active == 1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await _wait(lambda: entry.app.state.active == 0)
        assert entry.app.state.active == 0

    with entry.client:
        asyncio.run(asyncio.wait_for(scenario(), timeout=60))
        assert entry.app.state.active == 0
        # Nothing accumulated: the entry is not saturated and serves a real request.
        assert entry.post(request).status_code == 200


def test_a_call_that_raises_frees_its_slot_exactly_once_and_the_entry_recovers(notes):
    """A failing call is reported as a dependency outage, and its slot is released once.

    `dependency_unavailable` is the entry's verdict about the *dependency*, and it is still the
    entry's own status. The counter is asserted against the bound rather than a number, so a
    double release — which would make the entry admit more than four requests — cannot pass.
    """
    imported(notes)
    entry = start(notes)
    body = {"operation": "query", "project_id": "alpha", "arguments": {"text": "receipt", **BUDGET}}
    entry.app.state.observer = observer(
        entry.app, failure=sqlite3.OperationalError("no such table: knowledge_versions")
    )
    with entry.client:
        for _ in range(6):
            response = entry.post(body)
            assert response.status_code == 503
            assert response.json() == {"status": "failed", "code": "dependency_unavailable"}
            assert 0 <= entry.app.state.active < MAX_ACTIVE_EXECUTES
        assert entry.app.state.active == 0
        # The entry is intact: the same request succeeds once the dependency works again.
        entry.app.state.observer = None
        assert entry.post(body).status_code == 200
        assert entry.app.state.active == 0


def test_the_slot_is_released_when_the_synchronous_call_really_returns(notes):
    """The release point is the call returning, and a request can use the slot again afterwards.

    The entry's own settlement seam reports the exact moment the production call returned; the
    slot must still be held just before it and free just after, so a caller that answers 200 has
    already given its slot back rather than holding it until the response is flushed.
    """
    imported(notes)
    entry = start(notes)
    seen = []

    def settle():
        seen.append(entry.app.state.active)

    entry.app.state.settle = settle
    body = {"operation": "query", "project_id": "alpha", "arguments": {"text": "receipt", **BUDGET}}
    with entry.client:
        assert entry.post(body).status_code == 200
    # At the instant the call returned the slot was still held by this very request, and it was
    # released immediately afterwards — not left to a later response write.
    assert seen == [1]
    assert entry.app.state.active == 0


def test_alpha_and_beta_requests_do_not_cross_projects_or_identities(entry, notes):
    """Interleaved requests keep their own project, client and domain family.

    Four requests — two projects, two domain families — are held inside their admission slots and
    released together, so the bodies really are in flight at the same time. Each project is read
    with its own registered client, through its own entry.
    """
    imported(notes)
    imported(notes, "beta")
    beta = second_entry(notes, entry)
    meeting = Meeting(4)
    entry.app.state.observer = observer(entry.app, meeting)
    beta.app.state.observer = observer(beta.app, meeting)
    calls = [
        (entry, "query", "alpha", "receipt", SECRET),
        (entry, "note_query", "alpha", "receipt", SECRET),
        (beta, "query", "beta", "telemetry", CREDENTIALS["beta-writer"]),
        (beta, "note_query", "beta", "telemetry", CREDENTIALS["beta-writer"]),
    ]
    with beta.client, ThreadPoolExecutor(max_workers=4) as pool:
        running = [
            pool.submit(
                target.operation,
                name,
                {"text": text, **BUDGET},
                project=project,
                token=token,
            )
            for target, name, project, text, token in calls
        ]
        meeting.gather()
        meeting.release()
        finished = [item.result(timeout=60) for item in running]
    assert [item.status_code for item in finished] == [200, 200, 200, 200], [
        item.text for item in finished
    ]
    assert all("retry" in canonical(block["text"]) for block in finished[0].json()["blocks"])
    assert all("telemetry" in canonical(block["text"]) for block in finished[2].json()["blocks"])
    assert finished[1].json()["notes"] == []
    assert finished[3].json()["notes"] == []


def test_same_key_different_content_admits_one_semantic_result(entry, notes):
    """Two concurrent writes under one key: one is recorded, the other conflicts.

    The key is bound before any side effect, so the loser cannot arrive first and be overwritten;
    both requests are held in their slots and released together, so they really race.
    """
    imported(notes)
    reference = unit(notes)
    meeting = Meeting(2)
    entry.app.state.observer = observer(entry.app, meeting)

    def write(summary):
        return entry.post(record_body("race", reference, summary=summary))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "Decision one.")
        second = pool.submit(write, "Decision two.")
        meeting.gather()
        meeting.release()
        results = [first.result(timeout=60), second.result(timeout=60)]
    assert sorted(item.status_code for item in results) == [200, 422], [
        item.text for item in results
    ]
    winner = next(item for item in results if item.status_code == 200)
    loser = next(item for item in results if item.status_code != 200)
    assert loser.json() == {"status": "failed", "code": "idempotency_conflict"}
    with notes.store.transaction() as db:
        rows = db.execute(
            "SELECT version, payload FROM research_notes WHERE id=?",
            (winner.json()["note_id"],),
        ).fetchall()
    # Exactly one semantic outcome exists for the key.
    assert [row["version"] for row in rows] == [1]


def test_stale_expected_version_conflicts_without_writing(entry, notes):
    imported(notes)
    reference = unit(notes)
    recorded = entry.post(record_body("stale", reference))
    assert recorded.status_code == 200
    note_id_value = recorded.json()["note_id"]
    # The note is at version 1, so a revision that expects version 2 is stale by one whole version.
    stale = entry.operation(
        "note_revise",
        {
            "key": "stale",
            "dedupe": "stale-2",
            "note_id": note_id_value,
            "expected_version": 2,
            "note": note([reference], inferences=["A competing inference."]),
        },
    )
    assert stale.status_code == 422
    assert stale.json() == {"status": "failed", "code": "version_conflict"}
    with notes.store.transaction() as db:
        row = db.execute(
            "SELECT version FROM research_notes WHERE id=?", (note_id_value,)
        ).fetchone()
    assert row["version"] == 1


def test_transport_refusals_leave_no_write_behind(entry, notes):
    """Every transport-level refusal is decided before the domain sees a body."""
    imported(notes)
    before = counts(notes)
    entry.operation("note_query", {"text": "receipt", **BUDGET}, token="")
    entry.operation("import", {"key": "x"})
    entry.post({"operation": "note_query", "project_id": "alpha", "arguments": {}}, token="")
    entry.post(raw=b"x" * 300000, headers={"Content-Type": "application/json"})
    entry.post(
        {"operation": "note_query", "project_id": "alpha", "arguments": {"text": "x", **BUDGET}},
        headers={"Origin": "https://example.invalid"},
    )
    entry.post(
        {"operation": "note_query", "project_id": "alpha", "arguments": {"text": "x", **BUDGET}},
        host="example.invalid",
    )
    # Only the import that was refused before the domain ran, and reads, which write nothing.
    assert counts(notes) == before


# ------------------------------------------------------- domain regressions through the transport


def test_domain_bounds_and_errors_come_back_unchanged(entry, notes):
    """The entry never relaxes a domain bound: the byte budget still truncates, stale evidence
    still conflicts, and the code is the domain's own."""
    imported(notes)
    reference = unit(notes)
    # The same budget argument the domain accepts in the CLI: one byte under its floor is refused
    # with the domain's own code, and the refusal reaches the wire in the one envelope so a caller
    # never confuses it with a malformed request.
    refused = entry.operation("query", {"text": "receipt", "budget_bytes": 255})
    assert refused.status_code == 422
    assert refused.json() == {"status": "failed", "code": "invalid_input"}
    tiny = entry.operation("query", {"text": "receipt", "budget_bytes": 256})
    assert tiny.status_code == 200
    assert tiny.json()["blocks"] == []
    assert "budget" in tiny.json()["omissions"]
    # A reference captured while the file was current becomes stale once the file is rewritten,
    # and the domain refuses the write with its own evidence code.
    (notes.roots["alpha"] / "source.md").write_text("Rewritten source.\n", encoding="utf-8")
    stale = entry.operation(
        "note_record",
        {
            "key": "stale-evidence",
            "dedupe": "stale-evidence",
            "expected_version": 0,
            "note": note([reference], decision=decision([reference])),
        },
    )
    assert stale.status_code == 422
    assert stale.json() == {"status": "failed", "code": "stale_evidence"}


def test_citation_graph_rules_survive_the_transport(entry, notes):
    """The transport adds no graph rule of its own: a shared ancestor is not a cycle, and citing a
    version that is no longer current is refused with the domain's own code.

    This is the citation regression the domain owns, reached through a full round trip rather than
    by calling the domain directly, so a transport that quietly re-serialized or re-ordered a
    decision basis would show up here.
    """
    imported(notes)
    reference = unit(notes)

    def write(key, payload, dedupe=None):
        return entry.operation(
            "note_record",
            {
                "key": key,
                "dedupe": dedupe or key,
                "expected_version": 0,
                "note": note([reference], **payload),
            },
        )

    ancestor = write("graph-a", {"decision": decision([reference])})
    assert ancestor.status_code == 200, ancestor.text
    # Left and right both rest on the same ancestor: a diamond, not a ring.
    left = write("graph-b", {"decision": decision([], basis=basis(ancestor.json()))})
    right = write("graph-c", {"decision": decision([], basis=basis(ancestor.json()))})
    assert [left.status_code, right.status_code] == [200, 200], [left.text, right.text]
    combined = write(
        "graph-d",
        {"decision": decision([], basis=basis(left.json(), right.json()))},
    )
    assert combined.status_code == 200, combined.text
    assert combined.json()["cited_notes"] == 2
    status = entry.operation("note_status", {"note_id": combined.json()["note_id"], "version": 1})
    assert status.status_code == 200
    assert [item["kind"] for item in status.json()["citations"]] == [
        "source",
        "note",
        "note",
    ]
    # A version that has since been revised is no longer a citable version of that note.
    revised = entry.operation(
        "note_revise",
        {
            "key": "graph-a",
            "dedupe": "graph-a-revise",
            "note_id": ancestor.json()["note_id"],
            "expected_version": 1,
            "note": note([reference], inferences=["A revised inference."]),
        },
    )
    assert revised.status_code == 200, revised.text
    stale = write(
        "graph-e",
        {"decision": decision([], basis=basis(ancestor.json()))},
    )
    assert stale.status_code == 422
    assert stale.json() == {"status": "failed", "code": "stale_evidence"}


def test_http_agrees_with_the_existing_action_entrypoint(entry, notes):
    """The same operation through HTTP and through the CLI action file returns the same body.

    The CLI still takes its credential from an environment variable of the operator's choosing —
    that entrypoint is untouched — while this entry takes the same value from the request's
    `Authorization: Bearer`. Both hand it to the very same `execute`.
    """
    imported(notes)
    reference = unit(notes)
    body = record_body("parity", reference)
    over_http = entry.post(body)
    assert over_http.status_code == 200, over_http.text
    # The CLI runs the very same request afterwards, so its answer is the recorded replay of the
    # HTTP write: identical apart from the flag that says it was replayed.
    request_path = notes.tmp_path / "parity.json"
    request_path.write_text(canonical(body), encoding="utf-8")
    credential_env = "TIANSHU_PARITY_CREDENTIAL"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianshu_memory.knowledge_cli",
            "--config",
            str(notes.path),
            "action",
            "--client",
            "alpha-writer",
            "--credential-env",
            credential_env,
            str(request_path),
        ],
        cwd=notes.roots["alpha"],
        env=dict(os.environ, PYTHONUTF8="1", **{credential_env: SECRET}),
        capture_output=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    from_cli = json.loads(process.stdout)
    assert from_cli == {**over_http.json(), "replayed": True}
    # The replay really did come from the domain's own ledger, not from a second write.
    assert from_cli["version"] == 1 and from_cli["status"] == "recorded"


def test_the_serve_command_requires_only_a_client_and_a_port():
    """`serve` takes exactly the card's two arguments and no credential option of its own."""
    process = subprocess.run(
        [sys.executable, "-m", "tianshu_memory.knowledge_cli", "serve", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    assert "--client" in process.stdout and "--port" in process.stdout
    assert "--credential-env" not in process.stdout
    # And it will not start without them: the port has no default at all.
    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianshu_memory.knowledge_cli",
            "--config",
            "unused.json",
            "serve",
            "--client",
            "alpha-writer",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert missing.returncode == 2
    assert "--port" in missing.stderr


def test_http_module_calls_only_the_public_execute_and_holds_no_domain_state():
    """AST review of the transport: one public call, no SQL, no reverse domain import."""
    source = Path("src/tianshu_memory/knowledge_http.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    # Two standard-library additions are allowed for the authority rule: `ipaddress` and `re` are
    # what let a presented `Host` be judged by the interface it names rather than by its spelling,
    # with the same parser the binding validates its configured names with. Nothing else about this
    # list changes: no diagnostics, no runtime, no domain state.
    assert imported <= {
        "asyncio",
        "ipaddress",
        "re",
        "sqlite3",
        "pathlib",
        "fastapi",
        "starlette",
        "domain",
        "knowledge",
    }, imported
    attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    # `execute` is the only call the transport makes on an application object — `application()` is
    # the per-request factory — and no storage call appears anywhere.
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "execute" in called
    assert not {"cursor", "executemany", "fetchone", "fetchall", "commit", "connect"} & (
        attributes | called
    )
    lowered = source.lower()
    for keyword in (
        "select ",
        "insert ",
        "update ",
        "delete from",
        "pragma ",
        "knowledge_versions",
    ):
        assert keyword not in lowered, keyword
    # The instance is built per request inside this module and never stored on the application.
    assert "KnowledgeApplication(" in source
    assert "app.state.application" not in source
    # No credential of the entry's own, and no environment variable read anywhere in it: the
    # presented Bearer is the only credential, exactly as the card fixes.
    assert "os.environ" not in source and "getenv" not in source
    assert "credential=" in source  # ... only as the argument passed to the domain's execute
    # No domain module imports the transport back.
    for name in ("knowledge.py", "research_notes.py", "lessons.py", "store.py"):
        assert "knowledge_http" not in Path(f"src/tianshu_memory/{name}").read_text(
            encoding="utf-8"
        ), name
