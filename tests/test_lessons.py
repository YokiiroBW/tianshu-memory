"""Project lesson book and explicitly approved global experience.

Two registered synthetic projects, isolated files, DB and credentials. Every promotion and
review path runs through KnowledgeApplication, so authorization, idempotency and evidence
freshness are checked exactly as in production use.
"""

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.knowledge import KnowledgeApplication
from tianshu_memory.knowledge_migration import TRACKED, migrate
from tianshu_memory.lessons import ExperienceBook, lesson_id
from tianshu_memory.lessons_migration import migrate as migrate_lessons
from tianshu_memory.lessons_schema import TRACKED as LESSON_TRACKED
from tianshu_memory.store import Store

SECRET = "synthetic-project-client-secret"
OTHER_SECRET = "synthetic-second-client-secret"
REVIEW_SECRET = "synthetic-reviewer-client-secret"
PROJECT_WRITE = ["import", "query", "recover", "check", "write_state", "delete", "status"]
LESSON_WRITE = ["lesson_record", "lesson_revise", "lesson_retire"]
LESSON_READ = ["lesson_query", "lesson_recover", "lesson_check"]
EXPERIENCE_READ = ["experience_query", "experience_check"]
EXPERIENCE_WRITE = ["experience_promote", "experience_revoke", "experience_withdraw"]


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def registered_clients():
    return {
        "alpha-writer": {
            "credential_sha256": digest(SECRET),
            "projects": ["alpha"],
            "permissions": [*PROJECT_WRITE, *LESSON_WRITE, *LESSON_READ],
        },
        "beta-writer": {
            "credential_sha256": digest(OTHER_SECRET),
            "projects": ["beta"],
            "permissions": [*PROJECT_WRITE, *LESSON_WRITE, *LESSON_READ],
        },
        "operator": {
            "credential_sha256": digest(REVIEW_SECRET),
            "projects": ["alpha", "beta"],
            "permissions": [
                *PROJECT_WRITE,
                *LESSON_WRITE,
                *LESSON_READ,
                *EXPERIENCE_READ,
                *EXPERIENCE_WRITE,
                "promote",
                "review",
            ],
        },
        "reviewer": {
            "credential_sha256": digest(REVIEW_SECRET),
            "projects": ["alpha"],
            "permissions": ["query", *EXPERIENCE_READ, "review"],
        },
        "author": {
            "credential_sha256": digest(REVIEW_SECRET),
            "projects": ["alpha", "beta"],
            "permissions": ["query", *EXPERIENCE_READ, *EXPERIENCE_WRITE, "promote", "review"],
        },
    }


CREDENTIALS = {
    "alpha-writer": SECRET,
    "beta-writer": OTHER_SECRET,
    "operator": REVIEW_SECRET,
    "reviewer": REVIEW_SECRET,
    "author": REVIEW_SECRET,
}


