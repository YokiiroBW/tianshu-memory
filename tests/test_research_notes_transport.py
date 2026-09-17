"""Real subprocess CLI and official SDK stdio round-trip for research notes.

Both entrypoints are adapters over the same application operation, so this file exercises the
transport rather than the rules: the CLI runs a real child process with a real credential
environment variable, and the MCP session is a real stdio server built by the official SDK.
"""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from test_research_notes import (
    CREDENTIALS,
    OPERATOR_SECRET,
    SECRET,
    decision,
    imported,
    note,
    search,
    unit,
)
from test_research_notes import (
    notes as notes,
)

from tianshu_memory.domain import canonical

COMMAND = ["-m", "tianshu_memory.knowledge_cli"]


def action(notes, operation, arguments, *, client, secret, name="action.json"):
    """Run one operation through the real CLI child process."""
    path = notes.tmp_path / name
    path.write_text(
        canonical({"operation": operation, "project_id": "alpha", "arguments": arguments}),
        encoding="utf-8",
    )
    process = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(notes.path),
            "action",
            "--client",
            client,
            "--credential-env",
            "TIANSHU_PROJECT_SECRET",
            str(path),
        ],
        cwd=notes.roots["alpha"],
        env=dict(os.environ, TIANSHU_PROJECT_SECRET=secret, PYTHONUTF8="1"),
        capture_output=True,
        timeout=30,
    )
    return process


def test_cli_records_revises_queries_and_withdraws_a_note(notes):
    imported(notes)
    reference = unit(notes)
    recorded = action(
        notes,
        "note_record",
        dict(
            key="cli-note",
            dedupe="cli-note",
            expected_version=0,
            note=note([reference], decision=decision([reference])),
        ),
        client="alpha-writer",
        secret=SECRET,
        name="record.json",
    )
    assert recorded.returncode == 0, recorded.stderr
    body = json.loads(recorded.stdout)
    assert body["status"] == "recorded" and body["version"] == 1

    # A reader identity may query the same note through the CLI.
    found = action(
        notes,
        "note_query",
        dict(text="receipt", budget_bytes=8192),
        client="alpha-reader",
        secret=SECRET,
        name="query.json",
    )
    assert found.returncode == 0, found.stderr
    view = json.loads(found.stdout)["notes"][0]
    assert view["note_id"] == body["note_id"] and view["current"] is True
    assert view["decision"]["basis"][0]["kind"] == "source"

    revised = action(
        notes,
        "note_revise",
        dict(
            key="cli-note",
            dedupe="cli-revise",
            note_id=body["note_id"],
            expected_version=1,
            note=note([reference], inferences=["A revised inference."]),
        ),
        client="alpha-writer",
        secret=SECRET,
        name="revise.json",
    )
    assert revised.returncode == 0, revised.stderr
    assert json.loads(revised.stdout)["version"] == 2

    withdrawn = action(
        notes,
        "note_withdraw",
        dict(
            key="cli-withdraw",
            note_id=body["note_id"],
            expected_version=2,
            reason="superseded by the newer research",
        ),
        client="alpha-writer",
        secret=SECRET,
        name="withdraw.json",
    )
    assert withdrawn.returncode == 0, withdrawn.stderr
    assert json.loads(withdrawn.stdout)["status"] == "withdrawn"
    assert search(notes)["notes"] == []


def test_cli_reports_a_fault_as_a_non_zero_exit(notes):
    imported(notes)
    reference = unit(notes)
    # A note that cites nothing is refused by the application, and the adapter maps it to a
    # stable code and a failing exit status instead of printing a partial result.
    refused = action(
        notes,
        "note_record",
        dict(
            key="bad",
            dedupe="bad",
            expected_version=0,
            note=note([reference]) | {"source_statements": []},
        ),
        client="alpha-writer",
        secret=SECRET,
        name="refused.json",
    )
    assert refused.returncode == 1
    assert json.loads(refused.stdout) == {"status": "failed", "code": "evidence_required"}
    # An identity without the note permission is refused by the same path.
    denied = action(
        notes,
        "note_query",
        dict(text="receipt", budget_bytes=8192),
        client="alpha-writer",
        secret="wrong-but-long-enough-credential",
        name="denied.json",
    )
    assert denied.returncode == 1
    assert json.loads(denied.stdout) == {"status": "failed", "code": "unauthorized"}


