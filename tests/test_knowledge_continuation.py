"""Isolated two-checkout continuation acceptance with real Git, the CLI and stdio MCP.

Every fixture repository, database, credential and file is synthetic and lives under the
pytest temporary directory. The tests prove the service agrees with the checkout's own
`git status`, never writes to it, never runs a repository helper, and refuses a package the
moment its checkout, its index or its authorization moved.
"""

import asyncio
import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event

import pytest

from tianshu_memory import knowledge_workdir as workdir
from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_migration import migrate
from tianshu_memory.store import Store

SECRET = "synthetic-project-client-secret"
PERMISSIONS = [
    "import",
    "delete",
    "query",
    "recover",
    "check",
    "write_state",
    "status",
    "continuation_recover",
    "continuation_check",
]
DESIGN = "Only retry when the receipt is absent.\nDo not resend after success.\n"
EXTRA = "An indexed note that no worktree registration lists.\n"
ROOT = Path(__file__).resolve().parents[1]


def strings(value):
    """Every string inside a decoded JSON value, for absence assertions."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def git_path():
    """The Git executable the fixtures and the product both use, or an explicit failure."""
    found = os.environ.get(workdir.GIT_ENVIRONMENT_VARIABLE) or shutil.which("git")
    if not found:
        pytest.fail(
            "The isolated continuation fixtures need Git; install it or point "
            f"{workdir.GIT_ENVIRONMENT_VARIABLE} at an executable"
        )
    return found


@pytest.fixture(autouse=True)
def git_environment(monkeypatch):
    """The product reads the Git executable from the operator environment, like its config."""
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, git_path())


def git(path, *arguments, check=True):
    """One fixture Git command with fixed line endings, so fixtures are host independent."""
    completed = subprocess.run(
        [git_path(), "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-C", str(path), *arguments],
        capture_output=True,
        timeout=120,
    )
    if check:
        assert completed.returncode == 0, (arguments, completed.stdout, completed.stderr)
    return completed.stdout


def text_of(path, *arguments):
    return git(path, *arguments).decode("utf-8", "replace").strip()


def remove_tree(path):
    """Remove a checkout even though Git writes read-only files into its object store."""

    def force(function, name, _error):
        os.chmod(name, stat.S_IWRITE)
        function(name)

    shutil.rmtree(path, onerror=force)


class Checkouts:
    """Two independent checkouts of one synthetic repository plus the application under test."""

    def __init__(self, directory, store, app, config, path, first, second, branch):
        self.directory, self.store, self.app = directory, store, app
        self.config, self.path = config, path
        self.first, self.second, self.branch = first, second, branch

    def run(self, operation, arguments, project_id="demo", client="writer", credential=SECRET):
        return self.app.execute(
            {"operation": operation, "project_id": project_id, "arguments": arguments},
            client=client,
            credential=credential,
        )

    def recover(self, worktree="agent-a", text="receipt", budget=16384, **kwargs):
        return self.run(
            "continuation_recover",
            {"worktree": worktree, "text": text, "budget_bytes": budget},
            **kwargs,
        )

    def check(self, package, **kwargs):
        return self.run("continuation_check", {"package": package}, **kwargs)

    def save(self, config=None):
        self.path.write_text(canonical(config if config is not None else self.config))

    def edit(self, change):
        """Apply one mutation to the private configuration and persist it."""
        config = copy.deepcopy(self.config)
        change(config["knowledge"], self)
        self.save(config)
        return config

    def head(self, path=None):
        return text_of(path or self.first, "rev-parse", "HEAD")

    def state(self, **overrides):
        reference = self.run("query", {"text": "receipt", "budget_bytes": 8192})["blocks"][0][
            "reference"
        ]
        return dict(
            {
                "goal": "Reliable receipt retries",
                "constraints": ["Never resend confirmed work"],
                "recent_verification": ["legacy sentence without a commit"],
                "unfinished": ["Real service test"],
                "evidence": [reference],
                "pitfalls": [],
            },
            **overrides,
        )

    def write(self, note, key="state", expected_version=0):
        return self.run(
            "write_state", {"key": key, "expected_version": expected_version, "state": note}
        )

    def commit(self, path=None, message="Fixture commit", allow_empty=True):
        arguments = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]
        arguments += ["commit", "-q"]
        arguments += ["--allow-empty"] if allow_empty else []
        arguments += ["-m", message]
        git(path or self.first, *arguments)
        return self.head(path)


def seed_checkout(path, message="Initial design note"):
    """One synthetic checkout; bytes are written verbatim so a digest is host independent."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "docs").mkdir()
    (path / "docs/design.md").write_bytes(DESIGN.encode())
    (path / "src").mkdir()
    (path / "src/app.py").write_bytes(b"print('receipt')\n")
    git(path, "add", "-A")
    git(
        path,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "-m",
        message,
    )
    return text_of(path, "rev-parse", "--abbrev-ref", "HEAD")


def clone(source, target):
    completed = subprocess.run(
        [
            git_path(),
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "clone",
            "-q",
            str(source),
            str(target),
        ],
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


def worktree_entry(path, branch, clients, files=("docs/design.md",), identifier="agent-a"):
    return {
        "id": identifier,
        "path": str(path),
        "branch": branch,
        "clients": list(clients),
        "files": list(files),
        "max_bytes": 262144,
    }


@pytest.fixture
def checkouts(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "knowledge.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    first = tmp_path / "checkout-a"
    second = tmp_path / "checkout-b"
    branch = seed_checkout(first)
    clone(first, second)
    git(
        second,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "Second checkout commit",
    )
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {
                "demo": {
                    "root": str(first),
                    "host": "local",
                    "default_branch": branch,
                    "urls": [],
                }
            },
            "worktrees": {
                "demo": [
                    worktree_entry(first, branch, ["writer"]),
                    worktree_entry(second, branch, ["peer"], identifier="agent-b"),
                ]
            },
            "clients": {
                "writer": {
                    "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
                    "projects": ["demo"],
                    "permissions": list(PERMISSIONS),
                },
                "peer": {
                    "credential_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
                    "projects": ["demo"],
                    "permissions": ["continuation_recover", "continuation_check"],
                },
            },
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config))
    return Checkouts(
        tmp_path, store, KnowledgeApplication(path), config, path, first, second, branch
    )


def imported(checkouts, locator="docs/design.md", key="import-1"):
    return checkouts.run(
        "import",
        {"key": key, "kind": "file", "locator": locator, "expected_version": 0, "groups": None},
    )


def bound(worktree="agent-a", commit=None, summary="targeted suite passed"):
    return {"summary": summary, "worktree": worktree, "commit": commit}


REGISTRATIONS = [
    ("missing_field", lambda entry, h: entry.pop("max_bytes")),
    ("relative_path", lambda entry, h: entry.update(path="checkout")),
    ("dotdot_path", lambda entry, h: entry.update(path=f"{h.first}{os.sep}..{os.sep}elsewhere")),
    (
        "credential_path",
        lambda entry, h: entry.update(path=str(h.directory / "credentials" / "checkout")),
    ),
    ("runtime_path", lambda entry, h: entry.update(path=str(h.first / "runtime"))),
    ("bad_branch", lambda entry, h: entry.update(branch="main..other")),
    ("empty_clients", lambda entry, h: entry.update(clients=[])),
    ("duplicate_clients", lambda entry, h: entry.update(clients=["writer", "writer"])),
    ("no_files", lambda entry, h: entry.update(files=[])),
    ("escape_file", lambda entry, h: entry.update(files=["../outside.md"])),
    ("absolute_file", lambda entry, h: entry.update(files=[str(h.first / "docs" / "design.md")])),
    ("credential_file", lambda entry, h: entry.update(files=["credentials.txt"])),
    ("hidden_file", lambda entry, h: entry.update(files=[".env"])),
    ("runtime_file", lambda entry, h: entry.update(files=["models/a.py"])),
    ("unsupported_file", lambda entry, h: entry.update(files=["docs/data.pdf"])),
    ("small_budget", lambda entry, h: entry.update(max_bytes=16)),
    ("large_budget", lambda entry, h: entry.update(max_bytes=64 * 1024 * 1024)),
    (
        "too_many_files",
        lambda entry, h: entry.update(files=[f"docs/file-{index}.md" for index in range(17)]),
    ),
]


