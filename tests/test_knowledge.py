"""Project-domain acceptance with isolated files, DB, auth and real stdio MCP."""

import asyncio
import copy
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication, blocks
from tianshu_memory.knowledge_migration import TRACKED, migrate
from tianshu_memory.knowledge_sources import MAX_BYTES, decode, fetch_url
from tianshu_memory.store import Store

SECRET = "synthetic-project-client-secret"


@pytest.fixture
def knowledge(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "knowledge.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    root = tmp_path / "registered"
    root.mkdir()
    (root / "design.md").write_text(
        "Only retry when the receipt is absent.\nDo not resend after success.\n"
        "Because receipts prove execution, preserve them.\n"
        "The unrelated garden has roses.\n",
        encoding="utf-8",
    )
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {
                "demo": {
                    "root": str(root),
                    "host": "local",
                    "default_branch": "main",
                    "urls": ["https://example.com/design"],
                }
            },
            "clients": {
                "writer": {
                    "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
                    "projects": ["demo"],
                    "permissions": [
                        "import",
                        "query",
                        "recover",
                        "check",
                        "write_state",
                        "delete",
                        "status",
                    ],
                },
                "reader": {
                    "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
                    "projects": ["demo"],
                    "permissions": ["query", "recover", "check"],
                },
            },
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    app = KnowledgeApplication(path)

    def run(operation, arguments, project_id="demo", client="writer", credential=SECRET):
        return app.execute(
            dict(operation=operation, project_id=project_id, arguments=arguments),
            client=client,
            credential=credential,
        )

    return run, root, store, path, config


def imported(run, **changes):
    args = dict(
        key="import-1",
        kind="file",
        locator="design.md",
        expected_version=0,
        groups=[
            dict(start=1, end=1, depends_on=[1]),
            dict(start=2, end=3, depends_on=[]),
            dict(start=4, end=4, depends_on=[]),
        ],
    )
    args.update(changes)
    return run("import", args)


def query(run, text="retry receipt", budget=8192):
    return run("query", {"text": text, "budget_bytes": budget})


def state(run):
    reference = query(run)["blocks"][0]["reference"]
    return dict(
        goal="Reliable receipt retries",
        constraints=["Never resend confirmed work"],
        recent_verification=["Isolated retry test passed"],
        unfinished=["Real service test"],
        evidence=[reference],
        pitfalls=[
            dict(
                trigger="Timeout after send",
                symptom="Duplicate",
                cause="Missing receipt check",
                correction="Check receipt",
                verification="Isolated retry test",
                evidence=[reference],
            )
        ],
    )


def test_import_exact_blocks_versions_and_idempotency(knowledge):
    run, root, store, _, _ = knowledge
    result = imported(run)
    assert result["status"] == "imported" and result["version"] == 1
    assert imported(run)["replayed"]
    assert imported(run, key="again", expected_version=1)["status"] == "unchanged"
    with pytest.raises(Fault, match="idempotency_conflict"):
        imported(run, locator="other.md")
    with pytest.raises(Fault, match="version_conflict"):
        imported(run, key="stale")
    hit = query(run)
    assert len(hit["blocks"]) == 1
    assert "Do not resend" in hit["blocks"][0]["text"]
    assert "Because receipts" in hit["blocks"][0]["text"]
    assert "garden" not in hit["blocks"][0]["text"]
    assert hit["blocks"][0]["spans"] == [[1, 1], [2, 3]]
    assert not query(run, "zebras")["blocks"]
    assert not query(run, budget=256)["blocks"]
    assert query(run, budget=256)["omissions"] == ["budget"]
    with store.transaction() as db:
        assert (
            db.execute("SELECT raw FROM knowledge_versions").fetchone()[0]
            == (root / "design.md").read_bytes()
        )
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_live_worktree_and_replacement_delete_invalidate_recovery(knowledge):
    run, root, _, _, _ = knowledge
    first = imported(run)
    written = run("write_state", dict(key="state", expected_version=0, state=state(run)))
    assert written["sharing"] == "project_only"
    pack = run("recover", dict(text="retry", budget_bytes=8192))
    assert pack["state"]["version"] == 1 and run("check", {"package": pack})["valid"]
    assert len(canonical(pack).encode()) <= 8192
    (root / "design.md").write_text("New uncommitted receipt behavior.\n", encoding="utf-8")
    assert not run("check", {"package": pack})["valid"]
    assert query(run)["omissions"] == ["stale_source"]
    assert "stale_state" in run("recover", dict(text="retry", budget_bytes=8192))["omissions"]
    replacement = imported(run, key="replacement", expected_version=1, groups=None)
    assert replacement["document_id"] == first["document_id"] and replacement["version"] == 2
    assert not run("check", {"package": pack})["valid"]
    new = run("recover", dict(text="receipt", budget_bytes=8192))
    assert new["state"] is None and new["blocks"]
    run("delete", dict(key="delete", document_id=first["document_id"], expected_version=2))
    assert not run("check", {"package": new})["valid"]
    assert not query(run)["blocks"] and (root / "design.md").exists()
    resurrect = imported(run, key="reimport", expected_version=3, groups=None)
    assert resurrect["version"] == 4


@pytest.mark.parametrize("case", ["wrong_secret", "other_project", "reader_write", "body_identity"])
def test_client_permissions_fail_closed(knowledge, case):
    run, _, _, _, _ = knowledge
    with pytest.raises(Fault):
        if case == "wrong_secret":
            run("query", dict(text="receipt", budget_bytes=8192), credential="incorrect-credential")
        elif case == "other_project":
            run("query", dict(text="receipt", budget_bytes=8192), project_id="other")
        elif case == "reader_write":
            run("write_state", {}, client="reader")
        else:
            run("query", dict(text="receipt", budget_bytes=8192, client="writer"))


def test_state_conflict_evidence_tamper_and_budget(knowledge):
    run, _, _, _, _ = knowledge
    imported(run)
    note = state(run)
    run("write_state", dict(key="state", expected_version=0, state=note))
    with pytest.raises(Fault, match="version_conflict"):
        run("write_state", dict(key="state2", expected_version=0, state=note))
    note["evidence"][0]["hash"] = "forged"
    with pytest.raises(Fault, match="stale_evidence"):
        run("write_state", dict(key="state3", expected_version=1, state=note))
    small = run("recover", dict(text="receipt", budget_bytes=1024))
    assert small["state"] is None and len(canonical(small).encode()) <= 1024
    pack = run("recover", dict(text="receipt", budget_bytes=8192))
    pack["state"]["goal"] = "Forged state"
    assert not run("check", {"package": pack})["valid"]
    pack["seal"] = hmac.new(
        hashlib.sha256(SECRET.encode()).hexdigest().encode(),
        canonical({k: v for k, v in pack.items() if k != "seal"}).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert not run("check", {"package": pack})["valid"]


@pytest.mark.parametrize(
    "locator",
    [
        "../outside.md",
        ".env",
        "credentials.txt",
        "models/a.py",
        "data.pdf",
        "data.sqlite",
        "large.txt",
        "binary.txt",
    ],
)
def test_file_policy_and_failure_status(knowledge, locator):
    run, root, _, _, _ = knowledge
    path = root / locator
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"x" * (MAX_BYTES + 1) if locator == "large.txt" else b"\x00test")
    result = imported(run, locator=locator, groups=None)
    assert result["status"] == "failed"
    assert run("status", {"key": "import-1"}) == result
    assert imported(run, locator=locator, groups=None)["replayed"]


def test_html_no_script_and_unsupported_url(knowledge, monkeypatch):
    run, _, store, _, _ = knowledge
    raw = b"<h1>Receipt</h1><p>Never resend.</p><script>steal()</script>"
    monkeypatch.setattr(
        "tianshu_memory.knowledge.fetch_url",
        lambda *args: (raw, "text/html", "https://example.com/design"),
    )
    result = imported(run, kind="url", locator="https://example.com/design", groups=None)
    assert result["status"] == "imported"
    hit = query(run)["blocks"][0]
    assert "steal" not in hit["text"] and "Never resend" in hit["text"]
    assert hit["provenance"]["citation_space"] == "visible_text_lines"
    with store.transaction() as db:
        assert db.execute("SELECT raw FROM knowledge_versions").fetchone()[0] == raw
    with pytest.raises(Fault):
        fetch_url("https://unregistered.example/", [])
    for url in ["file:///etc/passwd", "http://example.com/", "https://user:pass@example.com/"]:
        with pytest.raises(Fault):
            fetch_url(url, [url])
    with pytest.raises(Fault):
        fetch_url("https://127.0.0.1/", ["https://127.0.0.1/"])
    assert "steal" not in decode(raw, "text/html")


def test_groups_retain_complete_source_and_dependency_closure():
    text = "condition\nnegative\ncause\ncontext\n"
    assert blocks(text, None)[0]["text"] == text
    grouped = blocks(
        text, [dict(start=i + 1, end=i + 1, depends_on=[max(0, i - 1)]) for i in range(4)]
    )
    assert len(grouped) == 1 and all(word in grouped[0]["text"] for word in text.splitlines())
    with pytest.raises(Fault):
        blocks(text, [dict(start=1, end=1, depends_on=[])])


def test_failed_url_refresh_invalidates_previous_snapshot(knowledge, monkeypatch):
    run, _, _, _, _ = knowledge
    monkeypatch.setattr(
        "tianshu_memory.knowledge.fetch_url",
        lambda *a: (b"Receipt retry rules", "text/plain", "https://example.com/design"),
    )
    imported(run, kind="url", locator="https://example.com/design", groups=None)
    pack = run("recover", dict(text="receipt", budget_bytes=8192))

    def unavailable(*args):
        raise OSError("synthetic transport failure")

    monkeypatch.setattr("tianshu_memory.knowledge.fetch_url", unavailable)
    result = imported(
        run,
        key="refresh",
        kind="url",
        locator="https://example.com/design",
        groups=None,
        expected_version=1,
    )
    assert result["status"] == "failed"
    assert not run("check", {"package": pack})["valid"]
    assert not query(run)["blocks"]


def test_static_symlink_escape_is_rejected(knowledge):
    run, root, _, _, _ = knowledge
    outside = root.parent / "outside-dir"
    outside.mkdir()
    (outside / "outside.md").write_text("Private outside source")
    link = root / "linked-dir"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, timeout=10
        )
        assert created.returncode == 0, created.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert imported(run, locator="linked-dir/outside.md", groups=None)["status"] == "failed"