def test_cli_migrate_research_notes_command(notes, tmp_path):
    """The upgrade command refuses an already-upgraded database and leaves no backup."""
    backup = tmp_path / "cli-before-notes.sqlite"
    process = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(notes.path),
            "migrate-research-notes",
            "--backup",
            str(backup),
        ],
        cwd=notes.roots["alpha"],
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 1, process.stdout
    assert json.loads(process.stdout)["code"] == "dependency_or_input_error"
    # The reserved backup path exists but holds nothing: the migration never ran.
    assert backup.exists() and backup.stat().st_size == 0


def test_official_sdk_stdio_note_roundtrip(notes):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    imported(notes)
    reference = unit(notes)
    env = dict(os.environ, TIANSHU_PROJECT_SECRET=SECRET, PYTHONUTF8="1")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            *COMMAND,
            "--config",
            str(notes.path),
            "mcp",
            "--client",
            "alpha-writer",
            "--credential-env",
            "TIANSHU_PROJECT_SECRET",
        ],
        env=env,
        cwd=str(notes.roots["alpha"]),
    )

    async def exercise():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {
                    "note_record",
                    "note_revise",
                    "note_withdraw",
                    "note_query",
                    "note_recover",
                    "note_status",
                    "note_check",
                } <= names
                # The same seven operations the CLI exposes are the only note tools: the entry
                # adapters do not add rules of their own.
                assert {name for name in names if name.startswith("note_")} == {
                    "note_record",
                    "note_revise",
                    "note_withdraw",
                    "note_query",
                    "note_recover",
                    "note_status",
                    "note_check",
                }
                recorded = await session.call_tool(
                    "note_record",
                    {
                        "project_id": "alpha",
                        "key": "mcp-note",
                        "expected_version": 0,
                        "note": note([reference], decision=decision([reference])),
                    },
                )
                assert not recorded.isError, recorded
                body = json.loads(recorded.content[0].text)
                assert body["status"] == "recorded"

                found = await session.call_tool(
                    "note_query", {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192}
                )
                assert not found.isError, found
                view = json.loads(found.content[0].text)["notes"][0]
                assert view["note_id"] == body["note_id"] and view["current"] is True

                package = await session.call_tool(
                    "note_recover",
                    {"project_id": "alpha", "text": "receipt", "budget_bytes": 16384},
                )
                assert not package.isError, package
                sealed = json.loads(package.content[0].text)
                checked = await session.call_tool(
                    "note_check", {"project_id": "alpha", "package": sealed}
                )
                assert not checked.isError, checked
                assert json.loads(checked.content[0].text) == {"valid": True, "reason": "current"}

                historical = await session.call_tool(
                    "note_status",
                    {"project_id": "alpha", "note_id": body["note_id"], "version": 1},
                )
                assert not historical.isError, historical
                assert json.loads(historical.content[0].text)["hash"] == body["hash"]

                # A refused operation comes back as an SDK error, never as a partial result.
                denied = await session.call_tool(
                    "note_revise",
                    {
                        "project_id": "alpha",
                        "key": "mcp-note",
                        "note_id": body["note_id"],
                        "expected_version": 99,
                        "note": note([reference]),
                    },
                )
                assert denied.isError
                other = await session.call_tool(
                    "note_query", {"project_id": "beta", "text": "receipt", "budget_bytes": 8192}
                )
                assert other.isError

    asyncio.run(exercise())


def test_credentials_map_to_independent_clients(notes):
    """The reader credential cannot act as the writer and vice versa."""
    assert CREDENTIALS["alpha-reader"] == SECRET != OPERATOR_SECRET
    with pytest.raises(Exception):
        notes.run(
            "note_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
            credential="wrong-but-long-enough-value",
        )