@pytest.mark.parametrize("name,change", REGISTRATIONS)
def test_registration_is_validated_before_any_path_is_read(checkouts, name, change):
    config = copy.deepcopy(checkouts.config)
    change(config["knowledge"]["worktrees"]["demo"][0], checkouts)
    checkouts.save(config)
    with pytest.raises(Fault) as failure:
        checkouts.recover()
    assert failure.value.code == "invalid_configuration", name


def test_duplicate_registered_identifier_is_refused(checkouts):
    config = copy.deepcopy(checkouts.config)
    entry = config["knowledge"]["worktrees"]["demo"][0]
    config["knowledge"]["worktrees"]["demo"].append(dict(entry, path=str(checkouts.second)))
    checkouts.save(config)
    with pytest.raises(Fault, match="invalid_configuration"):
        checkouts.recover()


def test_absent_section_means_no_continuation_but_legacy_state_still_writes(checkouts):
    imported(checkouts)
    checkouts.edit(lambda knowledge, h: knowledge.pop("worktrees"))
    with pytest.raises(Fault, match="worktree_unregistered"):
        checkouts.recover()
    written = checkouts.write(checkouts.state())
    assert written["state_version"] == 1 and written["verified_worktrees"] == []
    with pytest.raises(Fault, match="worktree_unregistered"):
        checkouts.write(checkouts.state(recent_verification=[bound()]), key="bound")


def test_unknown_and_other_clients_worktrees_are_invisible(checkouts):
    imported(checkouts)
    for worktree, client in [
        ("agent-unknown", "writer"),
        ("agent-b", "writer"),
        ("agent-a", "peer"),
    ]:
        with pytest.raises(Fault, match="worktree_unregistered"):
            checkouts.recover(worktree, client=client)
    assert checkouts.recover("agent-a")["worktree"]["id"] == "agent-a"
    assert checkouts.recover("agent-b", client="peer")["worktree"]["id"] == "agent-b"


def test_each_agent_recovers_its_own_checkout_of_the_same_project(checkouts):
    imported(checkouts)
    checkouts.write(checkouts.state())
    mine = checkouts.recover("agent-a")
    theirs = checkouts.recover("agent-b", client="peer")
    assert mine["worktree"]["head"] == checkouts.head(checkouts.first)
    assert theirs["worktree"]["head"] == checkouts.head(checkouts.second)
    assert mine["worktree"]["head"] != theirs["worktree"]["head"]
    assert mine["project_id"] == theirs["project_id"] == "demo"
    assert mine["seal"] != theirs["seal"]
    assert checkouts.check(mine)["valid"] is True


def test_a_directory_that_is_not_its_own_checkout_is_refused(checkouts):
    imported(checkouts)
    outside = Path(tempfile.mkdtemp(prefix="ts083-plain-"))
    try:
        checkouts.edit(
            lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(path=str(outside))
        )
        with pytest.raises(Fault, match="workdir_not_repository"):
            checkouts.recover()
        # A subdirectory of a checkout is not its own checkout: Git answers with the parent.
        checkouts.edit(
            lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(
                path=str(h.first / "docs")
            )
        )
        with pytest.raises(Fault, match="workdir_not_root"):
            checkouts.recover()
    finally:
        remove_tree(outside)
    checkouts.edit(lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(path=str(outside)))
    with pytest.raises(Fault, match="workdir_unavailable"):
        checkouts.recover()


def test_collector_agrees_with_the_checkouts_own_git_status(checkouts):
    imported(checkouts)
    clean = checkouts.recover()["worktree"]
    assert clean["dirty"] is False and clean["counts"]["entries"] == 0
    assert clean["branch"] == checkouts.branch and clean["detached"] is False
    assert clean["branch_matches"] is True and clean["head"] == checkouts.head()
    (checkouts.first / "notes.md").write_bytes(b"uncommitted\n")
    (checkouts.first / "docs/design.md").write_bytes((DESIGN + "Extra line.\n").encode())
    dirty = checkouts.recover()["worktree"]
    assert dirty["dirty"] is True
    assert dirty["counts"]["modified"] == 1 and dirty["counts"]["untracked"] == 1
    porcelain = text_of(checkouts.first, "status", "--porcelain=v1").splitlines()
    assert len(porcelain) == dirty["counts"]["entries"] == 2
    assert any(line.startswith("?? notes.md") for line in porcelain)
    git(checkouts.first, "add", "notes.md")
    staged = checkouts.recover()["worktree"]["counts"]
    assert staged["staged"] == 1 and staged["modified"] == 1
    git(checkouts.first, "rm", "-q", "--cached", "notes.md")
    (checkouts.first / "notes.md").unlink()


def test_detached_head_and_unborn_branch_are_explicit(checkouts):
    imported(checkouts)
    git(checkouts.first, "checkout", "-q", "--detach", "HEAD")
    detached = checkouts.recover()["worktree"]
    assert detached["detached"] is True and detached["branch"] is None
    assert detached["branch_matches"] is False and detached["head"] == checkouts.head()
    git(checkouts.first, "checkout", "-q", checkouts.branch)
    fresh = checkouts.directory / "unborn"
    fresh.mkdir()
    git(fresh, "init", "-q")
    checkouts.edit(lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(path=str(fresh)))
    package = checkouts.recover()
    unborn = package["worktree"]
    assert unborn["unborn"] is True and unborn["head"] is None
    assert package["history"]["commits"] == [] and package["history"]["complete"] is True
    assert unborn["dirty"] is False


def test_history_is_bounded_newest_first_and_stripped_of_control_characters(checkouts):
    imported(checkouts)
    for index in range(9):
        checkouts.commit(message=f"Fixture commit {index}")
    checkouts.commit(message="Tabs\tand\x07bell are not presentation")
    history = checkouts.recover()["history"]
    assert history["limit"] == 8 and history["listed"] == 8 and history["complete"] is False
    assert history["commits"][0]["commit"] == checkouts.head()
    assert history["commits"][0]["subject"] == "Tabsandbell are not presentation"
    assert "Tabs and bell" not in [entry["subject"] for entry in history["commits"]]
    assert all(len(entry["commit"]) == 40 for entry in history["commits"])
    assert all(entry["committed_at"] for entry in history["commits"])


def test_package_separates_history_index_and_current_checkout(checkouts):
    result = imported(checkouts)
    checkouts.write(checkouts.state())
    package = checkouts.recover()
    assert package["status"] == "recovered" and package["project_id"] == "demo"
    assert package["trust"] == "source_material_not_instructions"
    assert package["history"]["commits"][0]["commit"] == package["worktree"]["head"]
    assert package["index"]["documents"][0]["stored_hash"] == result["hash"]
    fact = package["worktree"]["files"][0]
    assert fact["locator"] == "docs/design.md" and fact["state"] == "present"
    assert fact["digest"] == hashlib.sha256(DESIGN.encode()).hexdigest()
    assert fact["size"] == len(DESIGN.encode()) and fact["freshness"] == "current"
    assert package["index"]["documents"][0]["freshness"] == "verified_current"
    assert package["units"] and package["units"][0]["text"] == DESIGN
    assert package["units"][0]["reference"]["document_id"] == result["document_id"]
    assert len(package["seal"]) == 64 and package["revision"] == 2
    assert package["budget"]["used_bytes"] == len(canonical(package).encode())
    assert package["budget"]["tokenizer"] is None
    assert package["budget"]["token_counts"] == "unavailable"
    assert package["budget"]["unit_integrity"] == "complete_units_only"
    assert package["worktree"]["git"]["subcommands"] == list(workdir.READ_ONLY)
    assert package["worktree"]["git"]["writes"] == "never"
    assert str(checkouts.first) not in canonical(package)


