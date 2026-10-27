"""Explicit module assembly, live client ownership and real worker drain."""

import asyncio
import copy
import hashlib
import json
import os
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from tianshu_storage import StorageOptions, StorageRuntime

from tianshu_memory.app import runtime_app
from tianshu_memory.chat_requests import Requests
from tianshu_memory.diagnostics import CHAT_SERVICE, DEFAULT_TOKEN_ENV, Diagnostics
from tianshu_memory.knowledge_http import ACTION_PATH, HEALTH_PATH, create_app
from tianshu_memory.knowledge_http import runtime_app as knowledge_runtime_app
from tianshu_memory.knowledge_storage import KnowledgeStorage
from tianshu_memory.runtime_probes import SelectedDatabase, present_token
from tianshu_memory.source_http_pool import OwnerHttpPool


def knowledge_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = {
        "database_path": str(tmp_path / "knowledge.sqlite"),
        "knowledge": {
            "clients": {
                "reader": {
                    "credential_sha256": hashlib.sha256(b"synthetic-secret-value").hexdigest(),
                    "projects": ["project"],
                    "permissions": ["query"],
                }
            },
            "projects": {
                "project": {
                    "root": str(project),
                    "host": "local",
                    "default_branch": "main",
                    "urls": [],
                }
            },
        },
    }
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path, config


def test_explicit_factories_keep_config_logs_storage_and_probe_domains(h, tmp_path, monkeypatch):
    monkeypatch.setenv("TIANSHU_MEMORY_CONFIG", str(tmp_path / "wrong.json"))
    monkeypatch.setenv("TIANSHU_POSTGRES_URL", "postgresql://invalid-platform-domain")
    monkeypatch.setenv("TIANSHU_STORAGE_NAMESPACE", "platform-domain")
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "platform-token")
    monkeypatch.setenv("TIANSHU_LOG_DIR", str(tmp_path / "wrong-logs"))
    original = dict(os.environ)
    contract = h.contracts.directory.parent.parent / "diagnostics" / "v1"
    config = copy.deepcopy(h.config)
    config["database_path"] = str(tmp_path / "memory.sqlite")
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "memory-logs").mkdir()
    (tmp_path / "knowledge-logs").mkdir()
    memory = runtime_app(
        path,
        contract,
        storage_environ={},
        log_directory=tmp_path / "memory-logs",
        diagnostics_environ={DEFAULT_TOKEN_ENV: "memory-token"},
    )
    knowledge_path, _ = knowledge_config(tmp_path)
    knowledge = knowledge_runtime_app(
        knowledge_path,
        contract,
        client="reader",
        port=8130,
        storage_environ={},
        log_directory=tmp_path / "knowledge-logs",
        diagnostics_environ={DEFAULT_TOKEN_ENV: "knowledge-token"},
    )
    with (
        TestClient(memory) as first,
        TestClient(knowledge, base_url="http://127.0.0.1:8130") as second,
    ):
        assert first.get("/missing").status_code == 404
        assert second.get(HEALTH_PATH).status_code == 200
        assert memory.state.memory.store.runtime.options == StorageOptions.from_settings(
            config, environ={}
        )
        assert (
            memory.state.probe_settings(
                SimpleNamespace(diagnostics=memory.state.diagnostics)
            ).storage_environ
            == {}
        )
    assert list((tmp_path / "memory-logs").glob("*.jsonl"))
    assert list((tmp_path / "knowledge-logs").glob("*.jsonl"))
    assert not (tmp_path / "wrong-logs").exists()
    assert dict(os.environ) == original


def test_selected_database_and_probe_tokens_use_their_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TIANSHU_STORAGE_NAMESPACE", "wrong-platform")
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "wrong-platform-token")
    seen = []
    monkeypatch.setattr(
        StorageRuntime, "open_existing", lambda options, **kwargs: seen.append(options)
    )
    raw = {"database_path": str(tmp_path / "unopened.sqlite")}
    SelectedDatabase(
        raw, CHAT_SERVICE, storage_environ={"TIANSHU_STORAGE_NAMESPACE": "memory-domain"}
    ).readonly()
    assert seen[0].namespace == "memory-domain"
    diagnostics = Diagnostics(
        CHAT_SERVICE, {"log_directory": False}, environ={DEFAULT_TOKEN_ENV: "memory-token"}
    )
    try:
        request = Request({"type": "http", "headers": [(b"authorization", b"Bearer memory-token")]})
        assert present_token(request, diagnostics) == "ok"
        other = Request(
            {"type": "http", "headers": [(b"authorization", b"Bearer wrong-platform-token")]}
        )
        assert present_token(other, diagnostics) == "unauthorized"
    finally:
        diagnostics.shutdown()


