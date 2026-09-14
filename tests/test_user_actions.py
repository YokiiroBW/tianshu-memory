"""Real local CLI credentials + Memory HTTP/SQLite + synthetic HTTPS owners.

These are product tests of the new entry point, not Core/Platform joint acceptance.
"""

import copy
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from source_sync_harness import SyncHarness
from test_auth_https import certificates as certificates
from test_process import server
from test_source_sync import sync as sync
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory.domain import Fault, canonical
from tianshu_memory.store import Store
from tianshu_memory.user_actions import LocalUserApplication, credential_digest
from tianshu_memory.workflow import TrustedWorkflow

SECRET = "synthetic-local-user-independent-credential-0123456789"
EXPIRES = "2030-01-01T00:00:00Z"


def setup_user(sync, *, migrate=True):
    if migrate:
        sync.store.migrate_users(sync.directory / "before-users.sqlite")
    permissions = []
    for actor in (0, 1):
        for kind, category, role in (
            ("person", "interest", "owner"),
            ("person", "style", "curator"),
            ("group", "topic", "curator"),
            ("group", "style", "curator"),
        ):
            for sharing in ("group_only", "public_preference"):
                if sharing == "public_preference" and (kind, category) != ("person", "interest"):
                    continue
                permissions.append(
                    dict(
                        role=role,
                        actor_id=sync.scope(actor)["actor_id"],
                        subject_kind=kind,
                        category=category,
                        sharing=sharing,
                        conversation_id=sync.scope(actor)["conversation_id"]
                        if sharing == "group_only"
                        else None,
                    )
                )
    sync.config["local_users"] = {
        "owner": dict(
            credential_sha256=credential_digest(SECRET),
            account=sync.account,
            actors=["actor:a", "actor:b"],
            revision_scopes=[sync.scope(), sync.scope(1)],
            profile_permissions=permissions,
        )
    }
    sync.save()
    return LocalUserApplication(sync.service, sync.config_path)


def execute(app, action, credential=SECRET):
    return app.execute(action, principal="owner", credential=credential)


def cli(sync, action, *, secret=SECRET, success=True):
    path = sync.directory / "explicit-action.json"
    path.write_text(canonical(action), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tianshu_memory.cli import main; main()",
            "--config",
            str(sync.config_path),
            "user-action",
            str(path),
            "--principal",
            "owner",
            "--credential-env",
            "TEST_LOCAL_USER_CREDENTIAL",
        ],
        env=dict(os.environ, TEST_LOCAL_USER_CREDENTIAL=secret, PYTHONUTF8="1"),
        cwd=sync.directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    assert SECRET not in result.stdout + result.stderr
    return json.loads(result.stdout)


def revision(sync, record, *, kind="forget"):
    return dict(
        command=sync.command(),
        record_id=record,
        expected_version=1,
        revision_kind=kind,
        confirmation_ref="explicit-confirmation:" + sync.command()["request_id"],
        evidence_refs=sync.event()["sources"],
        replacement_statement="仅上午喝茶" if kind == "correct" else None,
    )


def draft(sync, *, actor=0, kind="person", category="interest", sharing=None):
    scope = sync.scope(actor)
    sharing = sharing or ("group_only" if scope["audience"] == "group" else "public_preference")
    unit = copy.deepcopy(sync.examples["memory/candidate-commit"]["drafts"][0]["units"][0])
    unit["sources"] = sync.event(actor)["sources"]
    return dict(
        source_scope=scope,
        subject={
            "kind": kind,
            "person_id" if kind == "person" else "conversation_id": sync.person
            if kind == "person"
            else scope["conversation_id"],
        },
        sharing=sharing,
        conversation_id=scope["conversation_id"] if sharing == "group_only" else None,
        category=category,
        field_key=category + ".coffee",
        units=[unit],
    )


def profile_action(draft, *, operation="approve_profile", ref=None, actor=0):
    action = dict(
        operation=operation, draft=draft, origin={"assertion_ref": f"synthetic-viewer:{actor}"}
    )
    action["expires_at" if operation == "approve_profile" else "approval_ref"] = (
        EXPIRES if operation == "approve_profile" else ref
    )
    return action