@pytest.fixture
def lessons(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "lessons.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    roots = {}
    for name, content in (
        ("alpha", "Alpha retries must wait for a receipt before resending.\nSecond alpha line.\n"),
        ("beta", "Beta evidence: check the receipt table before any retry.\nSecond beta line.\n"),
    ):
        root = tmp_path / name
        root.mkdir()
        (root / "notes.md").write_text(content, encoding="utf-8")
        roots[name] = root
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {
                name: {
                    "root": str(root),
                    "host": "local",
                    "default_branch": "main",
                    "urls": [],
                }
                for name, root in roots.items()
            },
            "clients": registered_clients(),
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    app = KnowledgeApplication(path)

    def run(operation, arguments, *, project="alpha", client="operator", credential=None):
        return app.execute(
            dict(operation=operation, project_id=project, arguments=arguments),
            client=client,
            credential=CREDENTIALS[client] if credential is None else credential,
        )

    return SimpleNamespace(
        run=run, app=app, store=store, path=path, config=config, roots=roots, tmp_path=tmp_path
    )


def imported(harness, project="alpha", key=None, expected_version=0):
    result = harness.run(
        "import",
        dict(
            key=key or f"{project}-import",
            kind="file",
            locator="notes.md",
            expected_version=expected_version,
            groups=None,
        ),
        project=project,
        client=f"{project}-writer",
    )
    assert result["status"] == "imported", result
    return result


def reference(harness, project="alpha", text="receipt"):
    hit = harness.run(
        "query", dict(text=text, budget_bytes=8192), project=project, client=f"{project}-writer"
    )
    assert hit["blocks"], hit
    return hit["blocks"][0]["reference"]


def lesson(harness, project="alpha", evidence=None, **changes):
    payload = {
        "trigger": "Send timed out before the receipt arrived",
        "symptom": "The same message is delivered twice",
        "cause": "Retry did not read the receipt table first",
        "correction": "Read the receipt table and skip confirmed work",
        "verification": "Isolated two-project regression run passed",
        "scope": {
            "platform": "windows",
            "language": "python",
            "framework": "stdlib",
            "applies_to": ["idempotent delivery"],
            "excludes": ["outbound-only notifications"],
        },
        "evidence": evidence if evidence is not None else [reference(harness, project)],
    }
    payload.update(changes)
    return payload


def record(
    harness,
    project="alpha",
    key=None,
    op=None,
    client=None,
    expected_version=0,
    evidence="auto",
    **changes,
):
    """Record one lesson. `evidence="auto"` collects the current block for that project.

    `key` names the lesson (its identity); `op` is this request's idempotency key, so a
    test can record under a stable lesson identity with a fresh operation key.
    """
    collected = None if evidence == "auto" else evidence
    lesson_key = key or f"{project}-lesson"
    return harness.run(
        "lesson_record",
        dict(
            key=lesson_key,
            dedupe=op or lesson_key,
            expected_version=expected_version,
            lesson=lesson(harness, project, evidence=collected, **changes),
        ),
        project=project,
        client=client or f"{project}-writer",
    )


def entry(references, **changes):
    payload = {
        "title": "Check the receipt before retrying",
        "rule": "Always read the receipt table before resending a timed-out request.",
        "applicability": ["idempotent delivery"],
        "excludes": ["outbound-only notifications"],
        "counterexamples": ["Fire-and-forget telemetry"],
        "recheck_after": "2027-01-01",
        "evidence": references,
    }
    payload.update(changes)
    return payload


def promote(
    harness, references, *, key=None, op=None, client="operator", expected_version=0, **changes
):
    """Approve one global experience entry.

    `key` names the entry (its identity); `op` is this request's idempotency key, so an
    operator can re-approve the same entry with a fresh operation key.
    """
    entry_key = key or "promotion"
    return harness.run(
        "experience_promote",
        dict(
            key=entry_key,
            dedupe=op or entry_key,
            expected_version=expected_version,
            entry=entry(references, **changes),
        ),
        project="alpha",
        client=client,
    )


def two_projects(harness):
    imported(harness, "alpha")
    imported(harness, "beta")
    first = record(harness, "alpha", key="alpha-lesson", op="alpha-body")
    second = record(harness, "beta", key="beta-lesson", op="beta-body")
    return first, second


def shared_references(harness):
    first = harness.run(
        "lesson_query",
        dict(text="receipt", budget_bytes=8192),
        project="alpha",
        client="operator",
    )["lessons"][0]
    second = harness.run(
        "lesson_query",
        dict(text="receipt", budget_bytes=8192),
        project="beta",
        client="operator",
    )["lessons"][0]
    references = [
        {
            "project_id": "alpha",
            "lesson_id": first["lesson_id"],
            "version": first["version"],
            "hash": first["hash"],
        },
        {
            "project_id": "beta",
            "lesson_id": second["lesson_id"],
            "version": second["version"],
            "hash": second["hash"],
        },
    ]
    # Approval is order-insensitive; the server records and returns them sorted.
    return sorted(references, key=lambda reference: reference["lesson_id"])


def live_check(harness, entry_id, version, client="operator"):
    """Build the exact package the server would issue, then check it."""
    with harness.store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        references = ExperienceBook._references(db, entry_id)
        # The server derives its seal key from the registered credential digest, never the secret.
        seal_key = hmac.new(
            metadata["knowledge_seal_key"].encode(),
            canonical([client, digest(CREDENTIALS[client])]).encode(),
            hashlib.sha256,
        ).hexdigest()
    package = {"entry_id": entry_id, "version": version, "effect": "live"}
    return package, ExperienceBook.approval(entry_id, version, references, seal_key)


# -- lesson book ---------------------------------------------------------------


def test_record_query_and_check_lesson_with_current_evidence(lessons):
    imported(lessons)
    recorded = record(lessons, "alpha")
    assert recorded["status"] == "recorded" and recorded["version"] == 1
    assert recorded["sharing"] == "project_only"
    assert recorded["authority"] == "explicit_operator_lesson"
    hit = lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=8192), project="alpha", client="operator"
    )
    assert len(hit["lessons"]) == 1
    assert hit["lessons"][0]["lesson_id"] == recorded["lesson_id"]
    assert hit["lessons"][0]["hash"] == recorded["hash"]
    assert hit["lessons"][0]["scope"]["platform"] == "windows"
    assert hit["retrieval"] == "lexical" and hit["omissions"] == []
    assert not lessons.run(
        "lesson_query", dict(text="zebras", budget_bytes=8192), project="alpha", client="operator"
    )["lessons"]
    tight = lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=256), project="alpha", client="operator"
    )
    assert tight["lessons"] == [] and tight["omissions"] == ["budget"]
    package = lessons.run(
        "lesson_recover",
        dict(text="receipt", budget_bytes=8192),
        project="alpha",
        client="operator",
    )
    assert package["goal"] is None and package["lessons"]
    assert len(canonical(package).encode()) <= 8192
    assert lessons.run("lesson_check", dict(package=package), project="alpha", client="operator")[
        "valid"
    ]
    assert (
        lessons.run("lesson_check", dict(package=package), project="alpha", client="alpha-writer")[
            "valid"
        ]
        is False
    )
    forged = dict(package, lessons=[dict(package["lessons"][0], trigger="forged")])
    assert not lessons.run(
        "lesson_check", dict(package=forged), project="alpha", client="operator"
    )["valid"]