def test_drain_keeps_late_worker_ownership_and_identity_callback_admission():
    async def scenario():
        requests = Requests(2, reserve_identity=True)
        lease = requests.claim()
        entered, release = threading.Event(), threading.Event()
        receipts = []

        def worker():
            entered.set()
            release.wait(3)
            receipts.append("committed")

        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lease.run(worker, business=True), 0.03)
            assert entered.is_set()
            lease.finish()
            draining = asyncio.create_task(requests.drain())
            await asyncio.sleep(0)
            assert not draining.done() and requests.active == 1
            identity = requests.claim(identity=True)
            identity.finish()
            release.set()
            await asyncio.wait_for(draining, 2)
            assert receipts == ["committed"] and not requests.leases and requests.active == 0
        finally:
            release.set()

    asyncio.run(scenario())


def test_knowledge_drain_waits_after_timed_out_response(tmp_path):
    async def scenario():
        path, _ = knowledge_config(tmp_path)
        app = create_app(path, "reader", 8130, execute_timeout=0.03, storage_environ={})
        entered, release = threading.Event(), threading.Event()
        receipts = []

        def observer(body, credential, application):
            entered.set()
            release.wait(3)
            receipts.append("real-worker-ended")
            return {"status": "complete"}

        app.state.observer = observer
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8130"
                ) as client:
                    response = await client.post(
                        ACTION_PATH,
                        headers={"Authorization": "Bearer synthetic-secret-value"},
                        json={
                            "operation": "query",
                            "project_id": "project",
                            "arguments": {"text": "x", "budget_bytes": 1024},
                        },
                    )
                    assert response.status_code == 408 and entered.is_set()
                    draining = asyncio.create_task(app.state.drain())
                    await asyncio.sleep(0)
                    assert not draining.done() and app.state.active == 1
                    assert (await client.get(HEALTH_PATH)).status_code == 200
                    release.set()
                    await asyncio.wait_for(draining, 2)
                    assert receipts == ["real-worker-ended"] and app.state.active == 0
        finally:
            release.set()

    asyncio.run(scenario())


def test_index_quiesce_and_drain_finish_workers_before_closing_gateway(monkeypatch):
    from fastapi import FastAPI

    from tianshu_memory.memory_index_lifecycle import attach

    async def scenario():
        admission = Requests()
        index_entered, index_release = threading.Event(), threading.Event()
        work_entered, work_release = threading.Event(), threading.Event()
        receipts = []

        class Backend:
            def close(self):
                assert receipts == ["index-finished", "business-finished"]
                receipts.append("closed")

        class Indexer:
            def __init__(self, service):
                pass

            def index_step(self, *, batch_size):
                index_entered.set()
                assert index_release.wait(3)
                receipts.append("index-finished")
                return {"state": "pending"}

        @asynccontextmanager
        async def lifespan(app):
            yield

        app = FastAPI(lifespan=lifespan)
        app.state.drain = admission.drain
        monkeypatch.setattr("tianshu_memory.memory_index_lifecycle.MemoryReindexer", Indexer)
        attach(app, SimpleNamespace(retrieval=SimpleNamespace(backend=Backend())))

        def worker():
            work_entered.set()
            assert work_release.wait(3)
            receipts.append("business-finished")

        try:
            async with app.router.lifespan_context(app):
                assert await asyncio.to_thread(index_entered.wait, 2)
                lease = admission.claim()
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(lease.run(worker, business=True), 0.03)
                lease.finish()
                assert work_entered.is_set()
                await app.state.quiesce()
                draining = asyncio.create_task(app.state.drain())
                await asyncio.sleep(0)
                assert not draining.done() and not receipts
                draining.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await draining
                draining = asyncio.create_task(app.state.drain())
                await asyncio.sleep(0)
                assert not draining.done()
                index_release.set()
                while not receipts:
                    await asyncio.sleep(0)
                assert not draining.done() and receipts == ["index-finished"]
                work_release.set()
                await asyncio.wait_for(draining, 2)
                assert receipts == ["index-finished", "business-finished"]
            assert receipts[-1] == "closed"
        finally:
            index_release.set()
            work_release.set()

    asyncio.run(scenario())


