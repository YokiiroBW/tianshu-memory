"""Source I/O gates must not hold the shared Memory writer lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_knowledge import imported, query, state
from test_knowledge import knowledge as knowledge

from tianshu_memory import knowledge as module
from tianshu_memory import knowledge_sources as source_module
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.service import MemoryService


def chat_operation(store, contracts):
    service = MemoryService(store, contracts)
    account = {"namespace": "synthetic", "immutable_account_id": "concurrent-chat"}
    context = {
        "audience_service": "memory",
        "revoked": False,
        "expires_at": "2030-01-01T00:00:00Z",
        "verified_account": account,
        "allowed_scope": {"person_id": None},
        "authenticated_service": "synthetic-client",
    }
    service.register(
        {
            "account": account,
            "command": {
                "idempotency_key": "register",
                "request_id": "register",
                "deadline_at": "2030-01-01T00:00:00Z",
            },
        },
        context,
    )
    return service.resolve({"account": account, "query": {"request_id": "resolve"}}, context)


def operation(run, name, pack, note):
    if name == "import_url":
        return imported(
            run, key="slow", kind="url", locator="https://example.com/design", groups=None
        )
    if name == "import_file":
        return imported(run, key="slow", expected_version=1, groups=None)
    if name == "query":
        return query(run)
    if name == "recover":
        return run("recover", dict(text="receipt", budget_bytes=8192))
    if name == "check":
        return run("check", {"package": pack})
    return run("write_state", dict(key="new-state", expected_version=1, state=note))


def gate(monkeypatch, name, real):
    entered, release = Event(), Event()

    def paused(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "test gate was not released"
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, paused)
    if name == "read_file":
        monkeypatch.setattr(source_module, name, paused)
    return entered, release


@pytest.mark.parametrize(
    "name", ["import_url", "import_file", "query", "recover", "check", "write_state"]
)
def test_slow_source_allows_real_memory_register_and_resolve(
    knowledge, contracts, monkeypatch, name
):
    run, _, store, _, _ = knowledge
    imported(run)
    note = state(run)
    run("write_state", dict(key="state", expected_version=0, state=note))
    pack = run("recover", dict(text="receipt", budget_bytes=8192))
    hook = "fetch_url" if name == "import_url" else "read_file"
    real = (
        (lambda *a: (b"Receipt from URL", "text/plain", "https://example.com/design"))
        if hook == "fetch_url"
        else module.read_file
    )
    entered, release = gate(monkeypatch, hook, real)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(operation, run, name, pack, note)
        try:
            assert entered.wait(2)
            # The old implementation waits on SQLite's five-second writer timeout here.
            assert (
                pool.submit(chat_operation, store, contracts).result(timeout=1)["state"] == "found"
            )
        finally:
            release.set()
        result = waiting.result(timeout=3)
    if name == "check":
        assert result["valid"]
    elif name.startswith("import"):
        assert result["status"] == "imported"


@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("mutation", ["delete", "replace", "revoke", "registration"])
def test_import_gap_never_overwrites_concurrent_changes(knowledge, monkeypatch, outcome, mutation):
    run, _, store, path, config = knowledge
    url = "https://example.com/design"
    monkeypatch.setattr(module, "fetch_url", lambda *a: (b"Receipt original", "text/plain", url))
    first = imported(run, kind="url", locator=url, groups=None)
    entered, release = Event(), Event()

    def slow(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
            if outcome == "failure":
                raise OSError("synthetic failure")
            return b"Receipt stale delayed content", "text/plain", url
        return b"Receipt concurrent replacement", "text/plain", url

    monkeypatch.setattr(module, "fetch_url", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(
            imported, run, key="slow", kind="url", locator=url, groups=None, expected_version=1
        )
        try:
            assert entered.wait(2)
            if mutation == "delete":
                pool.submit(
                    run,
                    "delete",
                    dict(key="delete", document_id=first["document_id"], expected_version=1),
                ).result(timeout=1)
            elif mutation == "replace":
                pool.submit(
                    imported,
                    run,
                    key="replace",
                    kind="url",
                    locator=url,
                    groups=None,
                    expected_version=1,
                ).result(timeout=1)
            else:
                if mutation == "revoke":
                    config["knowledge"]["clients"]["writer"]["permissions"] = ["query"]
                else:
                    config["knowledge"]["projects"]["demo"]["default_branch"] = "changed"
                path.write_text(canonical(config))
        finally:
            release.set()
        with pytest.raises(Fault, match="project_conflict|forbidden|registration_changed"):
            waiting.result(timeout=3)
    with store.transaction() as db:
        row = db.execute(
            "SELECT version,state FROM knowledge_documents WHERE id=?", (first["document_id"],)
        ).fetchone()
        assert tuple(row) == (
            (2, "deleted")
            if mutation == "delete"
            else (2, "ready")
            if mutation == "replace"
            else (1, "ready")
        )
        assert not db.execute("SELECT 1 FROM knowledge_operations WHERE key='slow'").fetchone()


@pytest.mark.parametrize("name", ["query", "recover", "check", "write_state"])
@pytest.mark.parametrize("mutation", ["delete", "revoke"])
def test_read_gap_revalidates_project_and_permissions(knowledge, monkeypatch, name, mutation):
    run, _, _, path, config = knowledge
    first = imported(run)
    note = state(run)
    run("write_state", dict(key="state", expected_version=0, state=note))
    pack = run("recover", dict(text="receipt", budget_bytes=8192))
    entered, release = gate(monkeypatch, "read_file", module.read_file)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(operation, run, name, pack, note)
        try:
            assert entered.wait(2)
            if mutation == "delete":
                pool.submit(
                    run,
                    "delete",
                    dict(key="delete", document_id=first["document_id"], expected_version=1),
                ).result(timeout=1)
            else:
                config["knowledge"]["clients"]["writer"]["projects"] = []
                path.write_text(canonical(config))
        finally:
            release.set()
        with pytest.raises(Fault, match="project_conflict|forbidden"):
            waiting.result(timeout=3)


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
def test_timeout_and_cancellation_leave_no_lock_or_partial_import(
    knowledge, contracts, monkeypatch, outcome
):
    run, _, store, _, _ = knowledge
    entered, release = Event(), Event()

    def stopped(*args):
        entered.set()
        assert release.wait(10)
        if outcome == "cancel":
            raise KeyboardInterrupt("synthetic cancellation")
        raise Fault("source_timeout", 408)

    monkeypatch.setattr(module, "fetch_url", stopped)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(
            imported, run, kind="url", locator="https://example.com/design", groups=None
        )
        try:
            assert entered.wait(2)
            assert (
                pool.submit(chat_operation, store, contracts).result(timeout=1)["state"] == "found"
            )
        finally:
            release.set()
        if outcome == "cancel":
            with pytest.raises(KeyboardInterrupt):
                waiting.result(timeout=3)
        else:
            assert waiting.result(timeout=3)["code"] == "source_timeout"
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["query", "recover", "check", "write_state", "import_file"])
def test_file_change_during_external_validation_cannot_yield_current_evidence(
    knowledge, monkeypatch, name
):
    run, root, _, _, _ = knowledge
    imported(run)
    note = state(run)
    run("write_state", dict(key="state", expected_version=0, state=note))
    pack = run("recover", dict(text="receipt", budget_bytes=8192))
    real = module.read_file
    count = 0

    def changed(*args):
        nonlocal count
        raw = real(*args)
        count += 1
        if count == 1:
            (root / "design.md").write_text("New uncommitted receipt behavior")
        return raw

    monkeypatch.setattr(module, "read_file", changed)
    monkeypatch.setattr(source_module, "read_file", changed)
    if name == "write_state":
        with pytest.raises(Fault, match="stale_evidence"):
            operation(run, name, pack, note)
        return
    result = operation(run, name, pack, note)
    if name == "check":
        assert not result["valid"]
    elif name == "import_file":
        assert result["status"] == "failed" and result["code"] == "source_changed"
    else:
        assert not result["blocks"]
        if name == "recover":
            assert result["state"] is None


@pytest.mark.parametrize("same", ["same", "conflict"])
def test_idempotency_rechecked_after_io_and_deleted_result_not_resurrected(
    knowledge, monkeypatch, same
):
    run, _, store, _, _ = knowledge
    url = "https://example.com/design"
    entered, release = gate(monkeypatch, "fetch_url", lambda *a: (b"Receipt", "text/plain", url))
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(imported, run, key="same", kind="url", locator=url, groups=None)
        try:
            assert entered.wait(2)
            if same == "same":
                result = pool.submit(
                    imported, run, key="same", kind="url", locator=url, groups=None
                ).result(timeout=1)
            else:
                result = pool.submit(imported, run, key="same", groups=None).result(timeout=1)
            pool.submit(
                run,
                "delete",
                dict(key="delete", document_id=result["document_id"], expected_version=1),
            ).result(timeout=1)
        finally:
            release.set()
        if same == "same":
            assert waiting.result(timeout=3)["replayed"]
        else:
            with pytest.raises(Fault, match="idempotency_conflict"):
                waiting.result(timeout=3)
    with store.transaction() as db:
        assert db.execute("SELECT state FROM knowledge_documents").fetchone()[0] == "deleted"