def test_lesson_idempotency_versions_and_conflicts(lessons):
    imported(lessons)
    first = record(lessons, "alpha")
    assert record(lessons, "alpha")["replayed"]
    assert first["lesson_id"] == lesson_id("alpha", "alpha-lesson")
    with pytest.raises(Fault, match="idempotency_conflict"):
        record(lessons, "alpha", symptom="different content")
    with pytest.raises(Fault, match="version_conflict"):
        record(lessons, "alpha", key="alpha-lesson-stale", expected_version=2)
    # A revision repeats the recording key, which is what identifies the lesson. The
    # operation key is separate, so the same lesson body may be submitted again.
    revises = {
        "key": "alpha-lesson",
        "dedupe": "alpha-lesson@1",
        "lesson_id": first["lesson_id"],
        "expected_version": 1,
    }
    revised = lessons.run(
        "lesson_revise",
        dict(
            revises,
            lesson=lesson(lessons, "alpha", correction="Read the receipt table, then skip"),
        ),
        project="alpha",
        client="alpha-writer",
    )
    assert revised["status"] == "revised" and revised["version"] == 2
    assert revised["hash"] != first["hash"]
    # A stale expected_version is refused, and a key naming another lesson is refused.
    with pytest.raises(Fault, match="version_conflict"):
        lessons.run(
            "lesson_revise",
            dict(revises, dedupe="alpha-lesson@1-stale", lesson=lesson(lessons, "alpha")),
            project="alpha",
            client="alpha-writer",
        )
    with pytest.raises(Fault, match="identity_conflict"):
        lessons.run(
            "lesson_revise",
            dict(
                key="unrelated-key",
                dedupe="unrelated@2",
                lesson_id=first["lesson_id"],
                expected_version=2,
                lesson=lesson(lessons, "alpha"),
            ),
            project="alpha",
            client="alpha-writer",
        )
    with pytest.raises(Fault, match="idempotency_conflict"):
        lessons.run(
            "lesson_revise",
            dict(revises, lesson=lesson(lessons, "alpha", symptom="changed on replay")),
            project="alpha",
            client="alpha-writer",
        )
    with lessons.store.transaction() as db:
        versions = [
            row[0]
            for row in db.execute(
                "SELECT version FROM lesson_history WHERE lesson_id=? ORDER BY version",
                (first["lesson_id"],),
            )
        ]
        assert versions == [1, 2]


def test_retired_lesson_stops_being_recallable(lessons):
    imported(lessons)
    first = record(lessons, "alpha")
    retired = lessons.run(
        "lesson_retire",
        dict(key="retire", lesson_id=first["lesson_id"], expected_version=1, reason="superseded"),
        project="alpha",
        client="alpha-writer",
    )
    assert retired["status"] == "retired" and retired["version"] == 2
    assert retired["global_effect"] == "requires_review"
    with pytest.raises(Fault, match="already_retired"):
        lessons.run(
            "lesson_retire",
            dict(key="retire-2", lesson_id=first["lesson_id"], expected_version=2, reason="again"),
            project="alpha",
            client="alpha-writer",
        )
    assert not lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=8192), project="alpha", client="operator"
    )["lessons"]
    with lessons.store.transaction() as db:
        row = db.execute(
            "SELECT state,version FROM lessons WHERE id=?", (first["lesson_id"],)
        ).fetchone()
        assert (row["state"], row["version"]) == ("retired", 2)


