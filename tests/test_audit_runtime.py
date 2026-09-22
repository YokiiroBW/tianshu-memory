"""Actual chat ASGI entry: bounded work, late completion, and domain layering."""

import ast
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tianshu_memory import auth as auth_module
from tianshu_memory.app import create_app
from tianshu_memory.domain import Fault


class Driver:
    def __init__(self, app, token="test-only-companion-secret"):
        self.app, self.token = app, token
        self.queue = asyncio.Queue()
        self.reads, self.sent = 0, []

    def feed(self, body=b"", more=False):
        self.queue.put_nowait({"type": "http.request", "body": body, "more_body": more})

    async def receive(self):
        self.reads += 1
        return await self.queue.get()

    async def send(self, message):
        self.sent.append(message)

    async def run(self):
        await self.app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/internal/v1/identity/resolve",
                "query_string": b"",
                "headers": [(b"authorization", f"Bearer {self.token}".encode())],
            },
            self.receive,
            self.send,
        )
        status = next(m["status"] for m in self.sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")
        return status, json.loads(body)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def payload(h):
    return json.dumps({"query": h.query(), "account": h.account}).encode()


def test_eight_slow_bodies_refuse_ninth_without_reading_then_recover(h):
    async def scenario():
        app = create_app(service=h.service, auth=h.auth, body_timeout=0.4)
        held = [Driver(app) for _ in range(8)]
        tasks = [asyncio.create_task(d.run()) for d in held]
        await until(lambda: all(d.reads for d in held))
        ninth = Driver(app)
        assert (await ninth.run())[0] == 429
        assert ninth.reads == 0
        assert all(status == 408 for status, _ in await asyncio.gather(*tasks))
        assert app.state.chat_requests.active == 0
        legitimate = Driver(app)
        legitimate.feed(payload(h))
        status, body = await legitimate.run()
        assert status == 200 and body["person_id"] == h.person

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["timeout", "disconnect", "cancel"])
def test_late_business_work_keeps_capacity_and_never_replays(h, ending):
    entered, release = threading.Event(), threading.Event()
    original = h.service.resolve
    calls = []

    def slow(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args)

    h.service.resolve = slow

    async def scenario():
        app = create_app(
            service=h.service,
            auth=h.auth,
            execute_timeout=0.15 if ending == "timeout" else 3,
            max_active=1,
        )
        driver = Driver(app)
        driver.feed(payload(h))
        task = asyncio.create_task(driver.run())
        try:
            await until(entered.is_set)
            if ending == "disconnect":
                driver.queue.put_nowait({"type": "http.disconnect"})
            if ending == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                status, body = await task
                assert status == (408 if ending == "timeout" else 400)
                assert body["execution_state"] == "unknown" and not body["retryable"]
            assert app.state.chat_requests.active == 1
            rejected = Driver(app)
            assert (await rejected.run())[0] == 429
            assert rejected.reads == 0
        finally:
            release.set()
            await until(lambda: app.state.chat_requests.active == 0)
        assert len(calls) == 1
        legitimate = Driver(app)
        legitimate.feed(payload(h))
        assert (await legitimate.run())[0] == 200

    asyncio.run(scenario())


def test_authentication_body_size_and_disconnect_release_slot(h):
    async def scenario():
        app = create_app(service=h.service, auth=h.auth)
        unauthorized = Driver(app, token="invalid")
        assert (await unauthorized.run())[0] == 401
        assert unauthorized.reads == 0
        oversized = Driver(app)
        oversized.feed(b"x" * 262145, more=True)
        assert (await oversized.run())[0] == 400
        assert oversized.reads == 1
        disconnected = Driver(app)
        disconnected.queue.put_nowait({"type": "http.disconnect"})
        assert (await disconnected.run())[0] == 400
        assert app.state.chat_requests.active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_issuer_response_is_refused_without_reading_unbounded_body(h, monkeypatch, encoding):
    class Stream(httpx.SyncByteStream):
        reads = 0
        closed = False

        def __iter__(self):
            for _ in range(128):
                self.reads += 1
                yield b"x" * 65536

        def close(self):
            self.closed = True

    stream = Stream()
    client_type = httpx.Client

    def response(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding": encoding}, stream=stream)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(response), **kwargs),
    )
    h.config["mode"] = "remote"
    caller = h.config["callers"]["companion"]
    caller.update(issuer_url="https://synthetic.invalid/resolve", issuer_token="test-only")
    h.save_config()
    with pytest.raises(Fault, match="dependency_unavailable"):
        h.auth.resolve("companion", caller, "origin-private", "request-audit")
    assert stream.closed
    assert stream.reads <= (5 if encoding == "identity" else 0)


def test_issuer_drip_cannot_hide_deadline_in_a_chunk_buffer(h, monkeypatch):
    ticks = [0]

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                ticks[0] += 1
                yield b" "

    client_type = httpx.Client
    monkeypatch.setattr(auth_module, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip())),
            **kwargs,
        ),
    )
    h.config["mode"] = "remote"
    caller = h.config["callers"]["companion"]
    caller.update(issuer_url="https://synthetic.invalid/resolve", issuer_token="test-only")
    h.save_config()
    with pytest.raises(Fault, match="dependency_unavailable"):
        h.auth.resolve("companion", caller, "origin-private", "request-drip")
    assert ticks[0] == 5


def test_domain_imports_have_no_cycles_even_inside_functions():
    root = Path(__file__).parents[1] / "src/tianshu_memory"
    modules = {p.stem: p for p in root.glob("*.py")}
    graph = {name: set() for name in modules}
    for name, path in modules.items():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    target = (node.module or "").split(".")[0]
                    graph[name].update(
                        [target]
                        if target in modules
                        else [n.name for n in node.names if n.name in modules]
                    )
                elif node.level == 0 and (node.module or "").startswith("tianshu_memory."):
                    graph[name].add(node.module.split(".")[1])
            elif isinstance(node, ast.Import):
                graph[name].update(
                    n.name.split(".")[1] for n in node.names if n.name.startswith("tianshu_memory.")
                )
    finished = set()

    def visit(name, stack):
        assert name not in stack, f"Import cycle: {stack + [name]}"
        if name in finished:
            return
        for target in graph.get(name, ()):
            visit(target, stack + [name])
        finished.add(name)

    for name in graph:
        visit(name, [])
    for name in (
        "knowledge_evidence",
        "lessons",
        "knowledge_directories",
        "knowledge_continuation",
    ):
        assert "knowledge" not in graph[name]
    assert "user_actions" not in graph["workflow"]
