import copy
import sqlite3

import pytest

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.store import Store


def profile_draft(
    h,
    *,
    sharing="public_preference",
    subject=None,
    source_scope=None,
    conversation=None,
    category="interest",
    field="interest.coffee",
    units=None,
):
    return {
        "source_scope": source_scope or h.private,
        "subject": subject or {"kind": "person", "person_id": h.person},
        "sharing": sharing,
        "conversation_id": conversation,
        "category": category,
        "field_key": field,
        "units": units or [h.unit()],
    }


def publish(h, draft, origin="origin-private"):
    context = h.config["origins"][origin]
    approval = h.workflow.approve_profile(draft, context, "2027-01-01T00:00:00Z")
    return h.workflow.publish_profile(draft, approval, context)


@pytest.fixture
def p(h):
    h.store.migrate_profiles(h.directory / "before-profiles.sqlite")
    h.seed()
    return h


def test_explicit_migration_preserves_v1_and_backup(h):
    h.seed()
    before = h.select()
    with pytest.raises(Fault, match="dependency_unavailable"):
        publish(h, profile_draft(h))
    backup = h.directory / "rollback.sqlite"
    h.store.migrate_profiles(backup)
    assert h.select()["selected_units"] == before["selected_units"]
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == "1"
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='profile_shares'").fetchone()
            is None
        )
    with pytest.raises(FileExistsError):
        h.store.migrate_profiles(backup)
    with Store(h.store.path).transaction() as db:
        Store.require_profiles(db)
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_profile_requires_exact_live_approval_and_subject(p):
    h = p
    draft, context = profile_draft(h), h.config["origins"]["origin-private"]
    with pytest.raises(Fault, match="forbidden"):
        h.workflow.publish_profile(draft, "invented", context)
    ref = h.workflow.approve_profile(draft, context, "2027-01-01T00:00:00Z")
    changed = copy.deepcopy(draft)
    changed["units"][0]["statement"] = "未经批准的不同兴趣"
    with pytest.raises(Fault, match="forbidden"):
        h.workflow.publish_profile(changed, ref, context)
    result = h.workflow.publish_profile(draft, ref, context)
    assert h.workflow.publish_profile(draft, ref, context) == result
    changed = dict(draft, subject={"kind": "person", "person_id": "someone-else"})
    with pytest.raises(Fault, match="forbidden"):
        publish(h, changed)
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM profile_shares").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM relationship_entries").fetchone()[0] == 0


def test_group_source_needs_separate_author_approval_to_share_publicly(p):
    h = p
    source = h.source("group-message")
    h.workflow.observe_source(source, h.group)
    draft = profile_draft(
        h,
        sharing="group_only",
        source_scope=h.group,
        conversation=h.group["conversation_id"],
        units=[h.unit(source)],
    )
    context = h.config["origins"]["origin-group"]
    ref = h.workflow.approve_profile(draft, context, "2027-01-01T00:00:00Z")
    h.workflow.publish_profile(draft, ref, context)
    changed = dict(draft, sharing="public_preference", conversation_id=None)
    with pytest.raises(Fault, match="forbidden"):
        h.workflow.publish_profile(changed, ref, context)
    publish(h, changed, "origin-group")
    with pytest.raises(Fault, match="forbidden"):
        publish(h, dict(draft, conversation_id="different-group"), "origin-group")


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_original_revision_atomically_invalidates_all_profile_projections(p, kind):
    h = p
    one = publish(h, profile_draft(h))
    two = publish(
        h,
        profile_draft(h, sharing="group_only", conversation=h.group["conversation_id"]),
        "origin-group",
    )
    with h.store.transaction() as db:
        original = db.execute(
            "SELECT r.id FROM records r JOIN groups g ON g.id=r.group_id WHERE g.scope=?",
            (canonical(h.private),),
        ).fetchone()[0]
    response = h.post("memory/revise", h.revision(original, kind))
    assert response.status_code == 200, response.text
    with h.store.transaction() as db:
        for result in (one, two):
            assert (
                db.execute("SELECT state FROM groups WHERE id=?", (result["group_id"],)).fetchone()[
                    0
                ]
                == "invalidated"
            )
            assert db.execute(
                "SELECT state FROM records WHERE id=?", (result["record_ids"][0],)
            ).fetchone()[0] == ("tombstoned" if kind == "forget" else "invalidated")
        assert (
            db.execute("SELECT COUNT(*) FROM projections WHERE state='active'").fetchone()[0] == 0
        )
    with pytest.raises(Fault, match="version_conflict"):
        publish(h, profile_draft(h))