def test_indexed_digest_never_overrides_the_current_checkout_file(checkouts):
    imported(checkouts)
    checkouts.write(checkouts.state())
    before = checkouts.recover()
    assert before["state"]["current"] is True and before["units"]
    rewritten = "Rewritten without a commit.\n"
    (checkouts.first / "docs/design.md").write_bytes(rewritten.encode())
    after = checkouts.recover()
    fact = after["worktree"]["files"][0]
    assert fact["digest"] == hashlib.sha256(rewritten.encode()).hexdigest()
    assert fact["freshness"] == "changed" and fact["state"] == "present"
    assert fact["index"]["hash"] != fact["digest"]
    assert after["index"]["documents"][0]["stored_hash"] != fact["digest"]
    assert after["index"]["documents"][0]["freshness"] == "verified_changed"
    assert after["units"] == [] and "stale_source" in after["omissions"]
    assert after["state"]["current"] is False and "stale_state" in after["omissions"]
    assert after["state"]["stale_evidence"]
    stale = checkouts.check(before)
    assert stale["valid"] is False and stale["reason"] == "workdir_dirty"
    assert stale["differences"] == ["workdir_dirty", "file_changed", "stale_evidence"]


def test_state_records_goal_constraints_next_steps_and_bound_verification(checkouts):
    imported(checkouts)
    head = checkouts.head()
    note = checkouts.state(
        recent_verification=[bound(commit=head), "legacy sentence without a commit"]
    )
    written = checkouts.write(note)
    assert written["verified_worktrees"] == ["agent-a"]
    state = checkouts.recover()["state"]
    assert state["goal"] == "Reliable receipt retries"
    assert state["constraints"] == ["Never resend confirmed work"]
    assert state["unfinished"] == ["Real service test"]
    assert state["current"] is True and state["stale_evidence"] == []
    current, legacy = state["recent_verification"]
    assert current["scope"] == "current" and current["commit"] == head
    assert current["branch"] == checkouts.branch and current["dirty"] is False
    assert current["declared_at"]
    assert legacy["scope"] == "unbound" and legacy["commit"] is None
    assert legacy["summary"] == "legacy sentence without a commit"


def test_declared_commit_must_be_the_observed_head_of_a_registered_checkout(checkouts):
    imported(checkouts)
    head = checkouts.head()
    claims = [
        (bound(commit="0" * 40), "workdir_conflict"),
        (bound(worktree="agent-b"), "worktree_unregistered"),
        (bound(commit="not-a-commit"), "invalid_input"),
        ({"summary": "no worktree", "commit": None}, "invalid_input"),
    ]
    for entry, code in claims:
        with pytest.raises(Fault, match=code):
            checkouts.write(checkouts.state(recent_verification=[entry]), key=f"claim-{code}")
    with checkouts.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_states").fetchone()[0] == 0
    written = checkouts.write(checkouts.state(recent_verification=[bound(commit=head)]))
    assert written["state_version"] == 1
    assert checkouts.recover()["state"]["recent_verification"][0]["commit"] == head


def test_a_single_write_may_bind_only_a_bounded_number_of_checkouts(checkouts):
    config = copy.deepcopy(checkouts.config)
    entry = config["knowledge"]["worktrees"]["demo"][0]
    config["knowledge"]["worktrees"]["demo"] = [
        dict(entry, id=f"agent-{index}") for index in range(6)
    ]
    checkouts.save(config)
    imported(checkouts)
    note = checkouts.state(
        recent_verification=[bound(worktree=f"agent-{index}") for index in range(5)]
    )
    with pytest.raises(Fault, match="too_many_worktrees"):
        checkouts.write(note)


def test_historical_and_moved_verifications_never_look_current(checkouts):
    imported(checkouts)
    head = checkouts.head()
    checkouts.write(checkouts.state(recent_verification=[bound(commit=head)]))
    checkouts.commit(message="Later commit")
    moved = checkouts.recover()["state"]["recent_verification"][0]
    assert moved["scope"] == "historical_commit" and moved["commit"] == head
    (checkouts.first / "notes.md").write_bytes(b"uncommitted\n")
    dirty = checkouts.recover()["state"]["recent_verification"][0]
    assert dirty["scope"] == "historical_commit"
    checkouts.write(
        checkouts.state(recent_verification=[bound(summary="dirty run")]),
        key="state-2",
        expected_version=1,
    )
    stamped = checkouts.recover()["state"]["recent_verification"][0]
    assert stamped["scope"] == "current" and stamped["dirty"] is True
    (checkouts.first / "notes.md").unlink()
    cleaned = checkouts.recover()["state"]["recent_verification"][0]
    assert cleaned["scope"] == "workdir_changed" and cleaned["dirty"] is True
    # A verification bound to the agent's other checkout is never this checkout's verification.
    config = copy.deepcopy(checkouts.config)
    config["knowledge"]["worktrees"]["demo"].append(
        worktree_entry(checkouts.second, checkouts.branch, ["writer"], identifier="agent-c")
    )
    checkouts.save(config)
    checkouts.write(
        checkouts.state(recent_verification=[bound(worktree="agent-c")]),
        key="state-3",
        expected_version=2,
    )
    assert checkouts.recover()["state"]["recent_verification"][0]["scope"] == "other_worktree"


def test_budget_drops_only_units_and_never_claims_tokens(checkouts):
    imported(checkouts)
    checkouts.write(checkouts.state())
    full = checkouts.recover(budget=32768)
    assert full["units"] and full["budget"]["over_budget"] is False
    assert full["budget"]["used_bytes"] == len(canonical(full).encode())
    tight = checkouts.recover(budget=4096)
    assert tight["budget"]["used_bytes"] <= 4096
    assert tight["budget"]["used_bytes"] == len(canonical(tight).encode())
    assert tight["state"]["goal"] == "Reliable receipt retries"
    assert tight["worktree"]["head"] == full["worktree"]["head"]
    for budget in (1024, 65536):
        with pytest.raises(Fault, match="invalid_input"):
            checkouts.recover(budget=budget)
    long_state = checkouts.state(
        goal="G" * 1900,
        constraints=[f"constraint {index} " + "C" * 1800 for index in range(6)],
    )
    checkouts.write(long_state, key="big", expected_version=1)
    oversized = checkouts.recover(budget=4096)
    assert oversized["budget"]["over_budget"] is True
    assert oversized["state"]["goal"] == "G" * 1900
    assert oversized["units"] == [] and "budget" in oversized["omissions"]
    assert oversized["budget"]["used_bytes"] == len(canonical(oversized).encode())


def test_recovery_is_read_only_and_never_initializes_a_project(checkouts):
    imported(checkouts)
    checkouts.write(checkouts.state())
    index = checkouts.first / ".git/index"
    head = checkouts.first / ".git/HEAD"

    def snapshot():
        with checkouts.store.transaction() as db:
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "knowledge_documents",
                    "knowledge_versions",
                    "knowledge_blocks",
                    "knowledge_states",
                    "knowledge_operations",
                )
            }
            revision = db.execute(
                "SELECT revision FROM knowledge_projects WHERE id='demo'"
            ).fetchone()[0]
        return counts, revision, index.stat().st_mtime_ns, head.read_bytes()

    before = snapshot()
    package = checkouts.recover()
    assert checkouts.check(package)["valid"] is True
    assert checkouts.recover(budget=4096)["worktree"]["id"] == "agent-a"
    assert snapshot() == before
    fresh = checkouts.directory / "fresh"
    seed_checkout(fresh)
    config = copy.deepcopy(checkouts.config)
    config["knowledge"]["projects"]["fresh"] = dict(
        config["knowledge"]["projects"]["demo"], root=str(fresh)
    )
    config["knowledge"]["worktrees"]["fresh"] = [
        worktree_entry(fresh, checkouts.branch, ["writer"])
    ]
    config["knowledge"]["clients"]["writer"]["projects"] = ["demo", "fresh"]
    checkouts.save(config)
    # An uninitialized project can still be continued: the checkout is described, the empty
    # project state is honest, and a read-only preview never creates the project row.
    with checkouts.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM knowledge_projects WHERE id='fresh'").fetchone()
    empty = checkouts.recover(project_id="fresh")
    assert empty["revision"] == 0 and empty["state"] is None
    assert empty["index"]["total"] == 0 and empty["units"] == []
    assert empty["worktree"]["head"] == checkouts.head(fresh)
    with checkouts.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM knowledge_projects WHERE id='fresh'").fetchone()
    verdict = checkouts.check(empty, project_id="fresh")
    assert verdict["valid"] is True, verdict
    # A package is bound to its own project: the same object is not current as another project.
    assert checkouts.check(empty)["reason"] == "stale_or_tampered"


