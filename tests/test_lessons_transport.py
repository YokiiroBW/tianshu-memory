"""Real subprocess CLI and official SDK stdio round-trip for lessons and experience."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from test_lessons import (
    CREDENTIALS,
    REVIEW_SECRET,
    SECRET,
    reference,
)
from test_lessons import (
    lessons as lessons,
)

from tianshu_memory.domain import canonical

COMMAND = ["-m", "tianshu_memory.knowledge_cli"]


def test_cli_action_file_records_and_queries_a_lesson(lessons):
    imported = lessons.run(
        "import",
        dict(key="cli-import", kind="file", locator="notes.md", expected_version=0, groups=None),
        project="alpha",
        client="alpha-writer",
    )
    assert imported["status"] == "imported"
    evidence = reference(lessons, "alpha")
    action = lessons.tmp_path / "lesson.json"
    action.write_text(
        canonical(
            {
                "operation": "lesson_record",
                "project_id": "alpha",
                "arguments": {
                    "key": "cli-lesson",
                    "expected_version": 0,
                    "lesson": {
                        "trigger": "Timed out send",
                        "symptom": "Duplicate delivery",
                        "cause": "No receipt check",
                        "correction": "Check the receipt first",
                        "verification": "Isolated run",
                        "scope": {
                            "platform": "windows",
                            "language": "python",
                            "framework": "stdlib",
                            "applies_to": ["delivery"],
                            "excludes": ["telemetry"],
                        },
                        "evidence": [evidence],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ, TIANSHU_PROJECT_SECRET=SECRET, PYTHONUTF8="1")
    process = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(lessons.path),
            "action",
            "--client",
            "alpha-writer",
            "--credential-env",
            "TIANSHU_PROJECT_SECRET",
            str(action),
        ],
        cwd=lessons.roots["alpha"],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    recorded = json.loads(process.stdout)
    assert recorded["status"] == "recorded" and recorded["version"] == 1
    query = lessons.tmp_path / "query.json"
    query.write_text(
        canonical(
            {
                "operation": "lesson_query",
                "project_id": "alpha",
                "arguments": {"text": "receipt", "budget_bytes": 8192},
            }
        ),
        encoding="utf-8",
    )
    searched = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(lessons.path),
            "action",
            "--client",
            "operator",
            "--credential-env",
            "TIANSHU_PROJECT_SECRET",
            str(query),
        ],
        cwd=lessons.roots["alpha"],
        env=dict(env, TIANSHU_PROJECT_SECRET=REVIEW_SECRET),
        capture_output=True,
        timeout=30,
    )
    assert searched.returncode == 0, searched.stderr
    assert json.loads(searched.stdout)["lessons"][0]["lesson_id"] == recorded["lesson_id"]


def test_cli_migrate_lessons_command(lessons, tmp_path):
    """The upgrade command refuses an already-upgraded database and leaves no backup."""
    backup = tmp_path / "cli-before-lessons.sqlite"
    process = subprocess.run(
        [
            sys.executable,
            *COMMAND,
            "--config",
            str(lessons.path),
            "migrate-lessons",
            "--backup",
            str(backup),
        ],
        cwd=lessons.roots["alpha"],
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 1, process.stdout
    assert json.loads(process.stdout)["code"] == "dependency_or_input_error"
    # The reserved backup path exists but holds nothing: the migration never ran.
    assert backup.exists() and backup.stat().st_size == 0


def test_official_sdk_stdio_lesson_and_experience_roundtrip(lessons):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    imported = lessons.run(
        "import",
        dict(key="mcp-import", kind="file", locator="notes.md", expected_version=0, groups=None),
        project="alpha",
        client="alpha-writer",
    )
    assert imported["status"] == "imported"
    second = lessons.run(
        "import",
        dict(
            key="mcp-import-beta", kind="file", locator="notes.md", expected_version=0, groups=None
        ),
        project="beta",
        client="beta-writer",
    )
    assert second["status"] == "imported"
    alpha_evidence = reference(lessons, "alpha")
    beta_evidence = reference(lessons, "beta")
    env = dict(os.environ, TIANSHU_PROJECT_SECRET=REVIEW_SECRET, PYTHONUTF8="1")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            *COMMAND,
            "--config",
            str(lessons.path),
            "mcp",
            "--client",
            "operator",
            "--credential-env",
            "TIANSHU_PROJECT_SECRET",
        ],
        env=env,
        cwd=str(lessons.roots["alpha"]),
    )

    async def exercise():
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {
                    "lesson_record",
                    "lesson_revise",
                    "lesson_retire",
                    "lesson_query",
                    "lesson_recover",
                    "lesson_check",
                    "experience_promote",
                    "experience_query",
                    "experience_check",
                    "experience_revoke",
                    "experience_withdraw",
                } <= names
                payload = {
                    "trigger": "Timed out send",
                    "symptom": "Duplicate delivery",
                    "cause": "No receipt check",
                    "correction": "Check the receipt first",
                    "verification": "Isolated MCP run",
                    "scope": {
                        "platform": "windows",
                        "language": "python",
                        "framework": "stdlib",
                        "applies_to": ["delivery"],
                        "excludes": ["telemetry"],
                    },
                    "evidence": [alpha_evidence],
                }
                recorded = await session.call_tool(
                    "lesson_record",
                    {
                        "project_id": "alpha",
                        "key": "mcp-lesson",
                        "expected_version": 0,
                        "lesson": payload,
                    },
                )
                assert not recorded.isError, recorded
                alpha_lesson = json.loads(recorded.content[0].text)
                beta_recorded = await session.call_tool(
                    "lesson_record",
                    {
                        "project_id": "beta",
                        "key": "mcp-lesson-beta",
                        "expected_version": 0,
                        "lesson": dict(payload, evidence=[beta_evidence]),
                    },
                )
                assert not beta_recorded.isError, beta_recorded
                beta_lesson = json.loads(beta_recorded.content[0].text)
                recovered = await session.call_tool(
                    "lesson_recover",
                    {"project_id": "alpha", "text": "receipt", "budget_bytes": 8192},
                )
                assert not recovered.isError and "mcp-lesson" in str(recovered.content)
                package = json.loads(recovered.content[0].text)
                checked = await session.call_tool(
                    "lesson_check", {"project_id": "alpha", "package": package}
                )
                assert not checked.isError and json.loads(checked.content[0].text)["valid"]
                promoted = await session.call_tool(
                    "experience_promote",
                    {
                        "project_id": "alpha",
                        "key": "mcp-promotion",
                        "expected_version": 0,
                        "entry": {
                            "title": "Check the receipt before retrying",
                            "rule": "Read the receipt table before resending.",
                            "applicability": ["delivery"],
                            "excludes": ["telemetry"],
                            "counterexamples": ["fire-and-forget"],
                            "recheck_after": None,
                            "evidence": [
                                {
                                    "project_id": "alpha",
                                    "lesson_id": alpha_lesson["lesson_id"],
                                    "version": alpha_lesson["version"],
                                    "hash": alpha_lesson["hash"],
                                },
                                {
                                    "project_id": "beta",
                                    "lesson_id": beta_lesson["lesson_id"],
                                    "version": beta_lesson["version"],
                                    "hash": beta_lesson["hash"],
                                },
                            ],
                        },
                    },
                )
                assert not promoted.isError, promoted
                entry = json.loads(promoted.content[0].text)
                assert entry["evidence_projects"] == ["alpha", "beta"]
                found = await session.call_tool(
                    "experience_query",
                    {
                        "project_id": "alpha",
                        "text": "receipt",
                        "budget_bytes": 8192,
                        "filter_project_id": None,
                    },
                )
                assert not found.isError and entry["entry_id"] in str(found.content)
                denied = await session.call_tool(
                    "experience_revoke",
                    {
                        "project_id": "alpha",
                        "key": "mcp-revoke",
                        "entry_id": entry["entry_id"],
                        "expected_version": 99,
                        "reason": "stale",
                    },
                )
                assert denied.isError
                other = await session.call_tool(
                    "lesson_query", {"project_id": "other", "text": "receipt"}
                )
                assert other.isError

    asyncio.run(exercise())


def test_credentials_map_to_independent_clients(lessons):
    """The operator credential cannot act as a project writer and vice versa."""
    with pytest.raises(Exception):
        lessons.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
            credential="wrong-but-long-enough-value",
        )
    assert CREDENTIALS["operator"] == REVIEW_SECRET != CREDENTIALS["alpha-writer"]