def profile_read(sync, draft, *, known=None, budget=100000, context=None):
    context = context or sync.contexts["synthetic-viewer:0"]
    request = sync.profile_request(context, known=known, budget=budget, target=draft["subject"])
    request["selection"] = [draft["category"]]
    response = sync.post("memory/profiles/select", request)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["forget", "correct"])
def test_cli_confirm_http_revise_restart_actor_and_profile_isolation(sync, kind):
    app = setup_user(sync)
    first, _, _ = sync.seed()
    second, _, _ = sync.seed(1)
    projection = draft(sync)
    approval = cli(sync, profile_action(projection))
    published = cli(sync, profile_action(projection, operation="publish_profile", ref=approval))
    before = profile_read(sync, projection)
    assert before["selected_units"][0]["record_id"] == published["record_ids"][0]
    request = revision(sync, first["record_ids"][0], kind=kind)
    action = dict(operation="confirm_revision", request=request, expires_at=EXPIRES)
    assert sync.post("memory/revise", request).status_code == 403
    proof = cli(sync, action)
    assert not proof["consumed"] and proof["binding_version"] == 1
    assert cli(sync, action) == proof
    with server(sync.config_path) as client:
        response = client.post("/internal/v1/memory/revise", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["authoritative_state"] == (
            "tombstoned" if kind == "forget" else "corrected"
        )
        assert response.json()["semantic_state"] == "invalidated"
    with server(sync.config_path) as client:
        retry = client.post("/internal/v1/memory/revise", json=request)
        assert retry.status_code == 200 and retry.json() == response.json()
        assert (
            client.post("/internal/v1/memory/select", json=sync.selection()).json()[
                "selected_units"
            ]
            == []
        )
        remaining = client.post("/internal/v1/memory/select", json=sync.selection(actor=1)).json()
        assert [u["record_id"] for u in remaining["selected_units"]] == second["record_ids"]
    after = profile_read(sync, projection)
    assert after["selected_units"] == [] and after["scope_version"] > before["scope_version"]
    with pytest.raises(Fault):
        execute(app, profile_action(projection, operation="publish_profile", ref=approval))
    with Store(sync.store.path).transaction() as db:
        assert (
            db.execute(
                "SELECT consumed FROM confirmations WHERE ref=?", (proof["confirmation_ref"],)
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM physical_sources WHERE state='active'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        "secret",
        "service_token",
        "unconfigured",
        "account",
        "actor",
        "scope",
        "revoked",
        "self_reported",
        "expired",
    ],
)
def test_local_entry_rejects_credentials_scope_and_self_reported_approval(sync, change):
    setup_user(sync)
    seeded, _, _ = sync.seed()
    request = revision(sync, seeded["record_ids"][0])
    action = dict(operation="confirm_revision", request=request, expires_at=EXPIRES)
    secret = SECRET
    if change == "secret":
        secret = "wrong-independent-credential-01234567890"
    elif change == "service_token":
        secret = "service-token-longer-than-thirty-two-characters"
        sync.config["callers"]["companion"]["token"] = secret
        sync.config["local_users"]["owner"]["credential_sha256"] = credential_digest(secret)
    elif change == "unconfigured":
        sync.config.pop("local_users")
    elif change in {"account", "actor", "scope"}:
        registration = sync.config["local_users"]["owner"]
        if change == "account":
            registration["account"] = dict(sync.account, immutable_account_id="someone-else")
        elif change == "actor":
            registration["actors"] = ["actor:b"]
        else:
            registration["revision_scopes"] = [sync.scope(1)]
    elif change == "revoked":
        sync.contexts["synthetic-viewer:0"]["revoked"] = True
    elif change == "self_reported":
        action.update(verified_context=sync.contexts["synthetic-viewer:0"], confirmed=True)
    else:
        action["expires_at"] = "2020-01-01T00:00:00Z"
    sync.save()
    result = cli(sync, action, secret=secret, success=False)
    assert result["status"] in {400, 401, 403, 503}
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change", ["draft", "binding", "source", "permission", "expired", "scope", "principal"]
)
def test_profile_approval_rejects_changed_dependencies(sync, change):
    app = setup_user(sync)
    projection = draft(sync)
    approval = execute(app, profile_action(projection))
    if change == "draft":
        projection["units"][0]["negations"] = ["different wording"]
    elif change == "binding":
        with sync.store.transaction() as db:
            db.execute("UPDATE accounts SET version=version+1")
    elif change == "source":
        sync.physicals[0].update(
            revision=2, kind="edit", content_digest="f" * 64, physical_receipt_id="changed-source"
        )
        sync.core_head["sequence"] += 1
    elif change == "permission":
        sync.config["local_users"]["owner"]["profile_permissions"] = []
        sync.save()
    elif change == "expired":
        with sync.store.transaction() as db:
            db.execute("UPDATE profile_approvals SET expires_at='2020-01-01T00:00:00Z'")
    elif change == "scope":
        projection["source_scope"]["actor_id"] = "actor:b"
    else:
        sync.config["local_users"]["owner"] = dict(
            sync.config["local_users"]["owner"], disabled=True
        )
        sync.save()
    with pytest.raises(Fault):
        execute(app, profile_action(projection, operation="publish_profile", ref=approval))
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM profile_shares").fetchone()[0] == 0