def test_knowledge_storage_recomputes_live_registration_in_its_own_environment(
    tmp_path, monkeypatch
):
    _, config = knowledge_config(tmp_path)
    monkeypatch.setenv("TIANSHU_POSTGRES_URL", "postgresql://wrong-platform")
    owner = KnowledgeStorage(storage_environ={})
    first_key = owner.registration(config)
    try:
        with owner.lease(config, first_key) as first:
            changed = dict(config, database_path=str(tmp_path / "knowledge-next.sqlite"))
            second_key = owner.registration(changed)
            assert first_key != second_key
            with owner.lease(changed, second_key) as second:
                assert first is not second and first.backend == second.backend == "sqlite"
                assert first.runtime.options == StorageOptions.from_settings(config, environ={})
    finally:
        owner.close()


def test_failed_producer_drain_still_closes_memory_auth_resources():
    from tianshu_memory.app import create_app

    async def scenario():
        closed = []
        app = create_app(auth=SimpleNamespace(close=lambda: closed.append("auth")))

        async def failed():
            raise RuntimeError("synthetic index programming failure")

        app.state.drain = failed
        with pytest.raises(RuntimeError, match="synthetic index programming failure"):
            async with app.router.lifespan_context(app):
                pass
        assert closed == ["auth"] and app.state.actor_memory.closed

    asyncio.run(scenario())


def test_owner_transport_factory_reuses_only_its_explicit_capability():
    made, requests = [], []

    class Transport(httpx.BaseTransport):
        def __init__(self):
            self.closed = False
            made.append(self)

        def handle_request(self, request):
            requests.append(request.headers["authorization"])
            return httpx.Response(200, json={"valid": True})

        def close(self):
            self.closed = True

    pool = OwnerHttpPool(internal_clients={"platform": Transport})
    try:
        for token in ("first", "rotated"):
            with pool.client(
                "issuer-companion",
                "https://platform:8443/origin",
                "/unused/local-only-ca.pem",
                target="platform",
            ) as client:
                assert client.post(
                    "https://platform:8443/origin", headers={"Authorization": token}
                ).json()["valid"]
        assert len(made) == 1 and requests == ["first", "rotated"] and not made[0].closed
    finally:
        pool.close()
    assert made[0].closed


def test_real_exchange_calls_memory_factory_and_live_platform_issuer(h, tmp_path):
    exchange_module = pytest.importorskip("deploy.single_container.exchange")
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def scenario():
        exchange = exchange_module.LocalExchange()
        config = copy.deepcopy(h.config)
        config.update(mode="service", database_path=str(tmp_path / "local-exchange.sqlite"))
        caller = config["callers"]["companion"]
        caller.update(
            issuer="platform",
            issuer_url="https://platform:8443/internal/v1/origins/resolve",
            issuer_token="synthetic-memory-resolver-token",
        )
        path = tmp_path / "exchange-memory.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        seen = []

        async def resolve(request):
            assert exchange.owns(request) and request.scope["scheme"] == "http"
            assert request.headers["authorization"] == "Bearer synthetic-memory-resolver-token"
            body = await request.json()
            h.contracts.validate("common#origin_resolve_request", body)
            seen.append(body["assertion_ref"])
            context = dict(h.config["origins"][body["assertion_ref"]], issuer="platform")
            return JSONResponse(
                {"schema_version": 1, "request_id": body["request_id"], "context": context}
            )

        memory = runtime_app(
            path,
            None,
            internal_clients={"platform": lambda: exchange.sync_transport("platform")},
            storage_environ={},
        )
        platform = Starlette(
            routes=[Route("/internal/v1/origins/resolve", resolve, methods=["POST"])]
        )
        exchange.register("platform", platform, "asgi", "https://platform:8443")
        exchange.register("memory", memory, "asgi", "https://memory:8443")
        await exchange.open()
        try:
            async with httpx.AsyncClient(transport=exchange.async_transport("memory")) as client:
                response = await client.post(
                    "https://memory:8443/internal/v1/identity/resolve",
                    headers={"Authorization": "Bearer test-only-companion-secret"},
                    json={"query": h.query("origin-first"), "account": h.account},
                )
                assert response.status_code == 200 and response.json()["state"] == "unregistered"
            await memory.state.drain()
            assert seen == ["origin-first"] and not memory.state.chat_requests.leases
        finally:
            await exchange.close()

    asyncio.run(scenario())