@pytest.mark.parametrize(
    "change, code",
    [
        ({"hash": "0" * 64}, "stale_evidence"),
        ({"version": 99}, "stale_evidence"),
        ({"document_id": "document:" + "0" * 64}, "not_found"),
        ({"block_id": "missing"}, "stale_evidence"),
    ],
)
def test_tampered_or_stale_evidence_is_rejected(lessons, change, code):
    imported(lessons)
    evidence = dict(reference(lessons, "alpha"))
    evidence.update(change)
    with pytest.raises(Fault, match=code):
        record(lessons, "alpha", evidence=[evidence])


def test_lesson_without_evidence_and_wrong_shape_is_rejected(lessons):
    imported(lessons)
    with pytest.raises(Fault, match="evidence_required"):
        record(lessons, "alpha", evidence=[])
    with pytest.raises(Fault, match="invalid_input"):
        record(lessons, "alpha", unexpected="field")
    with pytest.raises(Fault, match="invalid_input"):
        record(lessons, "alpha", scope={"platform": "windows"})
    with pytest.raises(Fault, match="invalid_input"):
        lessons.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192, extra=True),
            project="alpha",
            client="operator",
        )


def test_evidence_must_belong_to_the_current_project(lessons):
    imported(lessons, "alpha")
    imported(lessons, "beta")
    beta_reference = reference(lessons, "beta")
    # Document lookup is project scoped, even for an operator allowed to read both.
    with pytest.raises(Fault, match="not_found"):
        record(lessons, "alpha", evidence=[beta_reference])
    with pytest.raises(Fault, match="not_found"):
        record(lessons, "alpha", client="operator", evidence=[beta_reference])


def test_authorization_is_per_operation_project_and_client(lessons):
    imported(lessons, "alpha")
    recorded = record(lessons, "alpha")
    assert recorded["lesson_id"].startswith("lesson:")
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "lesson_record",
            dict(key="cross", expected_version=0, lesson=lesson(lessons, "alpha")),
            project="alpha",
            client="beta-writer",
        )
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192),
            project="beta",
            client="reviewer",
        )
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "experience_query",
            dict(text="receipt", budget_bytes=256, project_id=None),
            client="alpha-writer",
        )
    with pytest.raises(Fault, match="unauthorized"):
        lessons.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
            credential="incorrect-credential-value",
        )
    # A project with no lesson book yet reports no lessons, and never another project's.
    imported(lessons, "beta")
    assert not lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=8192), project="beta", client="operator"
    )["lessons"]
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="beta-writer",
        )
    imported(lessons, "beta")
    record(lessons, "beta", client="beta-writer")
    assert (
        len(
            lessons.run(
                "lesson_query",
                dict(text="receipt", budget_bytes=8192),
                project="beta",
                client="operator",
            )["lessons"]
        )
        == 1
    )
    assert (
        len(
            lessons.run(
                "lesson_query",
                dict(text="receipt", budget_bytes=8192),
                project="alpha",
                client="operator",
            )["lessons"]
        )
        == 1
    )


def test_lessons_require_explicit_migration(tmp_path, contracts):
    """A database with project knowledge but no lessons refuses lesson operations."""
    contracts.load_sources()
    store = Store(tmp_path / "installed.sqlite")
    store.migrate_profiles(tmp_path / "before-profiles.sqlite")
    store.migrate_sources(tmp_path / "before-sources.sqlite", contracts)
    migrate(store, tmp_path / "before-knowledge.sqlite")
    # Simulate the pre-TS-081 state: knowledge installed, lesson objects absent.
    with store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='lessons_schema'")
        for table in [*LESSON_TRACKED, "lesson_index", "experience_index"]:
            db.execute(f"DROP TABLE {table}")
    root = tmp_path / "registered"
    root.mkdir()
    (root / "notes.md").write_text("Receipt evidence.", encoding="utf-8")
    config = {
        "database_path": store.path,
        "knowledge": {
            "projects": {
                "alpha": {
                    "root": str(root),
                    "host": "local",
                    "default_branch": "main",
                    "urls": [],
                }
            },
            "clients": registered_clients(),
        },
    }
    path = tmp_path / "private.json"
    path.write_text(canonical(config), encoding="utf-8")
    app = KnowledgeApplication(path)
    with pytest.raises(Fault, match="dependency_unavailable"):
        app.execute(
            dict(
                operation="lesson_query",
                project_id="alpha",
                arguments=dict(text="receipt", budget_bytes=256),
            ),
            client="operator",
            credential=REVIEW_SECRET,
        )
    upgrade = migrate_lessons(store, tmp_path / "before-lessons.sqlite")
    assert upgrade["lessons_schema"] == 1
    with store.transaction() as db:
        triggers = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for table in LESSON_TRACKED:
            for action in ("INSERT", "UPDATE", "DELETE"):
                assert f"source_revision_{table}_{action}" in triggers
    # Registering the project requires the knowledge write path, not the lesson path.
    app.execute(
        dict(
            operation="import",
            project_id="alpha",
            arguments=dict(
                key="alpha-import", kind="file", locator="notes.md", expected_version=0, groups=None
            ),
        ),
        client="alpha-writer",
        credential=SECRET,
    )
    assert (
        app.execute(
            dict(
                operation="lesson_query",
                project_id="alpha",
                arguments=dict(text="receipt", budget_bytes=256),
            ),
            client="operator",
            credential=REVIEW_SECRET,
        )["lessons"]
        == []
    )
    with pytest.raises(ValueError, match="already applied"):
        migrate_lessons(store, tmp_path / "before-lessons-2.sqlite")
    with pytest.raises(ValueError, match="distinct"):
        migrate_lessons(store, store.path)