def test_profile_revoke_pending_and_published_durable_idempotent(sync):
    app = setup_user(sync)
    projection = draft(sync)
    pending = cli(sync, profile_action(projection))
    withdrawal = profile_action(projection, operation="revoke_profile", ref=pending)
    assert cli(sync, withdrawal)["state"] == "revoked"
    assert cli(sync, withdrawal)["state"] == "revoked"
    with pytest.raises(Fault):
        execute(app, profile_action(projection, operation="publish_profile", ref=pending))
    approval = cli(sync, profile_action(projection))
    publish = profile_action(projection, operation="publish_profile", ref=approval)
    result = cli(sync, publish)
    assert cli(sync, publish) == result
    before = profile_read(sync, projection)
    assert before["selected_units"]
    cli(sync, profile_action(projection, operation="revoke_profile", ref=approval))
    after = profile_read(sync, projection)
    assert not after["selected_units"] and after["scope_version"] > before["scope_version"]
    assert cli(sync, publish, success=False)["status"] == 403
    assert sync.post("memory/select", sync.selection()).status_code == 200
    assert profile_read(sync, projection)["scope_version"] == after["scope_version"]


@pytest.mark.parametrize(
    "kind,category",
    [("person", "interest"), ("person", "style"), ("group", "topic"), ("group", "style")],
)
def test_group_categories_require_exact_registered_authority(group_sync, kind, category):
    sync = group_sync
    app = setup_user(sync)
    projection = draft(sync, kind=kind, category=category)
    registration = copy.deepcopy(sync.config["local_users"]["owner"])
    sync.config["local_users"]["owner"]["profile_permissions"] = []
    sync.save()
    with pytest.raises(Fault, match="forbidden"):
        execute(app, profile_action(projection))
    sync.config["local_users"]["owner"] = registration
    sync.save()
    approval = execute(app, profile_action(projection))
    result = execute(app, profile_action(projection, operation="publish_profile", ref=approval))
    selected = profile_read(sync, projection)
    assert [u["record_id"] for u in selected["selected_units"]] == result["record_ids"]
    assert selected["selected_units"][0]["subject"] == projection["subject"]
    broader = copy.deepcopy(projection)
    broader.update(sharing="public_preference", conversation_id=None, category="interest")
    with pytest.raises(Fault):
        execute(app, profile_action(broader, operation="publish_profile", ref=approval))


def test_unconfigured_profile_and_arbitrary_true_remain_unavailable(sync):
    from types import SimpleNamespace

    for workflow in (
        TrustedWorkflow(sync.service),
        TrustedWorkflow(
            sync.service,
            SimpleNamespace(verify_approval=lambda _: True),
            SimpleNamespace(verify_approval=lambda _: True),
        ),
    ):
        with pytest.raises(Fault) as error:
            workflow.approve_profile({}, {}, EXPIRES)
        assert error.value.status == 503


def test_user_migration_backup_guard_and_rollback_detection(sync):
    sync.seed()
    before = sync.store.recovery_path.read_bytes()
    setup_user(sync)
    assert sync.store.recovery_path.read_bytes() != before
    backup = sync.directory / "before-users.sqlite"
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] == "3"
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        assert (
            db.execute("SELECT 1 FROM metadata WHERE key='local_users_schema'").fetchone() is None
        )
    with pytest.raises(FileExistsError):
        sync.store.migrate_users(backup)
    with pytest.raises(ValueError):
        sync.store.migrate_users(sync.directory / "duplicate.sqlite")
    with (
        closing(sqlite3.connect(backup)) as reader,
        closing(sqlite3.connect(sync.store.path)) as dest,
    ):
        reader.backup(dest)
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(sync.store.path)


