"""Opt-in HTTP reads use existing knowledge rules and real isolated processes."""

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from test_knowledge_continuation import SECRET as CONTINUATION_SECRET
from test_knowledge_continuation import checkouts as checkouts
from test_knowledge_continuation import git_path
from test_knowledge_http_process import ACTION, serving
from test_lessons import CREDENTIALS, REVIEW_SECRET, promote, shared_references, two_projects
from test_lessons import lessons as lessons

from tianshu_memory.domain import canonical
from tianshu_memory.knowledge_http import create_app


@pytest.fixture(autouse=True)
def git_for_continuation(monkeypatch):
    monkeypatch.setenv("TIANSHU_GIT", git_path())


def send(client, operation, arguments, project="alpha"):
    return client.post(
        ACTION,
        json={"operation": operation, "project_id": project, "arguments": arguments},
    )


def save(harness):
    harness.path.write_text(canonical(harness.config), encoding="utf-8")


def test_lesson_and_experience_require_separate_http_and_domain_permissions(lessons):
    two_projects(lessons)
    promoted = promote(lessons, shared_references(lessons))
    lesson_args = {"text": "receipt", "budget_bytes": 8192}
    experience_args = {"text": "receipt", "budget_bytes": 8192, "project_id": None}
    with serving(lessons, client="operator", credential=REVIEW_SECRET) as (client, _):
        # The old operation permissions alone do not enable a new HTTP path.
        for operation, arguments in (
            ("lesson_query", lesson_args),
            ("experience_query", experience_args),
        ):
            response = send(client, operation, arguments)
            assert response.status_code == 415 and response.json()["code"] == "unsupported"
        lessons.config["knowledge"]["clients"]["operator"]["http_read_operations"] = [
            "lesson_query",
            "experience_query",
        ]
        save(lessons)
        found = send(client, "lesson_query", lesson_args)
        assert found.status_code == 200, found.text
        assert found.json()["lessons"]
        experience = send(client, "experience_query", experience_args)
        assert experience.status_code == 200, experience.text
        assert [item["entry_id"] for item in experience.json()["entries"]] == [promoted["entry_id"]]
        assert found.headers["cache-control"] == "no-store"
        # The existing domain budget omits an oversized complete lesson instead of truncating it.
        small = send(client, "lesson_query", {"text": "receipt", "budget_bytes": 256})
        assert small.status_code == 200 and small.json()["lessons"] == []
        assert "budget" in small.json()["omissions"]
        # Removing only the HTTP grant takes effect on the next request in this live process.
        lessons.config["knowledge"]["clients"]["operator"]["http_read_operations"] = []
        save(lessons)
        denied = send(client, "experience_query", experience_args)
        assert denied.status_code == 415 and "entries" not in denied.json()

    # An HTTP grant plus the operation permission does not imply global review authority.
    writer = lessons.config["knowledge"]["clients"]["alpha-writer"]
    writer["http_read_operations"] = ["experience_query"]
    writer["permissions"].append("experience_query")
    save(lessons)
    with serving(lessons, client="alpha-writer", credential=CREDENTIALS["alpha-writer"]) as (
        client,
        _,
    ):
        denied = send(client, "experience_query", experience_args)
        assert denied.status_code == 422 and denied.json()["code"] == "forbidden"

    # A reviewer of alpha cannot see an experience whose evidence also belongs to beta.
    reviewer = lessons.config["knowledge"]["clients"]["reviewer"]
    reviewer["http_read_operations"] = ["experience_query"]
    save(lessons)
    with serving(lessons, client="reviewer", credential=CREDENTIALS["reviewer"]) as (client, _):
        hidden = send(client, "experience_query", experience_args)
        assert hidden.status_code == 200 and hidden.json()["entries"] == []


def test_continuation_http_only_registered_alias_and_no_persisted_action(checkouts):
    checkouts.config["knowledge"]["clients"]["writer"]["http_read_operations"] = [
        "continuation_recover",
        "continuation_check",
    ]
    save(checkouts)
    with checkouts.store.transaction() as db:
        before = db.execute("SELECT COUNT(*) FROM knowledge_operations").fetchone()[0]
    with serving(checkouts, client="writer", credential=CONTINUATION_SECRET) as (client, _):
        recovered = send(
            client,
            "continuation_recover",
            {"worktree": "agent-a", "text": "receipt", "budget_bytes": 16384},
            project="demo",
        )
        assert recovered.status_code == 200, recovered.text
        package = recovered.json()
        assert package["status"] == "recovered" and package["worktree"]["id"] == "agent-a"
        assert str(checkouts.first) not in recovered.text
        checked = send(client, "continuation_check", {"package": package}, project="demo")
        assert checked.status_code == 200, checked.text
        assert checked.json()["valid"] is True
        # A browser cannot supply a filesystem path in place of a registered checkout alias.
        raw_path = send(
            client,
            "continuation_recover",
            {"worktree": str(checkouts.first), "text": "receipt", "budget_bytes": 16384},
            project="demo",
        )
        assert raw_path.status_code == 422 and raw_path.json()["status"] == "failed"
    with checkouts.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_operations").fetchone()[0] == before


def test_lesson_http_result_matches_existing_cli(lessons, tmp_path):
    two_projects(lessons)
    lessons.config["knowledge"]["clients"]["operator"]["http_read_operations"] = ["lesson_query"]
    save(lessons)
    arguments = {"text": "receipt", "budget_bytes": 8192}
    with serving(lessons, client="operator", credential=REVIEW_SECRET) as (client, _):
        http_result = send(client, "lesson_query", arguments)
        assert http_result.status_code == 200, http_result.text
    action_file = tmp_path / "lesson-query.json"
    action_file.write_text(
        canonical({"operation": "lesson_query", "project_id": "alpha", "arguments": arguments}),
        encoding="utf-8",
    )
    env = dict(os.environ, TIANSHU_KNOWLEDGE_TEST_CREDENTIAL=REVIEW_SECRET, PYTHONUTF8="1")
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianshu_memory.knowledge_cli",
            "--config",
            str(lessons.path),
            "action",
            "--client",
            "operator",
            "--credential-env",
            "TIANSHU_KNOWLEDGE_TEST_CREDENTIAL",
            str(action_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == http_result.json()


def test_http_grant_revoked_during_read_drops_result(lessons):
    two_projects(lessons)
    lessons.config["knowledge"]["clients"]["operator"]["http_read_operations"] = ["lesson_query"]
    save(lessons)
    app = create_app(lessons.path, "operator", 18139)

    def revoke_after_domain_read():
        lessons.config["knowledge"]["clients"]["operator"]["http_read_operations"] = []
        save(lessons)

    app.state.settle = revoke_after_domain_read
    with TestClient(app, base_url="http://127.0.0.1:18139") as client:
        response = client.post(
            ACTION,
            json={
                "operation": "lesson_query",
                "project_id": "alpha",
                "arguments": {"text": "receipt", "budget_bytes": 8192},
            },
            headers={"Authorization": f"Bearer {REVIEW_SECRET}"},
        )
    assert response.status_code == 415 and response.json() == {
        "status": "failed",
        "code": "unsupported",
    }
    assert "lessons" not in response.text
