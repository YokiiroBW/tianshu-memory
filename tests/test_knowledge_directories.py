"""Registered-directory incremental import: preview, explicit apply and resumable results.

Two registered synthetic projects with isolated files, database and credentials. Every scan,
apply and deletion decision runs through KnowledgeApplication, so authorization, plan binding,
conflict handling and the source guard are exercised exactly as in documented use.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from test_knowledge_concurrency import chat_operation

from tianshu_memory import knowledge_directories as directories
from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_directories_migration import migrate as migrate_directories
from tianshu_memory.knowledge_directories_schema import TRACKED as DIRECTORY_TRACKED
from tianshu_memory.knowledge_migration import migrate
from tianshu_memory.lessons import ExperienceBook
from tianshu_memory.store import Store

SECRET = "synthetic-directory-client-secret"
URL = "https://example.com/design"
PROJECT_WRITE = ["import", "query", "recover", "check", "write_state", "delete", "status"]
DIRECTORY = ["directory_scan", "directory_apply"]
LESSON_WRITE = ["lesson_record", "lesson_revise", "lesson_retire"]
LESSON_READ = ["lesson_query", "lesson_recover", "lesson_check"]
EXPERIENCE_READ = ["experience_query", "experience_check"]
EXPERIENCE_WRITE = ["experience_promote", "experience_revoke", "experience_withdraw"]
ALPHA_DESIGN = (
    "Alpha receipt retry rule: only retry when the receipt is absent.\n"
    "Never resend confirmed work.\n"
)
BETA_DESIGN = "Beta receipt evidence: Syntheticbetamarker check the table before any retry.\n"
LESSON = {
    "trigger": "Send timed out before the receipt arrived",
    "symptom": "The same message is delivered twice",
    "cause": "Retry did not read the receipt table first",
    "correction": "Read the receipt table and skip confirmed work",
    "verification": "Isolated directory regression run passed",
    "scope": {
        "platform": "windows",
        "language": "python",
        "framework": "stdlib",
        "applies_to": ["idempotent delivery"],
        "excludes": ["outbound-only notifications"],
    },
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def registered_clients():
    return {
        "alpha-writer": {
            "credential_sha256": digest(SECRET),
            "projects": ["alpha"],
            "permissions": [*PROJECT_WRITE, *DIRECTORY, *LESSON_WRITE, *LESSON_READ],
        },
        "beta-writer": {
            "credential_sha256": digest(SECRET),
            "projects": ["beta"],
            "permissions": [*PROJECT_WRITE, *DIRECTORY, *LESSON_WRITE, *LESSON_READ],
        },
        "operator": {
            "credential_sha256": digest(SECRET),
            "projects": ["alpha", "beta"],
            "permissions": [
                *PROJECT_WRITE,
                *DIRECTORY,
                *LESSON_WRITE,
                *LESSON_READ,
                *EXPERIENCE_READ,
                *EXPERIENCE_WRITE,
                "promote",
                "review",
            ],
        },
        "reader": {
            "credential_sha256": digest(SECRET),
            "projects": ["alpha"],
            "permissions": ["query", "recover", "check", "status", "directory_scan"],
        },
    }


@pytest.fixture
def catalog(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "catalog.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    roots = {}
    for name, design in (("alpha", ALPHA_DESIGN), ("beta", BETA_DESIGN)):
        root = tmp_path / name
        (root / "docs" / "notes").mkdir(parents=True)
        (root / "docs" / "design.md").write_text(design, encoding="utf-8")
        (root / "docs" / "notes" / "retry.md").write_text(
            "Nested note: preserve receipts as proof of execution.\n", encoding="utf-8"
        )
        (root / "docs" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        (root / "docs" / ".env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
        (root / "docs" / ".git").mkdir()
        (root / "docs" / ".git" / "config").write_text("synthetic vcs metadata\n", encoding="utf-8")
        (root / "docs" / "credentials").mkdir()
        (root / "docs" / "credentials" / "token.md").write_text("not scanned\n", encoding="utf-8")
        (root / "outside.md").write_text("Root file outside the registered directory.\n")
        roots[name] = root
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.md").write_text("Outside the registered root.\n", encoding="utf-8")
    link = roots["alpha"] / "docs" / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, timeout=10
        )
        assert created.returncode == 0, created.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {
                name: {
                    "root": str(root),
                    "host": "local",
                    "default_branch": "main",
                    "urls": [URL] if name == "alpha" else [],
                }
                for name, root in roots.items()
            },
            "clients": registered_clients(),
            "directories": {
                "alpha": [{"path": "docs", "max_files": 8, "max_bytes": 65536}],
                "beta": [{"path": "docs", "max_files": 4, "max_bytes": 32768}],
            },
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    app = KnowledgeApplication(path)

    def run(operation, arguments, *, project="alpha", client="operator", credential=SECRET):
        return app.execute(
            dict(operation=operation, project_id=project, arguments=arguments),
            client=client,
            credential=credential,
        )

    return SimpleNamespace(
        run=run,
        app=app,
        store=store,
        path=path,
        config=config,
        roots=roots,
        tmp_path=tmp_path,
        outside=outside,
    )


def scan(harness, directory="docs", *, project="alpha", client="operator"):
    return harness.run("directory_scan", {"directory": directory}, project=project, client=client)


def apply_plan(
    harness, plan, *, key="apply-1", tombstones=None, project="alpha", client="operator"
):
    return harness.run(
        "directory_apply",
        {"key": key, "plan": plan, "tombstones": tombstones or []},
        project=project,
        client=client,
    )


def applied(harness, key="apply-1", *, project="alpha", directory="docs", client="operator"):
    plan = scan(harness, directory, project=project, client=client)
    result = apply_plan(harness, plan, key=key, project=project, client=client)
    assert result["status"] == "applied", result
    return plan, result


def item(plan, locator):
    return next(entry for entry in plan["items"] if entry["locator"] == locator)


def reason(plan, locator):
    return next(entry["reason"] for entry in plan["omitted"] if entry["locator"] == locator)


def documents(store, project="alpha"):
    with store.transaction() as db:
        return {
            row["locator"]: (row["version"], row["state"])
            for row in db.execute(
                "SELECT locator,version,state FROM knowledge_documents WHERE project_id=?",
                (project,),
            )
        }


def gate(monkeypatch, name, real):
    """Pause one directory-phase call, so a test can act while a dispatch is in flight."""
    entered, release = Event(), Event()

    def paused(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "test gate was not released"
        return real(*args, **kwargs)

    monkeypatch.setattr(directories, name, paused)
    return entered, release


# -- preview -------------------------------------------------------------------


def test_preview_pins_paths_types_sizes_digests_and_current_versions(catalog):
    plan = scan(catalog)
    assert plan["scan"] == "registered_directory_only"
    assert plan["deletions"] == "explicit_approval_only"
    assert plan["plan_id"] == directories.plan_fingerprint(plan)
    assert plan["client"] == "operator" and plan["project_id"] == "alpha"
    assert plan["registration"] == fingerprint(catalog.config["knowledge"]["projects"]["alpha"])
    assert plan["revision"] == 0
    assert [entry["locator"] for entry in plan["items"]] == [
        "docs/design.md",
        "docs/notes/retry.md",
    ]
    for entry in plan["items"]:
        raw = (catalog.roots["alpha"] / entry["locator"]).read_bytes()
        assert entry["size"] == len(raw)
        assert entry["digest"] == hashlib.sha256(raw).hexdigest()
        assert entry["kind"] == "file" and entry["media_type"] == "text/plain"
        assert entry["version"] == 0 and entry["action"] == "import"
    assert reason(plan, "docs/logo.png") == "unsupported_type"
    assert reason(plan, "docs/.env") == "refused_name"
    # A junction is never followed, and a denied directory is neither entered nor listed.
    assert plan["counts"]["links"] == 1
    assert plan["counts"]["denied_directories"] == 2
    assert not [entry for entry in plan["omitted"] if "linked" in entry["locator"]]
    assert not [entry for entry in plan["omitted"] if "outside" in entry["locator"]]
    assert plan["counts"]["listed"] == 2 and plan["counts"]["missing"] == 0
    assert plan["walk_truncated"] is False and plan["budget_incomplete"] is False


def test_preview_is_read_only_and_records_only_the_plan(catalog):
    with catalog.store.transaction() as db:
        before = int(dict(db.execute("SELECT key,value FROM metadata"))["source_revision"])
    plan = scan(catalog)
    with catalog.store.transaction() as db:
        revision = int(dict(db.execute("SELECT key,value FROM metadata"))["source_revision"])
        stored = db.execute("SELECT * FROM knowledge_plans").fetchall()
        assert db.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 0
        # A preview never creates the project row: only a successful write does that.
        assert db.execute("SELECT COUNT(*) FROM knowledge_projects").fetchone()[0] == 0
    assert revision == before + 1
    assert len(stored) == 1 and stored[0]["plan_id"] == plan["plan_id"]
    assert json.loads(stored[0]["body"]) == plan
    with pytest.raises(Fault, match="project_uninitialized"):
        catalog.run("query", {"text": "receipt", "budget_bytes": 8192})


def test_later_preview_supersedes_the_earlier_one(catalog):
    first = scan(catalog)
    (catalog.roots["alpha"] / "docs" / "extra.md").write_text("Extra alpha note.\n")
    second = scan(catalog)
    assert second["plan_id"] != first["plan_id"]
    with pytest.raises(Fault, match="plan_superseded"):
        apply_plan(catalog, first)
    assert apply_plan(catalog, second)["status"] == "applied"
    assert [entry["locator"] for entry in second["items"]] == [
        "docs/design.md",
        "docs/extra.md",
        "docs/notes/retry.md",
    ]


# -- apply ---------------------------------------------------------------------


def test_apply_imports_the_previewed_content_and_a_rescan_is_unchanged(catalog):
    plan, result = applied(catalog)
    assert {entry["outcome"] for entry in result["items"]} == {"imported"}
    assert {entry["version"] for entry in result["items"]} == {1}
    assert result["rescan"] is False and result["remaining"] == []
    assert result["authority"] == "explicit_directory_confirmation"
    assert documents(catalog.store) == {
        "docs/design.md": (1, "ready"),
        "docs/notes/retry.md": (1, "ready"),
    }
    hit = catalog.run("query", {"text": "receipt absent", "budget_bytes": 8192})
    assert "only retry when the receipt is absent" in hit["blocks"][0]["text"]
    assert hit["blocks"][0]["provenance"]["locator"] == "docs/design.md"
    again = scan(catalog)
    assert {entry["action"] for entry in again["items"]} == {"unchanged"}
    assert {entry["version"] for entry in again["items"]} == {1}
    # Confirming an unchanged preview again writes no new version.
    repeat = apply_plan(catalog, again, key="apply-2")
    assert {entry["outcome"] for entry in repeat["items"]} == {"unchanged"}
    assert documents(catalog.store)["docs/design.md"] == (1, "ready")
    # The confirmed plan is still the recorded result for its own key.
    assert catalog.run("status", {"key": "apply-1"})["plan_id"] == plan["plan_id"]


def test_apply_replay_returns_the_recorded_result(catalog):
    plan, result = applied(catalog)
    replay = apply_plan(catalog, plan)
    assert replay["replayed"] is True and replay["plan_id"] == result["plan_id"]
    recorded = catalog.run("status", {"key": "apply-1"})
    assert recorded["status"] == "applied" and recorded["plan_id"] == plan["plan_id"]
    with pytest.raises(Fault, match="idempotency_conflict"):
        catalog.run(
            "directory_apply",
            {"key": "apply-1", "plan": scan(catalog), "tombstones": []},
        )


def test_plan_tampering_foreign_and_unissued_plans_are_refused(catalog):
    plan = scan(catalog)
    edited = json.loads(json.dumps(plan))
    edited["items"][0]["digest"] = "0" * 64
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(catalog, edited, key="tampered-digest")
    location = json.loads(json.dumps(plan))
    location["items"][0]["locator"] = "outside.md"
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(catalog, location, key="outside-locator")
    widened = json.loads(json.dumps(plan))
    widened["limits"]["max_files"] = 64
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(catalog, widened, key="widened-limits")
    # A plan may not point one locator at another document's identity, even self-consistently.
    confused = json.loads(json.dumps(plan))
    confused["items"][0]["document_id"] = confused["items"][1]["document_id"]
    confused["plan_id"] = directories.plan_fingerprint(confused)
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(catalog, confused, key="confused-identity")
    assert apply_plan(catalog, plan, key="alpha-apply")["status"] == "applied"
    # The beta preview is valid, but it was never issued for alpha.
    beta = scan(catalog, "docs", project="beta")
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(catalog, beta, key="cross-project")
    # Every plan is bound to the identity that received it, not merely to the project.
    operator_beta = scan(catalog, "docs", project="beta", client="operator")
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(
            catalog, operator_beta, key="foreign-client", project="beta", client="beta-writer"
        )
    writer_beta = scan(catalog, "docs", project="beta", client="beta-writer")
    assert (
        apply_plan(catalog, writer_beta, key="beta-apply", project="beta", client="beta-writer")[
            "project_id"
        ]
        == "beta"
    )
    with pytest.raises(Fault, match="forbidden"):
        apply_plan(catalog, beta, key="reader-apply", project="beta", client="reader")
    forged = json.loads(json.dumps(scan(catalog, "docs", project="beta")))
    forged["plan_id"] = directories.plan_fingerprint({**forged, "directory": "other"})
    with pytest.raises(Fault, match="invalid_plan"):
        apply_plan(catalog, forged, key="forged", project="beta")


def test_reader_may_preview_but_never_apply(catalog):
    plan = scan(catalog, client="reader")
    assert plan["client"] == "reader"
    with pytest.raises(Fault, match="forbidden"):
        apply_plan(catalog, plan, client="reader")
    assert documents(catalog.store) == {}


# -- conflicts -----------------------------------------------------------------


def test_changed_file_conflicts_and_is_never_imported_under_an_old_plan(catalog):
    plan = scan(catalog)
    (catalog.roots["alpha"] / "docs" / "design.md").write_text("Syntheticchangedmarker.\n")
    result = apply_plan(catalog, plan)
    assert result["status"] == "partial"
    assert [entry["reason"] for entry in result["conflicts"]] == ["source_changed"]
    assert result["remaining"] == ["docs/design.md"] and result["rescan"] is True
    assert documents(catalog.store) == {"docs/notes/retry.md": (1, "ready")}
    assert not catalog.run("query", {"text": "syntheticchangedmarker", "budget_bytes": 8192})[
        "blocks"
    ]
    # Only a fresh preview of the new bytes may import them.
    _, fresh = applied(catalog, "apply-2")
    assert {entry["outcome"] for entry in fresh["items"]} == {"imported", "unchanged"}
    assert catalog.run("query", {"text": "syntheticchangedmarker", "budget_bytes": 8192})["blocks"]


def test_added_and_removed_files_are_never_silently_imported_or_deleted(catalog):
    plan = scan(catalog)
    (catalog.roots["alpha"] / "docs" / "added.md").write_text("Syntheticaddedmarker.\n")
    (catalog.roots["alpha"] / "docs" / "notes" / "retry.md").unlink()
    result = apply_plan(catalog, plan)
    assert result["status"] == "partial"
    assert [entry["reason"] for entry in result["conflicts"]] == ["source_missing"]
    assert documents(catalog.store) == {"docs/design.md": (1, "ready")}
    assert not catalog.run("query", {"text": "syntheticaddedmarker", "budget_bytes": 8192})[
        "blocks"
    ]
    after = scan(catalog)
    # The added file was outside the confirmed preview, and a path that was never indexed is
    # not a disappearance: no import and no deletion is inferred from the listing alone.
    assert [entry["locator"] for entry in after["items"]] == ["docs/added.md", "docs/design.md"]
    assert after["missing"] == []
    assert not [entry for entry in after["omitted"] if "retry" in entry["locator"]]
    confirmed = apply_plan(catalog, after, key="apply-2")
    assert confirmed["status"] == "applied" and confirmed["tombstones"] == []
    assert documents(catalog.store) == {
        "docs/added.md": (1, "ready"),
        "docs/design.md": (1, "ready"),
    }
    assert (catalog.roots["alpha"] / "docs" / "design.md").exists()


def test_tombstone_requires_explicit_approval_and_never_deletes_the_original(catalog):
    applied(catalog)
    removed = catalog.roots["alpha"] / "docs" / "notes" / "retry.md"
    stored = catalog.roots["alpha"] / "docs" / "design.md"
    removed.unlink()
    after = scan(catalog)
    candidate = after["missing"][0]
    assert candidate["locator"] == "docs/notes/retry.md" and candidate["version"] == 1
    with pytest.raises(Fault, match="unapproved_tombstone"):
        apply_plan(catalog, after, key="apply-2", tombstones=["document:" + "0" * 8])
    result = apply_plan(catalog, after, key="apply-3", tombstones=[candidate["document_id"]])
    assert result["status"] == "applied"
    assert [entry["outcome"] for entry in result["tombstones"]] == ["deleted"]
    assert result["counts"]["tombstoned"] == 1
    assert documents(catalog.store)["docs/notes/retry.md"] == (2, "deleted")
    # The service never writes, moves or deletes a project file.
    assert stored.read_text(encoding="utf-8") == ALPHA_DESIGN
    assert not removed.exists()
    assert not catalog.run("query", {"text": "nested note", "budget_bytes": 8192})["blocks"]


def test_present_again_refuses_the_tombstone(catalog):
    applied(catalog)
    removed = catalog.roots["alpha"] / "docs" / "notes" / "retry.md"
    content = removed.read_text(encoding="utf-8")
    removed.unlink()
    after = scan(catalog)
    removed.write_text(content, encoding="utf-8")
    result = apply_plan(
        catalog, after, key="apply-2", tombstones=[after["missing"][0]["document_id"]]
    )
    assert [entry["outcome"] for entry in result["tombstones"]] == ["present_again"]
    assert result["remaining"] == ["docs/notes/retry.md"]
    assert documents(catalog.store)["docs/notes/retry.md"] == (1, "ready")


def test_concurrent_manual_version_conflicts_with_the_confirmed_plan(catalog, monkeypatch):
    applied(catalog)
    target = catalog.roots["alpha"] / "docs" / "design.md"
    plan = scan(catalog)
    assert item(plan, "docs/design.md")["action"] == "unchanged"
    entered, release = gate(monkeypatch, "observe", directories.observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(apply_plan, catalog, plan, key="apply-2")
        assert entered.wait(2)
        target.write_bytes(b"Manually revised content.\n")
        manual = catalog.run(
            "import",
            {
                "key": "manual",
                "kind": "file",
                "locator": "docs/design.md",
                "expected_version": 1,
                "groups": None,
            },
        )
        assert manual["status"] == "imported" and manual["version"] == 2
        target.write_text(ALPHA_DESIGN)
        release.set()
        result = waiting.result(timeout=5)
    assert result["status"] == "partial"
    assert [entry["reason"] for entry in result["conflicts"]] == ["version_conflict"]
    assert documents(catalog.store)["docs/design.md"] == (2, "ready")
    # The confirmed manual version keeps its own bytes: the old plan never rolls it back.
    with catalog.store.transaction() as db:
        stored = db.execute("SELECT hash FROM knowledge_versions WHERE version=2").fetchone()[0]
    assert stored == hashlib.sha256(b"Manually revised content.\n").hexdigest()
    assert not catalog.run("query", {"text": "manually revised", "budget_bytes": 8192})["blocks"]
    # A fresh preview of the current bytes imports them as the next version.
    _, fresh = applied(catalog, "apply-3")
    assert documents(catalog.store)["docs/design.md"] == (3, "ready")
    assert {entry["outcome"] for entry in fresh["items"]} == {"imported", "unchanged"}


def test_cancelled_apply_keeps_committed_items_and_is_resumable(catalog, monkeypatch):
    plan = scan(catalog)
    real = directories.import_item
    calls = {"count": 0}

    def stopping(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt("synthetic cancellation")
        return real(*args, **kwargs)

    monkeypatch.setattr(directories, "import_item", stopping)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(catalog, plan)
    monkeypatch.undo()
    assert documents(catalog.store) == {"docs/design.md": (1, "ready")}
    with pytest.raises(Fault, match="not_found"):
        catalog.run("status", {"key": "apply-1"})
    with catalog.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM knowledge_operations WHERE key='apply-1'").fetchone()
    # After a restart the same stored plan finishes the work the cancellation left behind.
    restarted = KnowledgeApplication(catalog.path)
    result = restarted.execute(
        {
            "operation": "directory_apply",
            "project_id": "alpha",
            "arguments": {"key": "apply-2", "plan": plan, "tombstones": []},
        },
        client="operator",
        credential=SECRET,
    )
    assert result["status"] == "applied"
    assert {entry["outcome"] for entry in result["items"]} == {"imported", "unchanged"}
    assert documents(catalog.store) == {
        "docs/design.md": (1, "ready"),
        "docs/notes/retry.md": (1, "ready"),
    }


def test_revocation_and_registration_change_fail_closed(catalog, monkeypatch):
    operator = catalog.config["knowledge"]["clients"]["operator"]
    plan = scan(catalog)
    operator["permissions"] = [
        permission for permission in operator["permissions"] if permission != "directory_apply"
    ]
    catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
    with pytest.raises(Fault, match="forbidden"):
        apply_plan(catalog, plan)
    assert documents(catalog.store) == {}
    operator["permissions"].append("directory_apply")
    catalog.path.write_text(canonical(catalog.config), encoding="utf-8")

    entered, release = gate(monkeypatch, "observe", directories.observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(apply_plan, catalog, plan, key="apply-2")
        assert entered.wait(2)
        operator["projects"] = []
        catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
        release.set()
        with pytest.raises(Fault, match="forbidden"):
            waiting.result(timeout=5)
    assert documents(catalog.store) == {}
    operator["projects"] = ["alpha", "beta"]
    catalog.config["knowledge"]["directories"]["alpha"] = [
        {"path": "docs", "max_files": 4, "max_bytes": 32768}
    ]
    catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
    with pytest.raises(Fault, match="registration_changed"):
        apply_plan(catalog, plan, key="apply-3")
    assert documents(catalog.store) == {}


def test_directory_registration_is_required_and_validated(catalog):
    with pytest.raises(Fault, match="directory_unregistered"):
        scan(catalog, "notes")
    with pytest.raises(Fault, match="directory_unregistered"):
        scan(catalog, "Docs")
    for path in ("../outside", "/outside", "docs/../outside", ".", "credentials", ".git"):
        catalog.config["knowledge"]["directories"]["alpha"] = [
            {"path": path, "max_files": 4, "max_bytes": 32768}
        ]
        catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
        with pytest.raises(Fault, match="invalid_configuration|directory_unregistered"):
            scan(catalog)
    for limits in (
        {"max_files": 0, "max_bytes": 32768},
        {"max_files": 4096, "max_bytes": 32768},
        {"max_files": 4, "max_bytes": 512},
        {"max_files": 4, "max_bytes": 64 * 1024 * 1024},
    ):
        catalog.config["knowledge"]["directories"]["alpha"] = [dict(path="docs", **limits)]
        catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
        with pytest.raises(Fault, match="invalid_configuration"):
            scan(catalog)
    catalog.config["knowledge"]["directories"].pop("alpha")
    catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
    with pytest.raises(Fault, match="directory_unregistered"):
        scan(catalog)


def test_project_isolation_between_two_registered_projects(catalog):
    _, alpha = applied(catalog)
    _, beta = applied(catalog, "beta-apply", project="beta", client="beta-writer")
    assert alpha["project_id"] == "alpha" and beta["project_id"] == "beta"
    assert documents(catalog.store) == {
        "docs/design.md": (1, "ready"),
        "docs/notes/retry.md": (1, "ready"),
    }
    with catalog.store.transaction() as db:
        rows = [dict(row) for row in db.execute("SELECT id,project_id FROM knowledge_documents")]
        assert {row["project_id"] for row in rows} == {"alpha", "beta"}
        assert len({row["id"] for row in rows}) == 4
    assert not catalog.run("query", {"text": "syntheticbetamarker", "budget_bytes": 8192})["blocks"]
    hit = catalog.run(
        "query", {"text": "syntheticbetamarker", "budget_bytes": 8192}, project="beta"
    )
    assert "check the table" in hit["blocks"][0]["text"]
    alpha_plan = scan(catalog, "docs", project="alpha")
    beta_plan = scan(catalog, "docs", project="beta")
    assert {entry["document_id"] for entry in alpha_plan["items"]} & {
        entry["document_id"] for entry in beta_plan["items"]
    } == set()
    assert beta_plan["limits"]["max_files"] == 4
    assert documents(catalog.store, "beta") == {
        "docs/design.md": (1, "ready"),
        "docs/notes/retry.md": (1, "ready"),
    }


def test_file_and_byte_limits_bound_each_apply_and_omissions_are_not_deletions(catalog):
    for name in ("one", "two", "three"):
        (catalog.roots["alpha"] / "docs" / f"{name}.md").write_text(f"{name}\n", encoding="utf-8")
    catalog.config["knowledge"]["directories"]["alpha"] = [
        {"path": "docs", "max_files": 2, "max_bytes": 1024}
    ]
    catalog.path.write_text(canonical(catalog.config), encoding="utf-8")
    first = scan(catalog)
    assert first["counts"]["import"] == 2 and first["budget_incomplete"] is True
    assert [entry["reason"] for entry in first["omitted"]].count("file_budget") == 3
    assert first["missing"] == []
    assert apply_plan(catalog, first, key="apply-1")["status"] == "applied"
    assert len(documents(catalog.store)) == 2
    second = scan(catalog)
    assert second["counts"]["import"] == 2
    assert apply_plan(catalog, second, key="apply-2")["status"] == "applied"
    assert len(documents(catalog.store)) == 4
    third = scan(catalog)
    assert third["counts"]["import"] == 1 and third["missing"] == []
    assert apply_plan(catalog, third, key="apply-3")["status"] == "applied"
    assert len(documents(catalog.store)) == 5
    # A file over the per-item byte limit is refused with a reason, never imported.
    (catalog.roots["alpha"] / "docs" / "big.md").write_text("x" * (1024 * 1024 + 1))
    (catalog.roots["alpha"] / "docs" / "b1.md").write_text("y" * 600)
    (catalog.roots["alpha"] / "docs" / "b2.md").write_text("z" * 600)
    fourth = scan(catalog)
    assert reason(fourth, "docs/big.md") == "source_too_large"
    assert [entry["reason"] for entry in fourth["omitted"]].count("byte_budget") == 1
    assert fourth["budget_incomplete"] is True
    assert fourth["missing"] == []


def test_directory_scan_and_apply_never_hold_the_shared_writer_lock(
    catalog, contracts, monkeypatch
):
    entered, release = gate(monkeypatch, "read_file", directories.read_file)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(scan, catalog)
        try:
            assert entered.wait(2)
            assert (
                pool.submit(chat_operation, catalog.store, contracts).result(timeout=1)["state"]
                == "found"
            )
        finally:
            release.set()
        plan = waiting.result(timeout=5)
    monkeypatch.undo()
    assert [entry["locator"] for entry in plan["items"]][0] == "docs/design.md"
    entered, release = gate(monkeypatch, "observe", directories.observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(apply_plan, catalog, plan)
        try:
            assert entered.wait(2)
            assert (
                pool.submit(chat_operation, catalog.store, contracts).result(timeout=1)["state"]
                == "found"
            )
        finally:
            release.set()
        assert waiting.result(timeout=5)["status"] == "applied"


def test_unmigrated_database_fails_closed_then_the_explicit_migration_adds_the_plan_book(
    catalog, tmp_path
):
    plan = scan(catalog)
    with catalog.store.transaction() as db:
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"DROP TRIGGER source_revision_knowledge_plans_{action}")
        db.execute("DROP TABLE knowledge_plans")
        db.execute("DELETE FROM metadata WHERE key='knowledge_directories_schema'")
    with pytest.raises(Fault, match="dependency_unavailable"):
        scan(catalog)
    with pytest.raises(Fault, match="dependency_unavailable"):
        apply_plan(catalog, plan)
    backup = tmp_path / "before-directories.sqlite"
    result = migrate_directories(catalog.store, backup)
    assert result["knowledge_directories_schema"] == 1 and result["backup"] == str(backup)
    with closing(sqlite3.connect(backup)) as reader:
        names = {row[0] for row in reader.execute("SELECT name FROM sqlite_master")}
    assert "knowledge_plans" not in names
    with pytest.raises(ValueError, match="already applied"):
        migrate_directories(catalog.store, tmp_path / "second.sqlite")
    with catalog.store.transaction() as db:
        triggers = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for table in DIRECTORY_TRACKED:
            for action in ("INSERT", "UPDATE", "DELETE"):
                assert f"source_revision_{table}_{action}" in triggers
        before = int(dict(db.execute("SELECT key,value FROM metadata"))["source_revision"])
    scan(catalog)
    with catalog.store.transaction() as db:
        after = int(dict(db.execute("SELECT key,value FROM metadata"))["source_revision"])
    # The plan book is tracked by the source guard, so recording a preview advances it.
    assert after == before + 1
    assert apply_plan(catalog, scan(catalog))["status"] == "applied"


def test_url_snapshot_and_directory_content_share_one_index(catalog, monkeypatch):
    monkeypatch.setattr(
        "tianshu_memory.knowledge.fetch_url",
        lambda *args: (b"URL receipt snapshot: never resend confirmed work.", "text/plain", URL),
    )
    plan, _ = applied(catalog)
    assert all(entry["kind"] == "file" for entry in plan["items"])
    url = catalog.run(
        "import",
        {"key": "url-1", "kind": "url", "locator": URL, "expected_version": 0, "groups": None},
    )
    assert url["status"] == "imported"
    hit = catalog.run("query", {"text": "receipt", "budget_bytes": 8192})
    assert {block["provenance"]["kind"] for block in hit["blocks"]} == {"file", "url"}
    after = scan(catalog)
    assert not [entry for entry in after["items"] if entry["kind"] == "url"]
    assert documents(catalog.store)[URL] == (1, "ready")
    assert apply_plan(catalog, after, key="apply-2")["status"] == "applied"
    assert documents(catalog.store)[URL] == (1, "ready")


def test_directory_apply_invalidates_recovery_lesson_and_experience_caches(catalog, monkeypatch):
    monkeypatch.setattr(
        "tianshu_memory.knowledge.fetch_url",
        lambda *args: (b"URL receipt snapshot: never resend confirmed work.", "text/plain", URL),
    )
    applied(catalog)
    applied(catalog, "beta-apply", project="beta", client="beta-writer")
    catalog.run(
        "import",
        {"key": "url-1", "kind": "url", "locator": URL, "expected_version": 0, "groups": None},
    )
    # Evidence is cited from the directory-imported document itself, not from the URL snapshot.
    directory_block = next(
        block
        for block in catalog.run("query", {"text": "receipt", "budget_bytes": 8192})["blocks"]
        if block["provenance"]["locator"] == "docs/design.md"
    )
    reference = directory_block["reference"]
    catalog.run(
        "write_state",
        {
            "key": "state",
            "expected_version": 0,
            "state": {
                "goal": "Keep receipt retries reliable",
                "constraints": ["Never resend confirmed work"],
                "recent_verification": ["Isolated directory apply passed"],
                "unfinished": ["Real repository run"],
                "evidence": [reference],
                "pitfalls": [],
            },
        },
    )
    package = catalog.run("recover", {"text": "receipt", "budget_bytes": 8192})
    assert catalog.run("check", {"package": package})["valid"] is True
    lessons = {}
    for name, client in (("alpha", "alpha-writer"), ("beta", "beta-writer")):
        lessons[name] = catalog.run(
            "lesson_record",
            {
                "key": f"{name}-lesson",
                "expected_version": 0,
                "lesson": {
                    **LESSON,
                    "evidence": [
                        catalog.run(
                            "query",
                            {
                                "text": (
                                    "syntheticbetamarker" if name == "beta" else "receipt absent"
                                ),
                                "budget_bytes": 8192,
                            },
                            project=name,
                            client=client,
                        )["blocks"][0]["reference"]
                    ],
                },
            },
            project=name,
            client=client,
        )
    promotion_evidence = sorted(
        (
            {
                "project_id": name,
                "lesson_id": lessons[name]["lesson_id"],
                "version": lessons[name]["version"],
                "hash": lessons[name]["hash"],
            }
            for name in ("alpha", "beta")
        ),
        key=lambda entry: entry["lesson_id"],
    )
    promoted = catalog.run(
        "experience_promote",
        {
            "key": "promotion",
            "expected_version": 0,
            "entry": {
                "title": "Check the receipt before retrying",
                "rule": "Read the receipt table before resending a timed-out request.",
                "applicability": ["idempotent delivery"],
                "excludes": ["outbound-only notifications"],
                "counterexamples": ["Fire-and-forget telemetry"],
                "recheck_after": "2027-01-01",
                "evidence": promotion_evidence,
            },
        },
    )
    assert promoted["status"] == "promoted"
    lesson_package = catalog.run(
        "lesson_recover", {"text": "receipt", "budget_bytes": 8192}, client="alpha-writer"
    )
    assert (
        catalog.run("lesson_check", {"package": lesson_package}, client="alpha-writer")["valid"]
        is True
    )
    with catalog.store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        references = ExperienceBook._references(db, promoted["entry_id"])
    seal_key = hmac.new(
        metadata["knowledge_seal_key"].encode(),
        canonical(["operator", digest(SECRET)]).encode(),
        hashlib.sha256,
    ).hexdigest()
    experience_package = {
        "entry_id": promoted["entry_id"],
        "version": promoted["version"],
        "effect": "live",
    }
    experience_package["seal"] = ExperienceBook.approval(
        promoted["entry_id"], promoted["version"], references, seal_key
    )
    live = catalog.run(
        "experience_check",
        {"entry_id": promoted["entry_id"], "package": experience_package},
    )
    assert live["valid"] is True and live["effect"] == "live"

    # Rewriting one directory source and importing the confirmed new version invalidates the
    # recovery package, the lesson content hash and the promoted entry that cites it.
    (catalog.roots["alpha"] / "docs" / "design.md").write_text(
        "Alpha receipt retry rule changed: receipts expire after one day.\n"
    )
    _, result = applied(catalog, "apply-2")
    assert result["status"] == "applied"
    assert documents(catalog.store)["docs/design.md"] == (2, "ready")
    assert catalog.run("check", {"package": package})["valid"] is False
    assert "expire after one day" in canonical(
        catalog.run("recover", {"text": "receipt", "budget_bytes": 8192})
    )
    assert (
        catalog.run("lesson_check", {"package": lesson_package}, client="alpha-writer")["valid"]
        is False
    )
    stale = catalog.run(
        "experience_check",
        {"entry_id": promoted["entry_id"], "package": experience_package},
    )
    assert stale["valid"] is False and stale["reason"] != "current"
    # The beta lesson and its directory source are untouched by alpha's rewrite.
    assert (
        lessons["beta"]["hash"]
        == catalog.run(
            "lesson_query",
            {"text": "receipt", "budget_bytes": 8192},
            project="beta",
            client="operator",
        )["lessons"][0]["hash"]
    )


def test_candidate_schema_and_examples_match_the_implementation():
    from jsonschema import Draft202012Validator

    directory = Path(__file__).resolve().parents[1] / "docs/candidates/project-directory-import/v1"
    validator = Draft202012Validator(json.loads((directory / "schema.json").read_text()))
    validator.check_schema(validator.schema)
    examples = json.loads((directory / "examples.json").read_text())
    assert [example["operation"] for example in examples] == ["directory_scan", "directory_apply"]
    for example in examples:
        validator.validate(example)
    # The published apply example is a real preview: the implementation accepts it unchanged.
    plan = examples[1]["arguments"]["plan"]
    assert plan["plan_id"] == directories.plan_fingerprint(plan)
    assert directories.plan_shape(json.loads(json.dumps(plan))) == plan
    assert directories.tombstone_ids(examples[1]["arguments"]["tombstones"], plan)


def test_cli_and_official_sdk_directory_chain(catalog):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = catalog.roots["alpha"]
    command = ["-m", "tianshu_memory.knowledge_cli", "--config", str(catalog.path)]
    env = dict(os.environ, TEST_DIRECTORY_SECRET=SECRET, PYTHONUTF8="1")

    def cli(name, payload, client="operator"):
        action = catalog.tmp_path / f"{name}.json"
        action.write_text(canonical(payload), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                *command,
                "action",
                "--client",
                client,
                "--credential-env",
                "TEST_DIRECTORY_SECRET",
                str(action),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=30,
        )

    preview = cli(
        "scan",
        {
            "operation": "directory_scan",
            "project_id": "alpha",
            "arguments": {"directory": "docs"},
        },
    )
    assert preview.returncode == 0, preview.stderr
    plan = json.loads(preview.stdout)
    assert [entry["locator"] for entry in plan["items"]] == [
        "docs/design.md",
        "docs/notes/retry.md",
    ]
    confirmed = cli(
        "apply",
        {
            "operation": "directory_apply",
            "project_id": "alpha",
            "arguments": {"key": "cli-apply", "plan": plan, "tombstones": []},
        },
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert json.loads(confirmed.stdout)["status"] == "applied"
    recorded = cli(
        "status",
        {"operation": "status", "project_id": "alpha", "arguments": {"key": "cli-apply"}},
    )
    assert recorded.returncode == 0
    assert json.loads(recorded.stdout)["plan_id"] == plan["plan_id"]
    # A partial apply exits non-zero: confirmed items were refused, so it is not success.
    (root / "docs" / "design.md").write_text("Changed before the CLI confirmation.\n")
    partial = cli(
        "apply-partial",
        {
            "operation": "directory_apply",
            "project_id": "alpha",
            "arguments": {"key": "cli-partial", "plan": plan, "tombstones": []},
        },
    )
    assert partial.returncode == 1
    assert json.loads(partial.stdout)["status"] == "partial"

    async def exercise():
        server = StdioServerParameters(
            command=sys.executable,
            args=[
                *command,
                "mcp",
                "--client",
                "operator",
                "--credential-env",
                "TEST_DIRECTORY_SECRET",
            ],
            env=env,
            cwd=str(root),
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"knowledge_directory_scan", "knowledge_directory_apply"} <= names
                previewed = await session.call_tool(
                    "knowledge_directory_scan", {"project_id": "alpha", "directory": "docs"}
                )
                assert not previewed.isError and "docs/design.md" in str(previewed.content)
                unregistered = await session.call_tool(
                    "knowledge_directory_scan", {"project_id": "alpha", "directory": "notes"}
                )
                assert unregistered.isError
                tampered = json.loads(json.dumps(plan))
                tampered["items"][0]["digest"] = "0" * 64
                refused = await session.call_tool(
                    "knowledge_directory_apply",
                    {"project_id": "alpha", "key": "mcp", "plan": tampered, "tombstones": []},
                )
                assert refused.isError

    asyncio.run(exercise())
    assert Path(catalog.store.path).exists()