@pytest.fixture
def group_sync(tmp_path, source_contracts, source_examples, certificates, monkeypatch):
    harness = SyncHarness(tmp_path, source_contracts, source_examples, certificates, "group")
    with harness.owners(), harness.runtime(monkeypatch):
        yield harness


def test_group_curator_is_deployment_authorized_and_cannot_approve_others_interest(group_sync):
    sync = group_sync
    app = setup_user(sync)
    author = sync.person
    curator = copy.deepcopy(sync.contexts["synthetic-first"])
    curator["assertion_ref"] = "synthetic-curator"
    curator["verified_account"] = dict(sync.account, immutable_account_id="group-curator")
    sync.contexts["synthetic-curator"] = curator
    command = sync.command()
    command["origin"]["assertion_ref"] = "synthetic-curator"
    response = sync.post(
        "identity/register", dict(command=command, account=curator["verified_account"])
    )
    assert response.status_code == 200, response.text
    curator["allowed_scope"] = dict(sync.scope(), person_id=response.json()["person_id"])
    sync.config["local_users"]["owner"]["account"] = curator["verified_account"]
    sync.save()
    projection = draft(sync, kind="group", category="topic")
    action = profile_action(projection)
    action["origin"]["assertion_ref"] = "synthetic-curator"
    approval = execute(app, action)
    publish = profile_action(projection, operation="publish_profile", ref=approval)
    publish["origin"]["assertion_ref"] = "synthetic-curator"
    execute(app, publish)
    assert profile_read(sync, projection, context=curator)["selected_units"]
    other_interest = draft(sync)
    assert other_interest["subject"]["person_id"] == author
    action = profile_action(other_interest)
    action["origin"]["assertion_ref"] = "synthetic-curator"
    with pytest.raises(Fault, match="forbidden"):
        execute(app, action)


def test_group_withdrawal_does_not_advance_private_public_epoch(group_sync):
    sync = group_sync
    app = setup_user(sync)
    group = draft(sync)
    public = draft(sync, sharing="public_preference")
    group_ref = execute(app, profile_action(group))
    public_ref = execute(app, profile_action(public))
    execute(app, profile_action(group, operation="publish_profile", ref=group_ref))
    public_result = execute(
        app, profile_action(public, operation="publish_profile", ref=public_ref)
    )
    private = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
    private["assertion_ref"] = "synthetic-private-reader"
    private["allowed_scope"].update(
        audience="self_private", conversation_id="private-separate-conversation"
    )
    sync.contexts[private["assertion_ref"]] = private
    before = profile_read(sync, public, context=private)
    assert [u["record_id"] for u in before["selected_units"]] == public_result["record_ids"]
    execute(app, profile_action(group, operation="revoke_profile", ref=group_ref))
    after = profile_read(sync, public, context=private)
    assert after["scope_version"] == before["scope_version"]
    assert after["selected_units"] == before["selected_units"]


def test_source_negative_commits_before_confirmation_rejection(sync):
    app = setup_user(sync)
    seeded, _, _ = sync.seed()
    request = revision(sync, seeded["record_ids"][0])
    sync.physicals[0].update(
        revision=2,
        kind="edit",
        content_digest="e" * 64,
        physical_receipt_id="edited-before-confirmation",
    )
    sync.core_head["sequence"] += 1
    with pytest.raises(Fault):
        execute(app, dict(operation="confirm_revision", request=request, expires_at=EXPIRES))
    with Store(sync.store.path).transaction() as db:
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (seeded["group_ids"][0],)).fetchone()[
                0
            ]
            != "active"
        )
        assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_approval_rechecks_binding_changed_during_https_barrier(sync):
    app = setup_user(sync)
    projection = draft(sync)
    changed = False

    def mutate(kind, body, response):
        nonlocal changed
        if kind == "snapshot" and not changed:
            changed = True
            with sync.store.transaction() as db:
                db.execute("UPDATE accounts SET version=version+1")

    sync.mutate = mutate
    with pytest.raises(Fault):
        execute(app, profile_action(projection))
    assert changed
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM profile_approvals").fetchone()[0] == 0