def test_lessons_migration_requires_project_knowledge_schema(tmp_path, contracts):
    contracts.load_sources()
    store = Store(tmp_path / "plain.sqlite")
    with pytest.raises(ValueError, match="requires project knowledge"):
        migrate_lessons(store, tmp_path / "backup.sqlite")


def test_guard_tracks_lessons_and_experience_tables(lessons):
    with lessons.store.transaction() as db:
        triggers = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for table in [*TRACKED, *LESSON_TRACKED]:
            for action in ("INSERT", "UPDATE", "DELETE"):
                assert f"source_revision_{table}_{action}" in triggers
        before = int(
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        )
    imported(lessons)
    record(lessons, "alpha")
    with lessons.store.transaction() as db:
        after = int(
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        )
    assert after > before


# -- global experience ---------------------------------------------------------


def test_promotion_requires_two_projects_and_protects_project_text(lessons):
    two_projects(lessons)
    references = shared_references(lessons)
    with pytest.raises(Fault, match="evidence_required"):
        promote(lessons, references[:1], key="single-reference")
    # Two lessons from the same project are still one project's worth of evidence.
    repeated = [
        references[0],
        dict(references[0], lesson_id="lesson:" + "9" * 64),
    ]
    with pytest.raises(Fault, match="insufficient_evidence"):
        promote(lessons, repeated, key="duplicate-project")
    with pytest.raises(Fault, match="evidence_required"):
        promote(lessons, [dict(references[0], lesson_id="other-lesson")], key="one-entry")
    promoted = promote(lessons, references)
    assert promoted["status"] == "promoted" and promoted["version"] == 1
    assert promoted["evidence_projects"] == ["alpha", "beta"]
    assert promoted["sharing"] == "explicit_operator_approval"
    hit = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert len(hit["entries"]) == 1
    assert hit["entries"][0]["evidence"] == "protected"
    serialized = canonical(hit)
    assert "Alpha retries must wait" not in serialized
    assert "notes.md" not in serialized
    assert "alpha" not in canonical(hit["entries"][0])
    scoped = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id="beta"),
        project="alpha",
        client="operator",
    )
    assert len(scoped["entries"]) == 1
    assert [item["project_id"] for item in scoped["entries"][0]["evidence"]] == ["beta"]
    empty = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="author",
    )
    assert len(empty["entries"]) == 1


def test_promotion_requires_explicit_promote_permission(lessons):
    two_projects(lessons)
    references = shared_references(lessons)
    # A project writer has no promote permission even inside its own project.
    with pytest.raises(Fault, match="forbidden"):
        promote(lessons, references, client="alpha-writer")
    # A caller with promote for alpha may not cite a project it cannot read, even when the
    # envelope itself names the project it does hold.
    lessons.config["knowledge"]["clients"]["reviewer"]["permissions"] = [
        "query",
        "experience_promote",
        "promote",
    ]
    lessons.path.write_text(canonical(lessons.config))
    with pytest.raises(Fault, match="forbidden"):
        promote(lessons, references, client="reviewer")
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "experience_promote",
            dict(key="beta-promotion", expected_version=0, entry=entry(references)),
            project="beta",
            client="reviewer",
        )