def test_check_follows_branch_head_and_uncommitted_truth(checkouts):
    imported(checkouts)
    package = checkouts.recover()
    current = checkouts.check(package)
    assert current["valid"] is True and current["observed"] is True
    assert current["reason"] == "current" and current["differences"] == []
    git(checkouts.first, "checkout", "-q", "-b", "other-branch")
    branch = checkouts.check(package)
    assert branch["reason"] == "branch_changed" and branch["differences"] == ["branch_changed"]
    git(checkouts.first, "checkout", "-q", checkouts.branch)
    assert checkouts.check(package)["valid"] is True
    checkouts.commit(message="Moving head")
    head = checkouts.check(package)
    assert head["reason"] == "head_changed" and head["differences"] == ["head_changed"]
    moved = checkouts.recover()
    assert checkouts.check(moved)["valid"] is True
    (checkouts.first / "notes.md").write_text("uncommitted\n", encoding="utf-8")
    assert checkouts.check(moved)["reason"] == "workdir_dirty"
    git(checkouts.first, "add", "notes.md")
    assert checkouts.check(moved)["reason"] == "workdir_dirty"
    checkouts.commit(message="Commit the note", allow_empty=False)
    assert checkouts.check(moved)["reason"] == "head_changed"


def test_a_second_edit_with_identical_counts_is_never_still_valid(checkouts):
    """The coordinator's reproduction: dirty, recover, edit again, same counts, same HEAD.

    A tracked file that is not on the registered digest list used to be invisible to a check:
    the counts, the dirty flag and HEAD all stayed equal, so an old package kept answering
    `valid`. The observation now carries a bounded stat fingerprint of the changed set, so a
    second edit is a difference, and a state that cannot be compared is never called current.
    """
    imported(checkouts)
    (checkouts.first / "src/app.py").write_bytes(b"print('change one')\n")
    package = checkouts.recover()
    assert package["worktree"]["counts"]["modified"] == 1
    assert package["worktree"]["changes"]["complete"] is True
    assert checkouts.check(package)["valid"] is True
    # Same size, same counts, same HEAD: only the content changed.
    (checkouts.first / "src/app.py").write_bytes(b"print('change two')\n")
    assert checkouts.check(package)["valid"] is False
    answer = checkouts.check(package)
    assert answer["reason"] == "workdir_unproven"
    assert answer["differences"] == ["workdir_unproven"]
    moved = checkouts.recover()
    assert (
        moved["worktree"]["changes"]["fingerprint"] != package["worktree"]["changes"]["fingerprint"]
    )
    assert checkouts.check(moved)["valid"] is True
    # A different size, still one modified entry, is a difference on any filesystem.
    (checkouts.first / "src/app.py").write_bytes(b"print('change three, longer')\n")
    assert checkouts.check(moved)["reason"] == "workdir_unproven"
    assert checkouts.check(checkouts.recover())["valid"] is True


def test_a_bound_verification_is_never_current_after_a_further_edit(checkouts):
    """A verification is stamped with the whole observable state, not with HEAD and a flag."""
    imported(checkouts)
    head = checkouts.head()
    (checkouts.first / "src/app.py").write_bytes(b"print('first draft')\n")
    checkouts.write(checkouts.state(recent_verification=[bound(commit=head)]))
    current = checkouts.recover()["state"]["recent_verification"][0]
    assert current["scope"] == "current" and current["dirty"] is True
    assert current["facts"] is not None
    # Same HEAD, same dirty flag, same counts: the checkout is not the one that was verified.
    (checkouts.first / "src/app.py").write_bytes(b"print('second pass')\n")
    stale = checkouts.recover()["state"]["recent_verification"][0]
    assert stale["scope"] == "workdir_changed"
    assert stale["commit"] == current["commit"] and stale["dirty"] is True


def test_a_registered_digest_change_invalidates_a_bound_verification(checkouts):
    """A verified clean checkout stops being current when a registered file changes."""
    imported(checkouts)
    checkouts.write(checkouts.state(recent_verification=[bound(commit=checkouts.head())]))
    stamped = checkouts.recover()["state"]["recent_verification"][0]
    assert stamped["scope"] == "current" and stamped["dirty"] is False
    (checkouts.first / "docs/design.md").write_bytes(b"Only retry when the receipt is absent.\n")
    stale = checkouts.recover()["state"]["recent_verification"][0]
    assert stale["scope"] == "workdir_changed"
    assert stale["dirty"] is False, "the record still says how it was declared"
    # Restoring the registered bytes puts the checkout back in the state that was verified, so
    # the record is current again: the scope is about the checkout, not a permanent poisoning of
    # the note. (The package's own evidence reference was stale while the file differed.)
    (checkouts.first / "docs/design.md").write_bytes(DESIGN.encode())
    restored = checkouts.recover()
    assert restored["state"]["recent_verification"][0]["scope"] == "current"
    assert checkouts.check(restored)["valid"] is True


def test_a_verification_without_a_stamped_fingerprint_is_never_assumed_current():
    """A record that cannot be compared is `workdir_unproven`, never silently current."""
    from tianshu_memory import knowledge_continuation as continuation

    facts = {
        "id": "agent-a",
        "head": "a" * 40,
        "unborn": False,
        "branch": "main",
        "detached": False,
        "dirty": True,
        "counts": dict.fromkeys(workdir.COUNT_KEYS, 1),
        "changes": {
            "mode": workdir.CHANGE_MODE,
            "entries": 1,
            "complete": True,
            "fingerprint": "b" * 64,
        },
        "files": [],
    }
    legacy = {
        "summary": "recorded before fingerprints existed",
        "worktree": "agent-a",
        "commit": "a" * 40,
        "branch": "main",
        "dirty": True,
        "declared_at": "2026-09-16T00:00:00Z",
    }
    assert continuation.scoped(legacy, facts)["scope"] == "workdir_unproven"
    # Only a checkout with nothing uncommitted is provable without a fingerprint: its content is
    # exactly the recorded commit.
    facts["dirty"] = False
    facts["counts"] = dict.fromkeys(workdir.COUNT_KEYS, 0)
    assert continuation.scoped(legacy, facts)["scope"] == "current"
    facts["dirty"] = True
    facts["changes"]["complete"] = False
    assert continuation.scoped(legacy, facts)["scope"] == "workdir_unproven"


def test_a_checkout_too_large_to_describe_never_claims_currency(checkouts):
    """Past the changed-entry cap the state is incomplete, so nothing may pass as proved."""
    imported(checkouts)
    crowded = checkouts.first / "crowd"
    crowded.mkdir()
    for index in range(workdir.CHANGE_LIMIT + 1):
        (crowded / f"note-{index}.md").write_bytes(b"x\n")
    git(checkouts.first, "add", "crowd")
    checkouts.commit(message="Crowd the checkout", allow_empty=False)
    for index in range(workdir.CHANGE_LIMIT + 1):
        (crowded / f"note-{index}.md").write_bytes(b"y\n")
    package = checkouts.recover()
    changes = package["worktree"]["changes"]
    assert changes["complete"] is False and changes["entries"] == workdir.CHANGE_LIMIT + 1
    assert package["worktree"]["counts"]["modified"] == workdir.CHANGE_LIMIT + 1
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["reason"] == "workdir_unproven"
    # A verification cannot be bound to a checkout whose state cannot be described completely.
    with pytest.raises(Fault, match="workdir_unproven"):
        checkouts.write(checkouts.state(recent_verification=[bound(commit=checkouts.head())]))
    git(checkouts.first, "add", "crowd")
    checkouts.commit(message="Commit the crowd", allow_empty=False)
    assert checkouts.check(checkouts.recover())["valid"] is True


