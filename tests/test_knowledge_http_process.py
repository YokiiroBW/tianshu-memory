"""The restricted entry as a real loopback process: start, serve, restart.

This file starts the actual `knowledge_cli serve` command as a child process on an explicit free
port and talks to it over a real socket, so the wiring under test is the wiring an operator would
run: argparse, uvicorn's binding, the ASGI application and the SQLite file on disk. Nothing here
is simulated by an in-process test double, and every operation is compared against the same
operation through the existing `action` CLI.

No secret is placed in the child's environment. The only credential this entry ever uses is the
`Authorization: Bearer` value a request presents, so the child is started exactly as the card's
command line says — `--client` and `--port` — and the tests prove that a correct Bearer is served,
a wrong one is refused by the domain, and a missing one is refused by the transport.
"""

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from test_research_notes import (
    OTHER_SECRET,
    SECRET,
    decision,
    imported,
    note,
    unit,
)
from test_research_notes import (
    notes as notes,
)

from tianshu_memory.domain import canonical

CLIENT = "alpha-writer"
BETA_CLIENT = "beta-writer"
COMMAND = ["-m", "tianshu_memory.knowledge_cli"]
ACTION = "/local/v1/project-knowledge/action"
READY_STATE = "listening"
REPO_ROOT = Path(__file__).resolve().parents[1]
# The CLI's own `action` command still reads a credential from the environment; that entrypoint is
# untouched, so the parity comparison keeps using it.
CLI_CREDENTIAL_ENV = "TIANSHU_PROJECT_SECRET"


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def request_body(operation, arguments, project="alpha"):
    return {"operation": operation, "project_id": project, "arguments": arguments}


@contextmanager
def serving(notes, port=None, *, client=CLIENT, credential=SECRET):
    """Start the real command as a child process and stop it again, always.

    Only the identity binding and the port are chosen by the test: the command is the one an
    operator would type, the child runs from the repository root, and its environment carries no
    knowledge credential at all.
    """
    port = free_port() if port is None else port
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
            str(notes.path),
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
        headers={"Authorization": f"Bearer {credential}"},
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode("utf-8", errors="replace"))
            try:
                health = served.get("/health")
                if health.status_code == 200 and health.json()["state"] == READY_STATE:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("The knowledge HTTP entry did not become ready")
            time.sleep(0.05)
        yield served, port
    finally:
        served.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()


def action(notes, operation, arguments, *, project="alpha", name="action.json"):
    """The same operation through the existing CLI adapter, for an exact comparison."""
    path = notes.tmp_path / name
    path.write_text(
        canonical(request_body(operation, arguments, project)),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(notes.path),
            "action",
            "--client",
            CLIENT,
            "--credential-env",
            CLI_CREDENTIAL_ENV,
            str(path),
        ],
        cwd=REPO_ROOT,
        env=dict(os.environ, **{CLI_CREDENTIAL_ENV: SECRET}, PYTHONUTF8="1"),
        capture_output=True,
        timeout=60,
    )


def send(client, operation, arguments, *, project="alpha", **kwargs):
    return client.post(ACTION, json=request_body(operation, arguments, project), **kwargs)


def package_of(client, text="receipt"):
    response = send(client, "note_recover", {"text": text, "budget_bytes": 16384})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_http_process_serves_a_bearer_with_no_secret_in_its_environment(notes):
    """The three credential verdicts over a real socket, with no server-side secret anywhere.

    The child process is started with every knowledge credential removed from its environment, so
    the only thing that can authorize a request is the Bearer value the request itself carries.
    A correct one is served, a wrong one is refused by the domain (422 with the domain's own
    code), and a missing one is refused by the transport (401) — three different layers, three
    different statuses, and nothing configured on the server.
    """
    imported(notes)
    body = request_body("query", {"text": "receipt", "budget_bytes": 8192})
    with serving(notes) as (client, port):
        assert client.post(ACTION, json=body).status_code == 200
        wrong = httpx.post(
            f"http://127.0.0.1:{port}{ACTION}",
            json=body,
            headers={"Authorization": f"Bearer {SECRET}-not-the-real-one"},
            timeout=20,
            trust_env=False,
        )
        assert wrong.status_code == 422
        assert wrong.json() == {"status": "failed", "code": "unauthorized"}
        absent = httpx.post(
            f"http://127.0.0.1:{port}{ACTION}", json=body, timeout=20, trust_env=False
        )
        assert absent.status_code == 401
        assert absent.json() == {"status": "failed", "code": "unauthorized"}
        assert absent.headers["cache-control"] == "no-store"