def test_guard_tracks_new_tables_and_restore_is_rejected(knowledge, tmp_path):
    run, _, store, _, _ = knowledge
    with store.transaction() as db:
        triggers = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
        assert all(
            f"source_revision_{table}_{action}" in triggers
            for table in TRACKED
            for action in ["INSERT", "UPDATE", "DELETE"]
        )
    snapshot = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(store.path)) as reader, closing(sqlite3.connect(snapshot)) as dest:
        reader.backup(dest)
    imported(run)
    with closing(sqlite3.connect(snapshot)) as reader, closing(sqlite3.connect(store.path)) as dest:
        reader.backup(dest)
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(store.path)


def test_cli_and_official_sdk_stdio_roundtrip(knowledge):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    run, root, _, path, _ = knowledge
    imported(run)
    action = root.parent / "query.json"
    action.write_text(
        canonical(
            dict(
                operation="query",
                project_id="demo",
                arguments=dict(text="receipt", budget_bytes=8192),
            )
        )
    )
    command = ["-m", "tianshu_memory.knowledge_cli", "--config", str(path)]
    env = dict(os.environ, TEST_KNOWLEDGE_SECRET=SECRET, PYTHONUTF8="1")
    process = subprocess.run(
        [
            sys.executable,
            *command,
            "action",
            "--client",
            "writer",
            "--credential-env",
            "TEST_KNOWLEDGE_SECRET",
            str(action),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["blocks"]

    async def exercise(client):
        server = StdioServerParameters(
            command=sys.executable,
            args=[
                *command,
                "mcp",
                "--client",
                client,
                "--credential-env",
                "TEST_KNOWLEDGE_SECRET",
            ],
            env=env,
            cwd=str(root),
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                # Seven project-knowledge tools, eleven lesson/experience tools of TS-081,
                # the two registered-directory tools of TS-082, the two continuation
                # tools of TS-083 and the seven research-note tools of TS-084.
                assert len(tools.tools) == 29
                names = {tool.name for tool in tools.tools}
                assert {
                    "knowledge_query",
                    "lesson_record",
                    "lesson_query",
                    "experience_promote",
                    "experience_check",
                    "knowledge_directory_scan",
                    "knowledge_directory_apply",
                    "knowledge_continuation_recover",
                    "knowledge_continuation_check",
                    "note_record",
                    "note_revise",
                    "note_withdraw",
                    "note_query",
                    "note_recover",
                    "note_status",
                    "note_check",
                } <= names
                response = await session.call_tool(
                    "knowledge_query",
                    {"project_id": "demo", "text": "receipt", "budget_bytes": 8192},
                )
                assert not response.isError and "Do not resend" in str(response.content)
                if client == "writer":
                    written = await session.call_tool(
                        "knowledge_write_state",
                        {
                            "project_id": "demo",
                            "key": "mcp-state",
                            "expected_version": 0,
                            "state": state(run),
                        },
                    )
                    assert not written.isError and "written" in str(written.content)
                    recovered = await session.call_tool(
                        "knowledge_recover", {"project_id": "demo", "text": "receipt"}
                    )
                    assert not recovered.isError and "Reliable receipt retries" in str(
                        recovered.content
                    )
                denied = await session.call_tool(
                    "knowledge_write_state",
                    {"project_id": "demo", "key": "bad", "expected_version": 0, "state": {}},
                )
                assert denied.isError
                other = await session.call_tool(
                    "knowledge_query", {"project_id": "other", "text": "receipt"}
                )
                assert other.isError

    asyncio.run(exercise("reader"))
    asyncio.run(exercise("writer"))


def test_registration_change_fails_closed_and_config_revoke_is_live(knowledge):
    run, _, _, path, config = knowledge
    imported(run)
    changed = copy.deepcopy(config)
    changed["knowledge"]["projects"]["demo"]["default_branch"] = "other"
    path.write_text(canonical(changed))
    with pytest.raises(Fault, match="registration_changed"):
        query(run)
    config["knowledge"]["clients"]["writer"]["projects"] = []
    path.write_text(canonical(config))
    with pytest.raises(Fault, match="forbidden"):
        query(run)


def test_candidate_schema_and_examples():
    from jsonschema import Draft202012Validator

    directory = Path(__file__).resolve().parents[1] / "docs/candidates/project-knowledge/v1"
    validator = Draft202012Validator(json.loads((directory / "schema.json").read_text()))
    validator.check_schema(validator.schema)
    for example in json.loads((directory / "examples.json").read_text()):
        validator.validate(example)