def test_an_edit_inside_a_collapsed_untracked_directory_is_never_still_valid(checkouts):
    """Git collapses `?? drafts/`, so the files inside it have to be described one by one.

    The coordinator's second reproduction. `--untracked-files=normal` reports the directory and
    an edit to an existing file inside it does not move the directory's own stat, so describing
    the directory instead of its files would call an edited checkout current.
    """
    folder = checkouts.first / "drafts"
    folder.mkdir()
    source = folder / "new.py"
    source.write_bytes(b"one")
    package = checkouts.recover()
    changes = package["worktree"]["changes"]
    # One collapsed entry, described by the file inside it rather than by the directory stat.
    assert changes["entries"] == 1 and changes["complete"] is True
    assert package["worktree"]["counts"]["untracked"] == 1
    assert checkouts.check(package)["valid"] is True
    source.write_bytes(b"two")
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["observed"] is True
    assert answer["reason"] == "workdir_unproven" and answer["differences"] == ["workdir_unproven"]
    # A fresh recovery of the same bytes is current again, and the next edit is caught the same
    # way instead of being excused as "the directory has not moved".
    fresh = checkouts.recover()
    assert checkouts.check(fresh)["valid"] is True
    (folder / "second.py").write_bytes(b"three")
    assert checkouts.check(fresh)["valid"] is False
    assert checkouts.check(checkouts.recover())["valid"] is True


def test_a_directory_stat_is_never_a_version_of_the_files_inside_it(checkouts):
    """The directory's own metadata is not what makes a collapsed entry current or stale."""
    folder = checkouts.first / "drafts"
    folder.mkdir()
    source = folder / "new.py"
    source.write_bytes(b"one")
    package = checkouts.recover()
    os.utime(folder, None)
    assert checkouts.check(package)["valid"] is True
    source.write_bytes(source.read_bytes() + b"two")
    assert checkouts.check(package)["valid"] is False


def test_a_verification_bound_with_an_untracked_directory_is_never_current_after_an_edit(checkouts):
    """A binding inherits the same conclusion: the fingerprint of the files inside is compared."""
    imported(checkouts)
    folder = checkouts.first / "drafts"
    folder.mkdir()
    source = folder / "new.py"
    source.write_bytes(b"one")
    checkouts.write(checkouts.state(recent_verification=[bound(commit=checkouts.head())]))
    assert checkouts.recover()["state"]["recent_verification"][0]["scope"] == "current"
    source.write_bytes(b"two")
    after = checkouts.recover()["state"]["recent_verification"][0]
    assert after["scope"] == "workdir_changed"
    assert after["facts"] is not None and after["dirty"] is True


def test_an_undescribable_untracked_directory_never_claims_currency(checkouts):
    """A nested checkout keeps its state in its own `.git`, which this service never reads."""
    imported(checkouts)
    nested = checkouts.first / "vendor" / "nested"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "keep.txt").write_bytes(b"nested\n")
    package = checkouts.recover()
    changes = package["worktree"]["changes"]
    assert changes["entries"] == 1 and changes["complete"] is False
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["reason"] == "workdir_unproven"
    # A verification cannot be bound to a state that could never be compared later.
    with pytest.raises(Fault, match="workdir_unproven"):
        checkouts.write(checkouts.state(recent_verification=[bound(commit=checkouts.head())]))
    remove_tree(checkouts.first / "vendor")
    recovered = checkouts.recover()
    assert recovered["worktree"]["changes"]["complete"] is True
    assert checkouts.check(recovered)["valid"] is True


def test_a_crowded_untracked_directory_is_never_described_partially(checkouts):
    """The same bound covers the files inside a collapsed directory, so nothing is half-known."""
    imported(checkouts)
    folder = checkouts.first / "drafts"
    folder.mkdir()
    for index in range(workdir.CHANGE_LIMIT + 1):
        (folder / f"note-{index}.md").write_bytes(b"x\n")
    package = checkouts.recover()
    changes = package["worktree"]["changes"]
    assert changes["entries"] == 1 and changes["complete"] is False
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["reason"] == "workdir_unproven"
    with pytest.raises(Fault, match="workdir_unproven"):
        checkouts.write(checkouts.state(recent_verification=[bound(commit=checkouts.head())]))


def test_untracked_file_contents_are_never_read_while_fingerprinting(checkouts, monkeypatch):
    """A collapsed directory is described by metadata only, and never charged to the read budget."""
    folder = checkouts.first / "drafts"
    folder.mkdir()
    big = folder / "big.bin"
    big.write_bytes(b"x" * (4 * 262144))
    reads = []
    real_read = workdir.read_file

    def spy(project, locator, *arguments, **kwargs):
        reads.append(locator)
        return real_read(project, locator, *arguments, **kwargs)

    monkeypatch.setattr(workdir, "read_file", spy)
    imported(checkouts)
    package = checkouts.recover()
    # Only the registered digest was ever opened: a file far past the registered byte budget is
    # described by its metadata and cannot make the collection fail or pay for it.
    assert reads == ["docs/design.md"]
    assert package["worktree"]["changes"]["complete"] is True
    assert big.stat().st_size == 4 * 262144


def test_check_rejects_index_and_revision_drift(checkouts):
    imported(checkouts)
    package = checkouts.recover()
    assert checkouts.check(package)["valid"] is True
    imported(checkouts, locator="src/app.py", key="import-2")
    drifted = checkouts.check(package)
    assert drifted["reason"] == "revision_changed"
    assert drifted["differences"] == ["revision_changed", "index_changed"]
    fresh = checkouts.recover()
    assert checkouts.check(fresh)["valid"] is True
    document = checkouts.run("query", {"text": "receipt", "budget_bytes": 8192})["blocks"][0][
        "reference"
    ]["document_id"]
    checkouts.run("delete", {"key": "delete", "document_id": document, "expected_version": 1})
    deleted = checkouts.check(fresh)
    assert deleted["valid"] is False and "index_changed" in deleted["differences"]


def test_check_is_identity_bound_and_fails_closed_on_revocation(checkouts):
    imported(checkouts)
    package = checkouts.recover()
    with pytest.raises(Fault, match="worktree_unregistered"):
        checkouts.check(package, client="peer")
    forged = copy.deepcopy(package)
    forged["worktree"]["head"] = "0" * 40
    assert checkouts.check(forged)["reason"] == "stale_or_tampered"
    forged = copy.deepcopy(package)
    forged["project_id"] = "other"
    assert checkouts.check(forged)["reason"] == "stale_or_tampered"
    broken = copy.deepcopy(package)
    broken.pop("history")
    with pytest.raises(Fault, match="invalid_input"):
        checkouts.check(broken)
    checkouts.edit(
        lambda knowledge, h: knowledge["clients"]["writer"]["permissions"].remove(
            "continuation_check"
        )
    )
    with pytest.raises(Fault, match="forbidden"):
        checkouts.check(package)
    checkouts.edit(lambda knowledge, h: knowledge["clients"]["writer"].update(projects=[]))
    with pytest.raises(Fault, match="forbidden"):
        checkouts.recover()


def test_an_unobservable_checkout_is_a_verdict_not_a_silent_success(checkouts):
    imported(checkouts)
    extra = checkouts.directory / "extra"
    seed_checkout(extra)
    config = copy.deepcopy(checkouts.config)
    config["knowledge"]["worktrees"]["demo"].append(
        worktree_entry(extra, checkouts.branch, ["writer"], identifier="agent-c")
    )
    checkouts.save(config)
    package = checkouts.recover("agent-c")
    assert checkouts.check(package)["valid"] is True
    remove_tree(extra)
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["observed"] is False
    assert answer["reason"] == "workdir_unavailable" and answer["differences"] == []
    with pytest.raises(Fault, match="workdir_unavailable"):
        checkouts.recover("agent-c")
    # The project itself is still available through its own first checkout.
    assert checkouts.check(checkouts.recover())["valid"] is True