def test_global_retrieval_never_leaks_unauthorized_projects(lessons):
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    # The reviewer may read alpha only, so an entry that also cites beta is not a candidate at
    # all: it must not appear, must not add an omission and must not displace a legal entry.
    hidden = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="reviewer",
    )
    assert hidden["entries"] == [] and hidden["omissions"] == []
    # The same query issued by a caller who may read both projects reports the real state.
    visible = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert [entry["entry_id"] for entry in visible["entries"]] == [promoted["entry_id"]]
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "experience_query",
            dict(text="receipt", budget_bytes=8192, project_id="beta"),
            project="alpha",
            client="reviewer",
        )
    checked = lessons.run(
        "experience_check",
        dict(
            entry_id=promoted["entry_id"],
            package={"entry_id": promoted["entry_id"], "seal": "0" * 64},
        ),
        project="alpha",
        client="reviewer",
    )
    assert not checked["valid"] and checked["effect"] == "unavailable"
    assert checked["version"] is None
    # The project filter never returns another project's lesson citation.
    filtered = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id="alpha"),
        project="alpha",
        client="operator",
    )
    assert len(filtered["entries"]) == 1
    assert [item["project_id"] for item in filtered["entries"][0]["evidence"]] == ["alpha"]


def test_source_withdrawal_invalidates_experience_and_its_cached_check(lessons):
    first, second = two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    package, seal = live_check(lessons, promoted["entry_id"], promoted["version"])
    package["seal"] = seal
    assert lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )["valid"]
    withdrawn = lessons.run(
        "experience_withdraw",
        dict(
            key="withdraw",
            entry_id=promoted["entry_id"],
            expected_version=1,
            lesson_id=second["lesson_id"],
            reason="internal detail",
        ),
        project="beta",
        client="operator",
    )
    assert withdrawn["status"] == "withdrawn" and withdrawn["effect"] == "unavailable"
    after = lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )
    assert not after["valid"] and after["effect"] == "withdrawn"
    queries = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert queries["entries"] == [] and queries["omissions"] == ["withdrawn"]
    # Another project's lesson is not resolvable from here at all, and a project writer
    # lacks the review permission needed to change global sharing.
    with pytest.raises(Fault, match="not_found"):
        lessons.run(
            "experience_withdraw",
            dict(
                key="withdraw-2",
                entry_id=promoted["entry_id"],
                expected_version=1,
                lesson_id=second["lesson_id"],
                reason="not mine",
            ),
            project="alpha",
            client="operator",
        )
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "experience_withdraw",
            dict(
                key="withdraw-3",
                entry_id=promoted["entry_id"],
                expected_version=1,
                lesson_id=first["lesson_id"],
                reason="writer lacks review permission",
            ),
            project="alpha",
            client="alpha-writer",
        )


def test_lesson_revision_and_source_delete_put_experience_out_of_use(lessons):
    first, second = two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    package, seal = live_check(lessons, promoted["entry_id"], promoted["version"])
    package["seal"] = seal
    assert lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )["valid"]
    revised = lessons.run(
        "lesson_revise",
        dict(
            key="alpha-lesson",
            dedupe="alpha-lesson@1",
            lesson_id=first["lesson_id"],
            expected_version=1,
            lesson=lesson(lessons, "alpha", correction="Read the receipt table first"),
        ),
        project="alpha",
        client="alpha-writer",
    )
    assert revised["version"] == 2
    superseded = lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )
    assert not superseded["valid"] and superseded["effect"] == "superseded"
    assert (
        lessons.run(
            "experience_query",
            dict(text="receipt", budget_bytes=8192, project_id=None),
            project="alpha",
            client="operator",
        )["entries"]
        == []
    )
    with lessons.store.transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM experience_entries WHERE id=?", (promoted["entry_id"],)
            ).fetchone()[0]
            == "active"
        )


def test_deleted_source_makes_the_lesson_unrecallable(lessons):
    two_projects(lessons)
    blocked = imported(lessons, "beta", key="beta-import")
    document_id = reference(lessons, "beta")["document_id"]
    lessons.run(
        "delete",
        dict(key="delete-beta", document_id=document_id, expected_version=blocked["version"]),
        project="beta",
        client="beta-writer",
    )
    recall = lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=8192), project="beta", client="operator"
    )
    assert recall["lessons"] == [] and recall["omissions"] == ["stale_source"]


# -- current evidence validation for promoted experience -----------------------


def test_current_file_change_stops_experience_reuse(lessons):
    """A replaced source file must invalidate the promoted entry, not stay reported live."""
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    package, seal = live_check(lessons, promoted["entry_id"], promoted["version"])
    package["seal"] = seal
    assert lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )["valid"]
    (lessons.roots["alpha"] / "notes.md").write_text(
        "The receipt guidance was replaced.", encoding="utf-8"
    )
    after = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert after["entries"] == []
    assert after["omissions"] == ["stale_source"]
    stale = lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )
    assert not stale["valid"] and stale["effect"] == "stale_source"
    # A package whose seal and claimed effect no longer match the entry is refused too.
    assert not lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=dict(package, effect="live")),
        project="alpha",
        client="operator",
    )["valid"]


