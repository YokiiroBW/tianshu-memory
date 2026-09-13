import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx


@contextmanager
def server(config_path):
    with socket.socket() as socket_reservation:
        socket_reservation.bind(("127.0.0.1", 0))
        port = socket_reservation.getsockname()[1]
    env = dict(os.environ, TIANSHU_MEMORY_CONFIG=str(config_path), PYTHONUTF8="1")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tianshu_memory.app:configured_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=3,
        trust_env=False,
        headers={"Authorization": "Bearer test-only-companion-secret"},
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode("utf-8", errors="replace"))
            try:
                health = client.get("/health")
                if health.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("Local HTTP service did not become ready")
            time.sleep(0.05)
        yield client
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def test_real_http_process_restart_replay_revision(h):
    seeded, _, event = h.seed()
    with server(h.config_path) as client:
        health = client.get("/health").json()
        assert (
            health["source_backend"] == "local_fixture"
            and health["raw_source_read"] == "unavailable"
        )
        response = client.post("/internal/v1/memory/select", json=h.selection())
        assert response.status_code == 200 and len(response.json()["selected_units"]) == 1
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post("/internal/v1/memory/turn-commits", json=event).json()["state"]
            == "duplicate"
        )
        before = response.json()["scope_version"]
        request = h.revision(seeded["record_ids"][0], "forget")
        assert client.post("/internal/v1/memory/revise", json=request).status_code == 200
    with server(h.config_path) as client:
        assert (
            client.post("/internal/v1/memory/select", json=h.selection()).json()["selected_units"]
            == []
        )
        assert (
            client.post(
                "/internal/v1/memory/select", json=h.selection(budget=0, known=before)
            ).json()["code"]
            == "scope_changed"
        )
        assert client.post("/internal/v1/memory/revise", json=request).json()["record_version"] == 2