def test_an_indexed_document_without_a_registered_digest_stays_unverified(checkouts):
    (checkouts.first / "docs/extra.md").write_bytes(EXTRA.encode())
    imported(checkouts)
    imported(checkouts, locator="docs/extra.md", key="import-extra")
    package = checkouts.recover()
    rows = {row["locator"]: row for row in package["index"]["documents"]}
    assert set(rows) == {"docs/design.md", "docs/extra.md"}
    assert rows["docs/design.md"]["freshness"] == "verified_current"
    # A registered digest is compared with the current file; an unregistered one is only shown.
    unregistered = rows["docs/extra.md"]
    assert unregistered["freshness"] == "unverified"
    assert unregistered["stored_hash"] == hashlib.sha256(EXTRA.encode()).hexdigest()
    assert [fact["locator"] for fact in package["worktree"]["files"]] == ["docs/design.md"]
    assert all(
        fact["digest"] != unregistered["stored_hash"] for fact in package["worktree"]["files"]
    )
    # No host path is echoed back anywhere in the package, in any section.
    lowered = str(checkouts.directory).lower()
    for value in strings(package):
        assert lowered not in value.lower()
        assert str(checkouts.first).lower() not in value.lower()


def test_a_failing_git_and_an_unmigrated_database_fail_closed(checkouts, tmp_path, monkeypatch):
    imported(checkouts)
    real_git = git_path()
    package = checkouts.recover()
    # A Git that answers everything but one read-only identity call is a hard failure, never a
    # partially observed checkout. (The delegated argument is caret free on purpose: `cmd`
    # strips `^`, which would silently turn a real failure into a false "unborn branch".)
    failing = tmp_path / "failing-git.cmd"
    failing.write_text(
        "@echo off\r\n"
        'echo %* | findstr /C:" symbolic-ref " >nul && exit /b 5\r\n'
        f'"{real_git}" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, str(failing))
    with pytest.raises(Fault, match="workdir_git_failed"):
        checkouts.recover()
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["observed"] is False
    assert answer["reason"] == "workdir_git_failed" and answer["differences"] == []
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, real_git)
    restored = checkouts.check(package)
    assert restored["valid"] is True, restored
    # An unmigrated database refuses both reads and keeps every row it had.
    with checkouts.store.transaction() as db:
        before = db.execute("SELECT revision FROM knowledge_projects WHERE id='demo'").fetchone()[0]
        db.execute("DELETE FROM metadata WHERE key='knowledge_schema'")
    for call in (checkouts.recover, lambda: checkouts.check(package)):
        with pytest.raises(Fault, match="dependency_unavailable"):
            call()
    with checkouts.store.transaction() as db:
        after = db.execute("SELECT revision FROM knowledge_projects WHERE id='demo'").fetchone()[0]
    assert after == before


def test_repository_configured_helper_is_never_run(checkouts):
    marker = checkouts.first / "fsmonitor-ran.txt"
    hook = checkouts.first / "fsmonitor-hook.cmd"
    hook.write_text(
        '@echo off\r\necho ran > "%~dp0fsmonitor-ran.txt"\r\nexit /b 0\r\n', encoding="ascii"
    )
    git(checkouts.first, "config", "core.fsmonitor", str(hook).replace("\\", "/"))
    git(checkouts.first, "update-index", "--fsmonitor", check=False)
    subprocess.run(
        [git_path(), "-c", "core.autocrlf=false", "-C", str(checkouts.first), "status"],
        capture_output=True,
        timeout=60,
    )
    assert marker.exists(), "the fixture helper never ran, so the assertion below proves nothing"
    marker.unlink()
    imported(checkouts)
    checkouts.recover()
    checkouts.check(checkouts.recover())
    assert marker.exists() is False


def clean_filter(checkouts, marker_name="clean-filter-marker.txt"):
    """A repository that makes Git run a program while comparing content, plus its marker."""
    (checkouts.first / ".gitattributes").write_bytes(b"src/app.py filter=probe\n")
    git(
        checkouts.first,
        "config",
        "filter.probe.clean",
        f"echo EXECUTED > {marker_name}; cat",
    )
    # A same-size edit of a tracked file that is *not* one of the registered digests. Only a
    # real content comparison can see it, which is exactly what makes Git run the clean filter.
    (checkouts.first / "src/app.py").write_bytes(b"print('changed')\n")
    return checkouts.first / marker_name


def plain_status(path):
    """The checkout's own `git status`, which is allowed to run whatever the repository says."""
    return subprocess.run(
        [git_path(), "-c", "core.autocrlf=false", "-C", str(path), "status", "--porcelain=v1"],
        capture_output=True,
        timeout=60,
    )


def test_a_repository_clean_filter_is_never_executed(checkouts, monkeypatch):
    """A `.gitattributes` filter is a repository program: a read-only read never runs it.

    This is the coordinator's reproduction. The control call below proves the fixture really
    makes Git execute a repository-configured program, so the assertions after it are about the
    collector's neutralization and not about a filter that was never reachable.
    """
    marker = clean_filter(checkouts)
    control = plain_status(checkouts.first)
    assert control.returncode == 0 and b"M src/app.py" in control.stdout, control.stdout
    assert marker.exists(), "the fixture filter never ran, so this test would prove nothing"
    marker.unlink()
    commands = []
    real_command = workdir.command

    def spy(entry, *arguments, guard=()):
        commands.append(real_command(entry, *arguments, guard=guard))
        return real_command(entry, *arguments, guard=guard)

    monkeypatch.setattr(workdir, "command", spy)
    imported(checkouts)
    package = checkouts.recover()
    assert package["worktree"]["counts"]["modified"] == 1
    assert "probe" in package["worktree"]["git"]["helpers"]
    assert marker.exists() is False
    assert checkouts.check(package)["valid"] is True
    assert marker.exists() is False
    flattened = [argument for command in commands for argument in command]
    # The guard is not an accident of the configuration: every call carries the override that
    # disables the discovered driver in both directions and its long-running process form.
    for expected in (
        "filter.probe.clean=",
        "filter.probe.process=",
        "filter.probe.smudge=",
        "filter.probe.required=false",
    ):
        assert expected in flattened, expected
    assert "core.fsmonitor=false" in flattened


def test_a_repository_process_filter_is_never_executed(checkouts):
    """The `process` form is preferred by Git when it exists, so it must be neutralized too."""
    marker = checkouts.first / "process-filter-marker.txt"
    (checkouts.first / ".gitattributes").write_bytes(b"src/app.py filter=probe\n")
    git(
        checkouts.first,
        "config",
        "filter.probe.process",
        "echo PROCESS > process-filter-marker.txt; cat",
    )
    (checkouts.first / "src/app.py").write_bytes(b"print('changed')\n")
    assert plain_status(checkouts.first).returncode == 0
    assert marker.exists(), "the fixture process filter never ran, so this proves nothing"
    marker.unlink()
    imported(checkouts)
    package = checkouts.recover()
    assert package["worktree"]["counts"]["modified"] == 1
    checkouts.check(package)
    assert marker.exists() is False


def test_an_inherited_git_configuration_cannot_install_a_helper(checkouts, monkeypatch):
    """No inherited `GIT_*` variable may define a program for a read-only call to run."""
    marker = checkouts.first / "environment-marker.txt"
    (checkouts.first / ".gitattributes").write_bytes(b"src/app.py filter=probe\n")
    (checkouts.first / "src/app.py").write_bytes(b"print('changed')\n")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.probe.clean")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "echo EXECUTED > environment-marker.txt; cat")
    imported(checkouts)
    package = checkouts.recover()
    assert marker.exists() is False
    assert package["worktree"]["counts"]["modified"] == 1
    # The inherited definition is invisible to the collector: it is neither listed as a driver
    # that had to be neutralized nor able to run.
    assert "probe" not in package["worktree"]["git"]["helpers"]