def test_real_http_process_agrees_with_the_existing_domain_entrypoint(notes):
    """The first acceptance point: a real process serves the same semantics as the CLI.

    The HTTP route adds no rule of its own, so a read and a note write through HTTP return exactly
    what the existing `action` command returns for the same request.
    """
    imported(notes)
    unit(notes)
    with serving(notes) as (client, port):
        arguments = {
            "key": "parity",
            "dedupe": "parity",
            "expected_version": 0,
            "note": note([unit(notes)], decision=decision([unit(notes)])),
        }
        over_http = send(client, "note_record", arguments)
        assert over_http.status_code == 200, over_http.text
        body = over_http.json()
        assert body["status"] == "recorded" and body["version"] == 1
        assert over_http.headers["cache-control"] == "no-store"
        assert over_http.headers["x-content-type-options"] == "nosniff"
    process = action(notes, "note_record", arguments, name="parity.json")
    assert process.returncode == 0, process.stderr
    # The CLI runs the same request after HTTP already recorded it, so its answer is that write's
    # replay: identical apart from the flag that says the ledger answered instead of a new write.
    assert json.loads(process.stdout) == {**body, "replayed": True}
    # The same comparison for a read, including the block reference the domain computed.
    with serving(notes) as (client, port):
        read = send(client, "query", {"text": "receipt", "budget_bytes": 8192})
        assert read.status_code == 200
        status = send(client, "note_status", {"note_id": body["note_id"], "version": 1})
        assert status.status_code == 200
        assert status.json()["hash"] == body["hash"]
    for operation, arguments, expected in (
        ("query", {"text": "receipt", "budget_bytes": 8192}, read.json()),
        ("note_status", {"note_id": body["note_id"], "version": 1}, status.json()),
    ):
        process = action(notes, operation, arguments, name=f"{operation}.json")
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout) == expected, operation


def test_real_http_process_full_note_lifecycle_and_restart(notes):
    """The whole block through a real process: retrieve, record, revise, replay, query, check,
    withdraw, then restart and find the same state and the same recorded results."""
    imported(notes)
    reference = unit(notes)
    port = free_port()
    with serving(notes, port) as (client, _):
        index = send(client, "query", {"text": "receipt", "budget_bytes": 8192})
        assert index.status_code == 200
        assert index.json()["blocks"][0]["reference"] == reference
        assert index.json()["retrieval"] == "lexical"

        record = request_body(
            "note_record",
            {
                "key": "lifecycle",
                "dedupe": "lifecycle",
                "expected_version": 0,
                "note": note([reference], decision=decision([reference])),
            },
        )
        recorded = client.post(ACTION, json=record)
        assert recorded.status_code == 200, recorded.text
        first = recorded.json()
        assert first["status"] == "recorded" and first["version"] == 1
        replayed = client.post(ACTION, json=record)
        assert replayed.status_code == 200
        assert replayed.json() == {**first, "replayed": True}

        found = send(client, "note_query", {"text": "receipt", "budget_bytes": 8192})
        assert found.status_code == 200
        view = found.json()["notes"][0]
        assert view["note_id"] == first["note_id"] and view["current"] is True
        assert view["hash"] == first["hash"]
        assert view["decision"]["basis"][0]["kind"] == "source"

        sealed = package_of(client)
        checked = send(client, "note_check", {"package": sealed})
        assert checked.status_code == 200
        assert checked.json() == {"valid": True, "reason": "current"}
        assert sealed["seal"] == checked.json().get("seal", sealed["seal"])

        revision = request_body(
            "note_revise",
            {
                "key": "lifecycle",
                "dedupe": "lifecycle-revise",
                "note_id": first["note_id"],
                "expected_version": 1,
                "note": note([reference], inferences=["A revised inference."]),
            },
        )
        revised = client.post(ACTION, json=revision)
        assert revised.status_code == 200, revised.text
        second = revised.json()
        assert second["status"] == "revised" and second["version"] == 2
        assert client.post(ACTION, json=revision).json() == {**second, "replayed": True}

        historical = send(client, "note_status", {"note_id": first["note_id"], "version": 1})
        assert historical.status_code == 200
        assert historical.json()["hash"] == first["hash"]

        withdrawal = request_body(
            "note_withdraw",
            {
                "key": "lifecycle-withdraw",
                "note_id": first["note_id"],
                "expected_version": 2,
                "reason": "superseded by the newer research",
            },
        )
        withdrawn = client.post(ACTION, json=withdrawal)
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["status"] == "withdrawn"
        assert (
            send(client, "note_query", {"text": "receipt", "budget_bytes": 8192}).json()["notes"]
            == []
        )
        after = package_of(client)
        assert send(client, "note_check", {"package": after}).json() == {
            "valid": True,
            "reason": "current",
        }

    # Restart: a new process on the same port reads the same state from disk.
    with serving(notes, port) as (client, _):
        assert client.get("/health").json()["state"] == READY_STATE
        status = send(client, "note_status", {"note_id": first["note_id"], "version": 3})
        assert status.status_code == 200
        assert status.json()["current_version"] == 3
        assert status.json()["state"] == "withdrawn"
        # Version 3 is the withdrawal, so the hashes to compare are the ones `note_status` reports
        # for each version — the withdrawal answer itself carries no hash of its own.
        expected_hashes = {
            1: first["hash"],
            2: second["hash"],
            3: status.json()["hash"],
        }
        for version, expected in expected_hashes.items():
            historical = send(
                client, "note_status", {"note_id": first["note_id"], "version": version}
            )
            assert historical.status_code == 200
            assert historical.json()["hash"] == expected, version
        # The recorded results survive the restart: a replay is still a replay, not a rewrite.
        assert client.post(ACTION, json=record).json() == {**first, "replayed": True}
        assert client.post(ACTION, json=withdrawal).json() == {
            **withdrawn.json(),
            "replayed": True,
        }
        assert client.post(ACTION, json=revision).json() == {**second, "replayed": True}
        assert (
            send(client, "note_query", {"text": "receipt", "budget_bytes": 8192}).json()["notes"]
            == []
        )