def test_pending_approval_cannot_restore_withdrawn_source(p):
    h = p
    draft, context = profile_draft(h), h.config["origins"]["origin-private"]
    ref = h.workflow.approve_profile(draft, context, "2027-01-01T00:00:00Z")
    h.workflow.observe_source(h.source(), h.private, state="withdrawn")
    with pytest.raises(Fault, match="version_conflict"):
        h.workflow.publish_profile(draft, ref, context)
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT consumed FROM profile_approvals WHERE ref=?", (ref,)).fetchone()[0]
            == 0
        )


def test_group_theme_never_comes_from_person_private_source(p):
    with pytest.raises(Fault, match="forbidden"):
        publish(
            p,
            profile_draft(
                p,
                sharing="group_only",
                category="topic",
                field="topic.technology",
                conversation=p.group["conversation_id"],
                subject={"kind": "group", "conversation_id": p.group["conversation_id"]},
            ),
            "origin-group",
        )


def test_public_sharing_cannot_publish_style_or_group_subject(p):
    with pytest.raises(Fault, match="invalid_input"):
        publish(p, profile_draft(p, category="style", field="style.expression"))


def test_expired_or_revoked_approval_context_cannot_publish(p):
    h = p
    draft, context = profile_draft(h), h.config["origins"]["origin-private"]
    ref = h.workflow.approve_profile(draft, context, "2027-01-01T00:00:00Z")
    with pytest.raises(Fault, match="forbidden"):
        h.workflow.publish_profile(draft, ref, dict(context, revoked=True))
    with h.store.transaction() as db:
        db.execute(
            "UPDATE profile_approvals SET expires_at='2026-01-01T00:00:00Z' WHERE ref=?", (ref,)
        )
    with pytest.raises(Fault, match="forbidden"):
        h.workflow.publish_profile(draft, ref, context)
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM profile_shares").fetchone()[0] == 0


def test_migration_failure_rolls_back_ddl_and_keeps_backup(h, monkeypatch):
    from tianshu_memory import store

    h.seed()
    monkeypatch.setattr(store, "PROFILE_SCHEMA", store.PROFILE_SCHEMA + "INVALID SQL;")
    backup = h.directory / "failed-migration-backup.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        h.store.migrate_profiles(backup)
    with h.store.transaction() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == "1"
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='profile_shares'").fetchone()
            is None
        )
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    assert h.select()["selected_units"]


@pytest.mark.parametrize("outcome", ["success", "backup_failure", "ddl_failure"])
def test_migration_closes_connections_and_releases_backup_file(h, monkeypatch, outcome):
    from tianshu_memory import store

    h.seed()
    opened = []  # Keep strong references so garbage collection cannot mask missing close().
    connect = sqlite3.connect

    class FailingBackup(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            raise sqlite3.OperationalError("injected backup failure")

    def tracked_connect(*args, **kwargs):
        if outcome == "backup_failure":
            kwargs["factory"] = FailingBackup
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    backup = h.directory / "connection-release.sqlite"
    with monkeypatch.context() as patch:
        patch.setattr(store.sqlite3, "connect", tracked_connect)
        if outcome == "ddl_failure":
            patch.setattr(store, "PROFILE_SCHEMA", store.PROFILE_SCHEMA + "INVALID SQL;")
        if outcome == "success":
            h.store.migrate_profiles(backup)
        else:
            with pytest.raises(sqlite3.OperationalError):
                h.store.migrate_profiles(backup)
    assert len(opened) == 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    released = h.directory / "released-backup.sqlite"
    backup.rename(released)  # Also exercises immediate Windows file-handle release.
    assert released.is_file()