def test_helper_names_keep_the_complete_driver_and_refuse_what_cannot_be_overridden():
    """Discovery never truncates a dotted subsection, and never claims an impossible override.

    A driver name is the whole subsection, so `filter.probe.dot.clean` belongs to `probe.dot` and
    not to `probe`; anything that cannot be written as one unambiguous `-c` key is refused rather
    than reported as neutralized. `config --null --list` separates key and value with a newline.
    """

    def record(key, value="x"):
        return f"{key}\n{value}".encode() + b"\x00"

    listing = (
        record("filter.probe.dot.clean")
        + record("filter.probe.dot.process")
        + record("diff.probe.dot.textconv")
        + record("filter.lfs.required", "true")
        + record("diff.renames", "true")
    )
    assert workdir.helper_names(listing) == {"probe.dot", "lfs"}
    # Two drivers that share a prefix are two drivers.
    assert workdir.helper_names(
        record("filter.probe.clean") + record("filter.probe.dot.clean")
    ) == {"probe", "probe.dot"}
    # Git splits a `-c` key at its first `=`, so this name cannot be addressed by any override.
    with pytest.raises(Fault, match="workdir_helper_unrepresentable"):
        workdir.helper_names(record("filter.a=b.clean"))
    # The `filter` section has no plain variables: a variable this service does not know cannot
    # be shown to be inert.
    with pytest.raises(Fault, match="workdir_helper_unrepresentable"):
        workdir.helper_names(record("filter.probe.unknown"))
    # A `diff` section does mix plain variables with driver keys, and only command/textconv name
    # a program.
    assert workdir.helper_names(record("diff.renames", "true") + record("diff.algorithm")) == set()


def test_a_dotted_filter_driver_name_is_never_left_executable(checkouts, monkeypatch):
    """A filter subsection may contain dots: the complete name has to be neutralized.

    The coordinator's second reproduction. The control call proves the fixture really makes Git
    run the repository program, and the spy proves every call carries the override for the
    complete driver name instead of a truncated one that would leave it executable.
    """
    marker = checkouts.first / "dotted-filter-marker.txt"
    (checkouts.first / ".gitattributes").write_bytes(b"src/app.py filter=probe.dot\n")
    git(
        checkouts.first,
        "config",
        "filter.probe.dot.clean",
        "echo EXECUTED > dotted-filter-marker.txt; cat",
    )
    (checkouts.first / "src/app.py").write_bytes(b"print('changed')\n")
    control = plain_status(checkouts.first)
    assert control.returncode == 0 and b"M src/app.py" in control.stdout, control.stdout
    assert marker.exists(), "the fixture filter never ran, so this test would prove nothing"
    marker.unlink()
    commands = []
    real_command = workdir.command

    def spy(entry, *arguments, guard=()):
        commands.append(real_command(entry, *arguments, guard=guard))
        return real_command(entry, *arguments, guard=guard)

    monkeypatch.setattr(workdir, "command", spy)
    imported(checkouts)
    package = checkouts.recover()
    assert package["worktree"]["counts"]["modified"] == 1
    helpers = package["worktree"]["git"]["helpers"]
    assert "probe.dot" in helpers and "probe" not in helpers
    flattened = [argument for command in commands for argument in command]
    for expected in (
        "filter.probe.dot.clean=",
        "filter.probe.dot.process=",
        "filter.probe.dot.smudge=",
        "filter.probe.dot.required=false",
    ):
        assert expected in flattened, expected
    assert marker.exists() is False
    assert checkouts.check(package)["valid"] is True
    assert marker.exists() is False


def test_a_dotted_process_filter_driver_name_is_never_executed(checkouts):
    """Git prefers the `process` form when it exists, so a dotted name has to cover it too."""
    marker = checkouts.first / "dotted-process-marker.txt"
    (checkouts.first / ".gitattributes").write_bytes(b"src/app.py filter=probe.dot\n")
    git(
        checkouts.first,
        "config",
        "filter.probe.dot.process",
        "echo PROCESS > dotted-process-marker.txt; cat",
    )
    (checkouts.first / "src/app.py").write_bytes(b"print('changed')\n")
    assert plain_status(checkouts.first).returncode == 0
    assert marker.exists(), "the fixture process filter never ran, so this proves nothing"
    marker.unlink()
    imported(checkouts)
    package = checkouts.recover()
    assert "probe.dot" in package["worktree"]["git"]["helpers"]
    assert package["worktree"]["counts"]["modified"] == 1
    checkouts.check(package)
    assert marker.exists() is False


def test_a_helper_name_that_cannot_be_overridden_is_refused(checkouts):
    """A driver that no override can address is refused, never reported as neutralized.

    Git splits a `-c` key at its first `=`, so `filter.a=b.clean` cannot be reached by
    `-c filter.a=b.clean=`; the only honest answer is to refuse the collection. The control call
    proves the fixture driver really does run under an ordinary `git status`.
    """
    imported(checkouts)
    package = checkouts.recover()
    assert checkouts.check(package)["valid"] is True
    marker = checkouts.first / "unsafe-filter-marker.txt"
    (checkouts.first / ".gitattributes").write_bytes(b"src/app.py filter=a=b\n")
    config = checkouts.first / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + '[filter "a=b"]\n\tclean = echo EXECUTED > unsafe-filter-marker.txt; cat\n',
        encoding="utf-8",
    )
    (checkouts.first / "src/app.py").write_bytes(b"print('changed')\n")
    assert plain_status(checkouts.first).returncode == 0
    assert marker.exists(), "the fixture filter never ran, so this test would prove nothing"
    marker.unlink()
    answer = checkouts.check(package)
    assert answer["valid"] is False and answer["observed"] is False
    assert answer["reason"] == "workdir_helper_unrepresentable" and answer["differences"] == []
    with pytest.raises(Fault, match="workdir_helper_unrepresentable"):
        checkouts.recover()
    assert marker.exists() is False


def test_output_caps_stop_the_child_instead_of_buffering_it(checkouts, tmp_path, monkeypatch):
    """Both pipes are capped while the child runs, so a flooding process is killed at the cap."""
    tools = tmp_path / "caps"
    tools.mkdir()
    flood = tools / "flood-git.cmd"
    flood.write_text(
        "@echo off\r\n:loop\r\necho xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\r\ngoto loop\r\n",
        encoding="ascii",
    )
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, str(flood))
    started = time.monotonic()
    with pytest.raises(Fault, match="workdir_output_too_large"):
        checkouts.recover()
    assert time.monotonic() - started < workdir.TIMEOUT_SECONDS, "the cap, not the deadline"
    # Standard error is capped as well: it used to be collected without a bound at all.
    noisy = tools / "noisy-stderr-git.cmd"
    noisy.write_text(
        "@echo off\r\n:loop\r\necho yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy 1>&2\r\ngoto loop\r\n",
        encoding="ascii",
    )
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, str(noisy))
    started = time.monotonic()
    with pytest.raises(Fault, match="workdir_output_too_large"):
        checkouts.recover()
    assert time.monotonic() - started < workdir.TIMEOUT_SECONDS, "the cap, not the deadline"


def test_only_read_only_git_subcommands_are_invoked_and_nothing_is_fetched(checkouts, monkeypatch):
    remote = checkouts.directory / "remote.git"
    completed = subprocess.run(
        [git_path(), "clone", "-q", "--bare", str(checkouts.first), str(remote)],
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    git(checkouts.first, "remote", "add", "origin", str(remote))
    git(checkouts.first, "fetch", "-q", "origin")
    tracked = text_of(checkouts.first, "rev-parse", f"refs/remotes/origin/{checkouts.branch}")
    pusher = checkouts.directory / "pusher"
    clone(checkouts.first, pusher)
    git(pusher, "remote", "set-url", "origin", str(remote))
    checkouts.commit(path=pusher, message="Remote moves on")
    git(pusher, "push", "-q", "origin", checkouts.branch)
    assert text_of(remote, "rev-parse", checkouts.branch) != tracked
    invoked, commands = [], []
    real_run, real_command = workdir.run, workdir.command

    def spy_run(entry, arguments, limit, guard=()):
        invoked.append(arguments[0])
        commands.append(real_command(entry, *arguments, guard=guard))
        return real_run(entry, arguments, limit, guard)

    monkeypatch.setattr(workdir, "run", spy_run)
    package = checkouts.recover()
    assert invoked and set(invoked) <= set(workdir.READ_ONLY)
    assert invoked.count("status") == 1 and invoked.count("log") == 1
    for command in commands:
        assert command[0] == git_path()
        assert "--no-optional-locks" in command and "core.fsmonitor=false" in command
        assert not {"fetch", "checkout", "reset", "clean", "diff", "pull"} & set(command)
    # The remote moved on, and a recovery still never fetches it.
    assert (
        text_of(checkouts.first, "rev-parse", f"refs/remotes/origin/{checkouts.branch}") == tracked
    )
    assert package["worktree"]["git"]["subcommands"] == list(workdir.READ_ONLY)


def test_git_failure_timeout_output_and_missing_git_fail_closed(checkouts, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    sleeping = tools / "slow-git.cmd"
    sleeping.write_text("@echo off\r\nping -n 6 127.0.0.1 > nul\r\nexit /b 0\r\n", encoding="ascii")
    big = tools / "big.txt"
    big.write_text("x" * 300000, encoding="ascii")
    noisy = tools / "noisy-git.cmd"
    noisy.write_text('@echo off\r\ntype "%~dp0big.txt"\r\nexit /b 0\r\n', encoding="ascii")
    monkeypatch.setattr(workdir, "TIMEOUT_SECONDS", 1)
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, str(sleeping))
    with pytest.raises(Fault, match="workdir_timeout"):
        checkouts.recover()
    monkeypatch.setattr(workdir, "TIMEOUT_SECONDS", 10)
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, str(noisy))
    with pytest.raises(Fault, match="workdir_output_too_large"):
        checkouts.recover()
    monkeypatch.setenv(workdir.GIT_ENVIRONMENT_VARIABLE, "definitely-not-a-git-executable")
    with pytest.raises(Fault, match="git_unavailable"):
        checkouts.recover()