def test_real_http_process_refuses_without_side_effects(notes):
    """The second acceptance point, over the real socket: no credential, no origin, a forged
    identity, another project and an operation outside the allowlist are each refused, and none
    of them changes anything."""
    imported(notes)
    reference = unit(notes)
    with serving(notes) as (client, port):
        write = request_body(
            "note_record",
            {
                "key": "refused",
                "dedupe": "refused",
                "expected_version": 0,
                "note": note([reference], decision=decision([reference])),
            },
        )
        assert client.post(ACTION, json=write, headers={"Authorization": ""}).status_code == 401
        assert client.post(
            ACTION, json=write, headers={"Origin": "https://example.invalid"}
        ).json() == {"status": "failed", "code": "browser_origin_refused"}
        assert client.post(ACTION, json=write, headers={"Host": "example.invalid"}).json() == {
            "status": "failed",
            "code": "invalid_host",
        }
        forged = write | {"client": "operator"}
        forged_response = client.post(ACTION, json=forged)
        # A body field this entry does not act on is the entry's own verdict on the request text.
        assert forged_response.status_code == 400
        assert forged_response.json() == {"status": "failed", "code": "invalid_input"}
        # Another project is a real project this client is not registered for, so the domain
        # refuses it on the client's own project list rather than on the project's existence.
        cross_project = send(client, "note_record", forged["arguments"], project="beta")
        assert cross_project.status_code == 422
        assert cross_project.json() == {"status": "failed", "code": "forbidden"}
        assert send(
            client, "import", {"key": "x", "kind": "file", "locator": "source.md"}
        ).json() == {"status": "failed", "code": "unsupported"}
        # The refused key was never bound, so the same key still records exactly once afterwards.
        accepted = client.post(ACTION, json=write)
        assert accepted.status_code == 200
        assert accepted.json()["version"] == 1
        assert client.post(ACTION, json=write).json() == {**accepted.json(), "replayed": True}
    assert source_unchanged(notes)


def source_unchanged(notes):
    """Exactly the source import and the one accepted write exist, so nothing refused wrote.

    The import that seeded the project is itself one recorded operation, and the accepted
    `note_record` is the other; a refused request would add a third row here.
    """
    with notes.store.transaction() as db:
        rows = db.execute("SELECT COUNT(*) FROM research_notes").fetchone()[0]
        operations = db.execute("SELECT COUNT(*) FROM knowledge_operations").fetchone()[0]
    return rows == 1 and operations == 2


