"""One reusable transport carries the live execution, never an earlier caller."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tianshu_memory.diagnostics import (
    CORRELATION_HEADER,
    Diagnostics,
    correlation_of,
    read_correlation,
)
from tianshu_memory.source_http_pool import OwnerHttpPool


def test_owner_calls_follow_each_request_through_worker_and_source_fanout(tmp_path):
    observed, clients = [], []

    def owner(request):
        observed.append((request.headers[CORRELATION_HEADER], request.headers["Authorization"]))
        return httpx.Response(200, json={})

    def transport():
        clients.append(1)
        return httpx.MockTransport(owner)

    pool = OwnerHttpPool(internal_clients={"platform": transport})
    app = FastAPI()
    directory = tmp_path / "logs"
    directory.mkdir()
    diagnostics = Diagnostics("memory", {"log_directory": str(directory)})
    diagnostics.install(app)

    def send():
        with pool.client("platform", "https://owner.test/read") as client:
            return client.post(
                "https://owner.test/read",
                headers={"Authorization": "Bearer synthetic", CORRELATION_HEADER: "untrusted"},
                json={},
            ).status_code

    @app.post("/read")
    def read(request: Request):
        # The request's threadpool boundary and the source fanout both copy
        # ContextVars in production; the reusable HTTPX hook runs at send time.
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(copy_context().run, send) for _ in range(2)]
            return {
                "statuses": [task.result() for task in pending],
                "correlation": correlation_of(request),
            }

    try:
        with TestClient(app) as client:
            for correlation in ("a" * 32, "b" * 32, "bad-correlation"):
                response = client.post("/read", headers={CORRELATION_HEADER: correlation})
                assert response.status_code == 200
                actual = response.json()["correlation"]
                assert read_correlation(actual) is not None
                if correlation != "bad-correlation":
                    assert actual == correlation
                else:
                    assert actual != correlation
                assert observed[-2:] == [(actual, "Bearer synthetic")] * 2
        assert len(clients) <= 2  # Concurrent first borrowers may build a losing generation.
        send()
        send()
        assert observed[-2][0] != observed[-1][0]
        assert all(read_correlation(value) is not None for value, _ in observed)
    finally:
        pool.close()
        diagnostics.shutdown()