def test_user_migration_exception_rolls_back_and_retains_backup(sync, monkeypatch):
    from tianshu_memory import source_recovery

    original = source_recovery.persist
    before = sync.store.recovery_path.read_bytes()

    def fail_before_checkpoint(*args, **kwargs):
        raise OSError("synthetic checkpoint storage failure")

    monkeypatch.setattr(source_recovery, "persist", fail_before_checkpoint)
    backup = sync.directory / "failed-users-backup.sqlite"
    with pytest.raises(OSError):
        sync.store.migrate_users(backup)
    monkeypatch.setattr(source_recovery, "persist", original)
    assert backup.exists() and sync.store.recovery_path.read_bytes() == before
    with Store(sync.store.path).transaction() as db:
        assert (
            db.execute("SELECT 1 FROM metadata WHERE key='local_users_schema'").fetchone() is None
        )
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='profile_approval_authorities'"
            ).fetchone()
            is None
        )
    sync.store.migrate_users(sync.directory / "retry-users-backup.sqlite")


@pytest.fixture
def private_sync(tmp_path, source_contracts, source_examples, certificates, monkeypatch):
    harness = SyncHarness(tmp_path, source_contracts, source_examples, certificates, "self_private")
    with harness.owners(), harness.runtime(monkeypatch):
        yield harness


def test_owner_private_interest_can_share_to_exact_current_group(private_sync):
    sync = private_sync
    app = setup_user(sync)
    # Preserve the real private admission shape; only the viewer enters another allowed group.
    sync.contexts["synthetic-viewer:0"]["allowed_scope"].update(
        audience="group", conversation_id="current-group"
    )
    sync.config["local_users"]["owner"]["profile_permissions"].append(
        dict(
            role="owner",
            actor_id="actor:a",
            subject_kind="person",
            category="interest",
            sharing="group_only",
            conversation_id="current-group",
        )
    )
    sync.save()
    projection = draft(sync, sharing="group_only")
    projection["conversation_id"] = "current-group"
    approval = execute(app, profile_action(projection))
    result = execute(app, profile_action(projection, operation="publish_profile", ref=approval))
    assert [u["record_id"] for u in profile_read(sync, projection)["selected_units"]] == result[
        "record_ids"
    ]
    bad = copy.deepcopy(projection)
    bad.update(category="style", field_key="style.coffee")
    with pytest.raises(Fault, match="forbidden"):
        execute(app, profile_action(bad))


def test_pending_revocation_cannot_be_lost_by_database_only_restore(sync):
    app = setup_user(sync)
    projection = draft(sync)
    approval = execute(app, profile_action(projection))
    backup = sync.directory / "before-pending-revoke.sqlite"
    with (
        closing(sqlite3.connect(sync.store.path)) as reader,
        closing(sqlite3.connect(backup)) as dest,
    ):
        reader.backup(dest)
    execute(app, profile_action(projection, operation="revoke_profile", ref=approval))
    with (
        closing(sqlite3.connect(backup)) as reader,
        closing(sqlite3.connect(sync.store.path)) as dest,
    ):
        reader.backup(dest)
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(sync.store.path)


def test_profile_requires_explicit_migration_and_preserves_complete_units(sync):
    app = setup_user(sync, migrate=False)
    projection = draft(sync)
    projection["units"].append(
        dict(
            projection["units"][0],
            statement="咖啡只选低因",
            conditions=["睡眠充足时"],
            negations=["晚上不喝"],
            uncertainty="uncertain",
        )
    )
    with pytest.raises(Fault) as error:
        execute(app, profile_action(projection))
    assert error.value.status == 503
    sync.store.migrate_users(sync.directory / "explicit-users.sqlite")
    approval = cli(sync, profile_action(projection))
    result = cli(sync, profile_action(projection, operation="publish_profile", ref=approval))
    selected = profile_read(sync, projection)
    assert len(selected["selected_units"]) == 2
    assert set(result["record_ids"]) == {u["record_id"] for u in selected["selected_units"]}
    assert any(
        u["conditions"] == ["睡眠充足时"]
        and u["negations"] == ["晚上不喝"]
        and u["uncertainty"] == "uncertain"
        for u in selected["selected_units"]
    )