def test_real_http_process_survives_parallel_requests(notes):
    """Real client threads, real sockets, two identities: each response carries its own project.

    This is the concurrent path of the acceptance list without an in-process test double: two real
    processes serve two identities on their own ports, and eight requests are issued at once, so
    the four admission slots of each entry are genuinely contended while every accepted response
    still carries only the project its own client is registered for.
    """
    imported(notes)
    imported(notes, "beta")
    listeners = {"alpha": (CLIENT, SECRET), "beta": (BETA_CLIENT, OTHER_SECRET)}
    with (
        serving(notes) as alpha,
        serving(notes, client=BETA_CLIENT, credential=OTHER_SECRET) as beta,
    ):
        ports = {"alpha": alpha[1], "beta": beta[1]}

        def read(project, text):
            _, secret = listeners[project]
            with httpx.Client(
                base_url=f"http://127.0.0.1:{ports[project]}",
                timeout=30,
                trust_env=False,
                headers={"Authorization": f"Bearer {secret}"},
            ) as own:
                response = own.post(
                    ACTION,
                    json=request_body("query", {"text": text, "budget_bytes": 8192}, project),
                )
                return project, response

        work = [("alpha", "receipt")] * 4 + [("beta", "telemetry")] * 4
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [
                item.result(timeout=60) for item in [pool.submit(read, *item) for item in work]
            ]
        for project, response in results:
            assert response.status_code in {200, 503}, (project, response.text)
        served = [
            (project, response) for project, response in results if response.status_code == 200
        ]
        refused = [response for _, response in results if response.status_code == 503]
        assert set(project for project, _ in served) == {"alpha", "beta"}, [
            (project, response.text) for project, response in results
        ]
        assert all(
            response.json() == {"status": "failed", "code": "overloaded"} for response in refused
        )
        for project, response in served:
            blocks = response.json()["blocks"]
            assert blocks, project
            want = "receipt" if project == "alpha" else "telemetry"
            other = "telemetry" if project == "alpha" else "receipt"
            assert all(want in canonical(block["text"]) for block in blocks)
            assert all(other not in canonical(block["text"]) for block in blocks)
        # A refusal left no slot held: both entries serve normally straight afterwards.
        assert send(alpha[0], "query", {"text": "receipt", "budget_bytes": 8192}).status_code == 200
        assert (
            send(
                beta[0], "query", {"text": "telemetry", "budget_bytes": 8192}, project="beta"
            ).status_code
            == 200
        )


def read_response(connection, *, timeout=30):
    """Read one complete HTTP response from a raw socket: status line, headers and body.

    A single `recv` can return the headers without the body, so the declared `Content-Length` is
    read before the answer is judged.
    """
    connection.settimeout(timeout)
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = connection.recv(65536)
        if not chunk:
            return raw
        raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def status_of(raw):
    return int(raw.split(b"\r\n")[0].split(b" ")[1])


def body_of(raw):
    return json.loads(raw.partition(b"\r\n\r\n")[2].decode("utf-8"))


def test_real_http_process_refuses_a_fifth_request_before_reading_its_body(notes):
    """A real socket, four held bodies, and a fifth request that is refused before its body.

    The four holders open a request, write only part of the body and then stop writing, so the
    server is genuinely parked reading them and every admission slot is taken. The fifth request is
    sent on a fresh connection with no body at all and must be answered 503 without the server
    waiting for one: the answer can only arrive if the entry refused it before reading. Afterwards
    the held bodies are completed and all four are served, so the bound released exactly what it
    held.
    """
    imported(notes)
    request = canonical(request_body("query", {"text": "receipt", "budget_bytes": 8192})).encode()

    def headers(length, port):
        return (
            b"Host: 127.0.0.1:" + str(port).encode() + b"\r\n"
            b"Authorization: Bearer " + SECRET.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(length).encode() + b"\r\n\r\n"
        )

    with serving(notes) as (_, port):
        errors = []
        held = []

        def hold():
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=30)
                connection.sendall(
                    b"POST "
                    + ACTION.encode()
                    + b" HTTP/1.1\r\n"
                    + headers(len(request), port)
                    + request[:16]
                )
                held.append(connection)
            except OSError as error:  # pragma: no cover - only on a broken environment
                errors.append(error)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: hold(), range(4)))
        assert not errors and len(held) == 4
        # All four connections are inside the server, holding their slots with an unfinished body.
        time.sleep(1.0)
        # A fifth request announces no body and sends none: the entry must answer it immediately.
        probe = socket.create_connection(("127.0.0.1", port), timeout=30)
        probe.sendall(b"POST " + ACTION.encode() + b" HTTP/1.1\r\n" + headers(0, port))
        started = time.monotonic()
        answer = read_response(probe)
        elapsed = time.monotonic() - started
        assert status_of(answer) == 503, answer[:200]
        assert body_of(answer) == {"status": "failed", "code": "overloaded"}, answer[:400]
        # It was not parked waiting for a body it never sent, and it never read one.
        assert elapsed < 5, elapsed
        probe.close()
        # Completing the held bodies serves all four, so the bound released what it held.
        for connection in held:
            connection.sendall(request[16:])
            served = read_response(connection)
            assert status_of(served) == 200, served[:200]
            assert body_of(served)["blocks"], served[:400]
            connection.close()
        # And the entry is idle again: a normal request is served.
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=20,
            trust_env=False,
            headers={"Authorization": f"Bearer {SECRET}"},
        ) as fresh:
            assert (
                send(fresh, "query", {"text": "receipt", "budget_bytes": 8192}).status_code == 200
            )