def test_slow_git_never_holds_the_shared_writer_lock(checkouts, contracts, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from tianshu_memory.service import MemoryService

    imported(checkouts)
    entered, release = Event(), Event()
    real = workdir.run

    def paused(entry, arguments, limit, guard=()):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "test gate was not released"
        return real(entry, arguments, limit, guard)

    def chat():
        service = MemoryService(checkouts.store, contracts)
        account = {"namespace": "synthetic", "immutable_account_id": "continuation-chat"}
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

    monkeypatch.setattr(workdir, "run", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(checkouts.recover)
        try:
            assert entered.wait(5)
            assert pool.submit(chat).result(timeout=1)["state"] == "found"
        finally:
            release.set()
        assert waiting.result(timeout=10)["worktree"]["id"] == "agent-a"


def test_registered_file_that_escapes_the_checkout_is_refused(checkouts):
    outside = checkouts.directory / "outside"
    outside.mkdir()
    (outside / "outside.md").write_text("Private outside source\n", encoding="utf-8")
    link = checkouts.first / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, timeout=30
        )
        assert created.returncode == 0, created.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    checkouts.edit(
        lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(files=["linked/outside.md"])
    )
    with pytest.raises(Fault, match="workdir_file_refused"):
        checkouts.recover()
    checkouts.edit(
        lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(
            files=["docs/design.md", "src/app.py"]
        )
    )
    facts = checkouts.recover()["worktree"]["files"]
    assert [fact["locator"] for fact in facts] == ["docs/design.md", "src/app.py"]
    assert facts[1]["digest"] == hashlib.sha256(b"print('receipt')\n").hexdigest()


def test_missing_and_unreadable_registered_files_are_honest_states(checkouts):
    checkouts.edit(
        lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(
            files=["docs/design.md", "docs/gone.md"]
        )
    )
    (checkouts.first / "docs").mkdir(exist_ok=True)
    facts = checkouts.recover()["worktree"]["files"]
    assert [fact["state"] for fact in facts] == ["present", "missing"]
    assert facts[1]["digest"] is None and facts[1]["index"] is None
    assert facts[1]["freshness"] == "unindexed"


def test_a_registered_file_that_outgrows_its_reader_is_never_digested(checkouts, monkeypatch):
    """The reader's own limit is an honest per-file state, never a wrong digest."""
    real = workdir.read_file

    def grown(project, locator):
        if locator == "docs/design.md":
            raise Fault("source_too_large", 413)
        return real(project, locator)

    monkeypatch.setattr(workdir, "read_file", grown)
    facts = checkouts.recover()["worktree"]["files"]
    assert [fact["state"] for fact in facts] == ["too_large"]
    assert facts[0]["digest"] is None and facts[0]["size"] is None


def test_the_cumulative_read_budget_is_charged_before_a_file_is_read(checkouts, monkeypatch):
    """`max_bytes` bounds the whole registered read, not merely every file so far."""
    checkouts.edit(
        lambda knowledge, h: knowledge["worktrees"]["demo"][0].update(
            files=["docs/design.md", "docs/second.md"], max_bytes=1024
        )
    )
    (checkouts.first / "docs/second.md").write_bytes(b"y" * 2048)
    read = []
    real = workdir.read_file

    def observed(project, locator):
        read.append(locator)
        return real(project, locator)

    monkeypatch.setattr(workdir, "read_file", observed)
    with pytest.raises(Fault, match="workdir_byte_budget"):
        checkouts.recover()
    # The file that does not fit the budget is refused *before* it is opened, so the registered
    # read set can never exceed the configured maximum, not even by its last member.
    assert read == ["docs/design.md"]


def test_cli_and_official_sdk_stdio_roundtrip(checkouts):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    imported(checkouts)
    checkouts.write(checkouts.state())
    command = ["-m", "tianshu_memory.knowledge_cli", "--config", str(checkouts.path)]
    environment = dict(
        os.environ,
        TEST_KNOWLEDGE_SECRET=SECRET,
        PYTHONUTF8="1",
        PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
    )
    action = checkouts.directory / "recover.json"
    action.write_text(
        canonical(
            {
                "operation": "continuation_recover",
                "project_id": "demo",
                "arguments": {"worktree": "agent-a", "text": "receipt", "budget_bytes": 16384},
            }
        )
    )

    def cli(path):
        return subprocess.run(
            [
                sys.executable,
                *command,
                "action",
                "--client",
                "writer",
                "--credential-env",
                "TEST_KNOWLEDGE_SECRET",
                str(path),
            ],
            cwd=checkouts.directory,
            env=environment,
            capture_output=True,
            timeout=120,
        )

    recovered = cli(action)
    assert recovered.returncode == 0, recovered.stderr
    package = json.loads(recovered.stdout)
    assert package["worktree"]["head"] == checkouts.head()
    check = checkouts.directory / "check.json"
    check.write_text(
        canonical(
            {
                "operation": "continuation_check",
                "project_id": "demo",
                "arguments": {"package": package},
            }
        )
    )
    verified = cli(check)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True
    checkouts.commit(message="Moved after the package")
    stale = cli(check)
    assert stale.returncode == 0, stale.stderr
    assert json.loads(stale.stdout)["reason"] == "head_changed"

    async def exercise(client, denied):
        server = StdioServerParameters(
            command=sys.executable,
            args=[*command, "mcp", "--client", client, "--credential-env", "TEST_KNOWLEDGE_SECRET"],
            env=environment,
            cwd=str(checkouts.directory),
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert len(tools.tools) == 22
                assert {
                    "knowledge_continuation_recover",
                    "knowledge_continuation_check",
                } <= names
                response = await session.call_tool(
                    "knowledge_continuation_recover",
                    {
                        "project_id": "demo",
                        "worktree": "agent-a",
                        "text": "receipt",
                        "budget_bytes": 8192,
                    },
                )
                assert response.isError is denied
                if not denied:
                    assert "head" in str(response.content)
                checking = await session.call_tool(
                    "knowledge_continuation_check", {"project_id": "demo", "package": package}
                )
                assert checking.isError is denied

    asyncio.run(exercise("writer", False))
    asyncio.run(exercise("peer", True))


def test_candidate_contract_covers_the_emitted_package(checkouts):
    from jsonschema import Draft202012Validator

    directory = ROOT / "docs/candidates/project-continuation/v1"
    schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    examples = json.loads((directory / "examples.json").read_text(encoding="utf-8"))
    assert examples
    for example in examples:
        validator.validate(example)
    imported(checkouts)
    checkouts.write(checkouts.state())
    validator.validate(
        {
            "operation": "continuation_check",
            "project_id": "demo",
            "arguments": {"package": checkouts.recover()},
        }
    )