def test_reimported_document_version_stops_experience_reuse(lessons):
    """A new document version invalidates the entry even when the text only grew."""
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    (lessons.roots["beta"] / "notes.md").write_text(
        "Beta evidence: check the receipt table before any retry.\nSecond beta line.\nExtra line.\n",
        encoding="utf-8",
    )
    replacement = imported(lessons, "beta", key="beta-reimport", expected_version=1)
    assert replacement["status"] == "imported" and replacement["version"] == 2
    check = lessons.run(
        "experience_check",
        dict(
            entry_id=promoted["entry_id"], package=live_check(lessons, promoted["entry_id"], 1)[0]
        ),
        project="alpha",
        client="operator",
    )
    assert not check["valid"] and check["effect"] in {"stale_source", "superseded"}
    assert (
        lessons.run(
            "experience_query",
            dict(text="receipt", budget_bytes=8192, project_id=None),
            project="alpha",
            client="operator",
        )["entries"]
        == []
    )


def test_deleted_or_tombstoned_source_stops_experience_reuse(lessons):
    """Deleting the evidence document must not leave the entry reported as live."""
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    document_id = reference(lessons, "alpha")["document_id"]
    deleted = lessons.run(
        "delete",
        dict(key="delete-alpha", document_id=document_id, expected_version=1),
        project="alpha",
        client="alpha-writer",
    )
    assert deleted["status"] == "deleted"
    after = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert after["entries"] == []
    assert after["omissions"] == ["stale_source"]
    assert (
        lessons.run(
            "experience_check",
            dict(
                entry_id=promoted["entry_id"],
                package=live_check(lessons, promoted["entry_id"], 1)[0],
            ),
            project="alpha",
            client="operator",
        )["effect"]
        == "stale_source"
    )


def test_project_permission_revocation_stops_experience_reuse(lessons):
    """Losing read access to a source project makes the entry invisible and checked invalid."""
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    package, seal = live_check(lessons, promoted["entry_id"], promoted["version"])
    package["seal"] = seal
    assert lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )["valid"]
    # The caller keeps beta but loses alpha, so the entry is no longer fully readable.
    lessons.config["knowledge"]["clients"]["operator"]["projects"] = ["beta"]
    lessons.path.write_text(canonical(lessons.config))
    checked = lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="beta",
        client="operator",
    )
    assert not checked["valid"] and checked["effect"] == "unavailable"
    assert checked["version"] is None
    queried = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="beta",
        client="operator",
    )
    assert queried["entries"] == [] and queried["omissions"] == []
    # The lost project itself is refused outright, before anything is read.
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "experience_query",
            dict(text="receipt", budget_bytes=8192, project_id="alpha"),
            project="alpha",
            client="operator",
        )


def test_unauthorized_entry_cannot_shift_omissions_or_displace_candidates(lessons):
    """Global retrieval must not let an unreadable entry be observed through its hits."""
    two_projects(lessons)
    references = shared_references(lessons)
    hidden = promote(lessons, references)
    # The operator owns a legal entry whose text also matches "receipt".
    visible = promote(
        lessons,
        references,
        key="visible-promotion",
        op="visible@1",
        title="Alpha only receipt rule",
    )
    operator = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert {entry["entry_id"] for entry in operator["entries"]} == {
        hidden["entry_id"],
        visible["entry_id"],
    }
    assert operator["omissions"] == []
    # The reviewer may read alpha only: the entry citing beta is not a candidate at all.
    reviewer = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="reviewer",
    )
    assert reviewer["entries"] == []
    assert reviewer["omissions"] == []
    miss = lessons.run(
        "experience_query",
        dict(text="zzzznonexistent", budget_bytes=8192, project_id=None),
        project="alpha",
        client="reviewer",
    )
    assert miss["entries"] == [] and miss["omissions"] == []


def test_unauthorized_expired_entry_reports_nothing(lessons):
    """An unreadable entry that also expired must not be observable in any form."""
    two_projects(lessons)
    references = shared_references(lessons)
    promote(lessons, references)
    (lessons.roots["beta"] / "notes.md").write_text("Beta text replaced.", encoding="utf-8")
    reviewer = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="reviewer",
    )
    assert reviewer["entries"] == [] and reviewer["omissions"] == []