def test_real_http_process_recovers_from_an_abandoned_request(notes):
    """A client that disappears mid-body does not cost the entry a slot or its health.

    The connection is closed after a partial body, which is what a crashed connector looks like.
    The entry must keep serving: the next real request is answered normally, which is only possible
    if the abandoned one gave its admission slot back.
    """
    imported(notes)
    request = canonical(request_body("query", {"text": "receipt", "budget_bytes": 8192})).encode()
    with serving(notes) as (client, port):
        abandoned = socket.create_connection(("127.0.0.1", port), timeout=30)
        abandoned.sendall(
            b"POST " + ACTION.encode() + b" HTTP/1.1\r\n"
            b"Host: 127.0.0.1:" + str(port).encode() + b"\r\n"
            b"Authorization: Bearer " + SECRET.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(request)).encode() + b"\r\n\r\n" + request[:16]
        )
        time.sleep(0.5)
        abandoned.close()
        # The entry is still alive and still serving, so the abandoned request released its slot.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            answer = send(client, "query", {"text": "receipt", "budget_bytes": 8192})
            if answer.status_code == 200:
                break
            assert answer.status_code == 503, answer.text
            time.sleep(0.1)
        assert answer.status_code == 200, answer.text
        assert answer.json()["blocks"]
        assert client.get("/health").json()["state"] == READY_STATE


@pytest.mark.parametrize(
    "tail",
    [
        ["serve", "--client", CLIENT],
        ["serve", "--client", CLIENT, "--port", "0"],
    ],
)
def test_real_process_refuses_to_start_without_an_explicit_valid_port(notes, tail):
    """The port is the operator's decision: there is no default and no borrowed chat port."""
    process = subprocess.run(
        [sys.executable, *COMMAND, "--config", str(notes.path), *tail],
        cwd=REPO_ROOT,
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        timeout=60,
    )
    assert process.returncode != 0
    if "--port" in tail:
        # A real port that is not a usable port is refused by the entry itself, with its code.
        assert json.loads(process.stdout) == {
            "status": "failed",
            "code": "invalid_configuration",
        }
    else:
        # Without a port at all, argparse refuses before anything runs: no default exists.
        assert b"--port" in process.stderr


def test_real_process_refuses_to_serve_an_unregistered_client(notes):
    """A process whose fixed client is not registered refuses to start instead of serving 401s."""
    process = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(notes.path),
            "serve",
            "--client",
            "not-registered",
            "--port",
            str(free_port()),
        ],
        cwd=REPO_ROOT,
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        timeout=60,
    )
    assert process.returncode == 1
    assert json.loads(process.stdout) == {"status": "failed", "code": "unregistered_client"}


def test_real_process_refuses_to_start_without_a_configuration(notes):
    process = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(notes.tmp_path / "absent.json"),
            "serve",
            "--client",
            CLIENT,
            "--port",
            str(free_port()),
        ],
        cwd=REPO_ROOT,
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        timeout=60,
    )
    assert process.returncode == 1
    assert json.loads(process.stdout) == {"status": "failed", "code": "invalid_configuration"}