def test_authorized_expiry_is_explained_while_unreadable_is_not(lessons):
    """A caller who may read the sources gets the real reason; a partial caller gets none."""
    two_projects(lessons)
    references = shared_references(lessons)
    promote(lessons, references)
    (lessons.roots["beta"] / "notes.md").write_text("Beta text replaced.", encoding="utf-8")
    operator = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )
    assert operator["entries"] == [] and operator["omissions"] == ["stale_source"]
    reviewer = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="reviewer",
    )
    assert reviewer["entries"] == [] and reviewer["omissions"] == []


def test_revoke_requires_explicit_review_permission_and_blocks_reuse(lessons):
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    with pytest.raises(Fault, match="forbidden"):
        lessons.run(
            "experience_revoke",
            dict(key="revoke", entry_id=promoted["entry_id"], expected_version=1, reason="wrong"),
            project="alpha",
            client="alpha-writer",
        )
    revoked = lessons.run(
        "experience_revoke",
        dict(
            key="revoke",
            entry_id=promoted["entry_id"],
            expected_version=1,
            reason="no longer valid",
        ),
        project="alpha",
        client="operator",
    )
    assert revoked["status"] == "revoked" and revoked["version"] == 2
    assert revoked["reusable"] is False
    assert not lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=8192, project_id=None),
        project="alpha",
        client="operator",
    )["entries"]
    reapproved = promote(lessons, references, op="promotion@2", expected_version=2)
    assert reapproved["status"] == "reapproved" and reapproved["version"] == 3
    assert (
        lessons.run(
            "experience_query",
            dict(text="receipt", budget_bytes=8192, project_id=None),
            project="alpha",
            client="operator",
        )["entries"][0]["effect"]
        == "live"
    )
    with pytest.raises(Fault, match="version_conflict"):
        lessons.run(
            "experience_revoke",
            dict(key="revoke-2", entry_id=promoted["entry_id"], expected_version=1, reason="stale"),
            project="alpha",
            client="operator",
        )


def test_experience_query_is_bounded_and_check_reports_effects(lessons):
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    package, seal = live_check(lessons, promoted["entry_id"], promoted["version"])
    package["seal"] = seal
    live = lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=package),
        project="alpha",
        client="operator",
    )
    assert live["valid"] and live["reason"] == "current" and live["effect"] == "live"
    assert live["version"] == promoted["version"]
    assert not lessons.run(
        "experience_check",
        dict(entry_id=promoted["entry_id"], package=dict(package, effect="revoked")),
        project="alpha",
        client="operator",
    )["valid"]
    assert not lessons.run(
        "experience_check",
        dict(entry_id="experience:" + "0" * 64, package=package),
        project="alpha",
        client="operator",
    )["valid"]
    tight = lessons.run(
        "experience_query",
        dict(text="receipt", budget_bytes=256, project_id=None),
        project="alpha",
        client="operator",
    )
    assert tight["entries"] == [] and tight["omissions"] == ["budget"]


def test_global_promotion_is_idempotent_and_never_auto_grants(lessons):
    two_projects(lessons)
    references = shared_references(lessons)
    promoted = promote(lessons, references)
    replay = promote(lessons, references)
    assert replay["replayed"] and replay["entry_id"] == promoted["entry_id"]
    with pytest.raises(Fault, match="idempotency_conflict"):
        promote(lessons, references, title="different title")
    with pytest.raises(Fault, match="version_conflict"):
        promote(lessons, references, op="stale-version", expected_version=3)
    with pytest.raises(Fault, match="evidence_required"):
        promote(lessons, references[:1], key="short-reapproval", expected_version=1)
    with pytest.raises(Fault, match="evidence_required|insufficient_evidence"):
        promote(lessons, references[:1], key="too-few")
    # Re-approval targets the same entry by its original key and current version.
    reapproved = promote(lessons, references, op="promotion@1", expected_version=1)
    assert reapproved["status"] == "reapproved" and reapproved["version"] == 2
    with lessons.store.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM experience_history WHERE entry_id=?", (promoted["entry_id"],)
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM experience_entries WHERE project_id='alpha'"
            ).fetchone()[0]
            == 1
        )


def test_candidate_schema_and_examples_cover_new_operations():
    from jsonschema import Draft202012Validator

    directory = Path(__file__).resolve().parents[1] / "docs/candidates/project-lessons/v2"
    schema = json.loads((directory / "schema.json").read_text())
    validator = Draft202012Validator(schema)
    validator.check_schema(schema)
    for example in json.loads((directory / "examples.json").read_text()):
        validator.validate(example)
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
    } <= set(schema["properties"]["operation"]["enum"])
