"""TS-114 synthetic authority/transaction tests; no production DB or messages."""

import copy
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tianshu_memory.domain import Fault, canonical, source_key
from tianshu_memory.relationship_migration import migrate
from tianshu_memory.relationships import Policy, Relationships
from tianshu_memory.relationships.legacy import migration_report
from tianshu_memory.relationships.schema import TRACKED
from tianshu_memory.relationships.timestamps import stamp


@pytest.fixture
def relationship(h):
    h.store.migrate_profiles(h.directory / "before-profiles.sqlite")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "before-sources.sqlite", h.contracts)
    clock = [datetime(2026, 10, 1, tzinfo=UTC)]
    h.service.clock = lambda: clock[0]
    h.auth.clock = h.service.clock
    migrate(h.store, h.directory / "before-relationships.sqlite", clock=h.service.clock)
    caller = h.config["callers"]["companion"]
    caller.update(issuer="platform", role_admin=True)
    caller["operations"].append("relationships.manage")
    for origin in h.config["origins"].values():
        origin.update(
            issuer="platform", principal_id="synthetic-admin", expires_at="2099-01-01T00:00:00Z"
        )
    h.save_config()
    app = Relationships(h.service, auth=h.auth)
    return h, app, clock


def context(h, group=False):
    return copy.deepcopy(h.config["origins"]["origin-group" if group else "origin-private"])


def command(h, app, operation, **fields):
    current = app.read(h.private, context(h))
    h.counter += 1
    request = dict(
        request_id=f"manage-{h.counter}",
        pair=current["pair"],
        expected_version=current["version"],
        operation=operation,
        **fields,
    )
    result = app.manage(
        request, authorization="Bearer test-only-companion-secret", assertion_ref="origin-private"
    )
    return request, result


def candidate(h, clock, number, *, group=False, kind="conversation_completed"):
    source = h.source(f"new-message-{number}")
    scope = h.group if group else h.private
    h.workflow.observe_source(source, scope, reality="real")
    event = h.event([source], scope, f"new-turn-{number}", f"commit-{number}")
    event["occurred_at"] = stamp(clock[0])
    h.service.consume(event, {"authenticated_service": "companion", "allowed_scopes": [scope]})
    value = dict(
        event_id=f"affinity-{number}",
        pair={k: scope[k] for k in ("actor_id", "person_id")},
        kind=kind,
        turn_id=event["aggregate_id"],
        source_ref=source_key(source),
        source_revision=1,
        occurred_at=event["occurred_at"],
    )
    return value, scope, context(h, group), event


def test_explicit_management_is_audited_idempotent_and_never_adds_permissions(relationship):
    h, app, _ = relationship
    initial_config = copy.deepcopy(h.config)
    request, result = command(h, app, "set_binding", relationship_type="partner")
    assert result["relationship_type"] == "partner" and result["score"] == 0
    assert (
        app.manage(
            request,
            authorization="Bearer test-only-companion-secret",
            assertion_ref="origin-private",
        )
        == result
    )
    with pytest.raises(Fault, match="idempotency_conflict"):
        app.manage(
            dict(request, relationship_type="friend"),
            authorization="Bearer test-only-companion-secret",
            assertion_ref="origin-private",
        )
    assert h.config == initial_config
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM relationship_commands").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE kind='management'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("mutation", ["principal", "role_admin", "operation", "revoked", "expired"])
def test_manager_requires_existing_real_origin_role_and_explicit_operation(relationship, mutation):
    h, app, _ = relationship
    request = dict(
        request_id="denied",
        pair=app.read(h.private, context(h))["pair"],
        expected_version=1,
        operation="set_freeze",
        frozen=True,
    )
    if mutation == "principal":
        h.config["origins"]["origin-private"]["principal_id"] = None
    if mutation == "role_admin":
        h.config["callers"]["companion"]["role_admin"] = False
    if mutation == "operation":
        h.config["callers"]["companion"]["operations"].remove("relationships.manage")
    if mutation == "revoked":
        h.config["origins"]["origin-private"]["revoked"] = True
    if mutation == "expired":
        h.config["origins"]["origin-private"]["expires_at"] = "2025-01-01T00:00:00Z"
    h.save_config()
    with pytest.raises(Fault):
        app.manage(
            request,
            authorization="Bearer test-only-companion-secret",
            assertion_ref="origin-private",
        )
    with h.store.transaction() as db:
        assert db.execute("SELECT frozen FROM relationship_pairs").fetchone()[0] == 0


def test_positive_daily_budget_source_dedup_and_negative_requires_attestation(relationship):
    h, app, clock = relationship
    for number in range(14):
        value, scope, ctx, _ = candidate(h, clock, number)
        result = app.settle(value, scope, ctx)
        assert result["applied_delta"] == (1 if number < 12 else 0)
    assert app.read(h.private, context(h))["score"] == 12
    assert app.settle(value, scope, ctx) == result
    alias = app.settle(dict(value, event_id="alias-same-source"), scope, ctx)
    assert alias["applied_delta"] == 0
    negative, scope, ctx, _ = candidate(h, clock, 20, kind="boundary_violation")
    with pytest.raises(Fault, match="unverified_behavior"):
        app.settle(negative, scope, ctx)
    app.behavior_verifier = lambda db, value, event: value["kind"] == "boundary_violation"
    assert app.settle(negative, scope, ctx)["applied_delta"] == -4
    assert app.read(h.private, context(h))["score"] == 8


def test_freeze_blocks_both_directions_and_decay_unfreeze_never_catches_up(relationship):
    h, app, clock = relationship
    command(h, app, "adjust_affinity", delta=100, reason="synthetic baseline")
    request, frozen = command(h, app, "set_freeze", frozen=True)
    value, scope, ctx, _ = candidate(h, clock, 1)
    assert app.settle(value, scope, ctx)["outcome"] == "rejected_frozen"
    app.behavior_verifier = lambda *args: True
    negative, scope, ctx, _ = candidate(h, clock, 2, kind="boundary_violation")
    assert app.settle(negative, scope, ctx)["applied_delta"] == 0
    clock[0] += timedelta(days=30)
    assert app.read(h.private, context(h))["score"] == frozen["score"] == 100
    assert (
        app.manage(
            request,
            authorization="Bearer test-only-companion-secret",
            assertion_ref="origin-private",
        )
        == frozen
    )
    _, unfrozen = command(h, app, "set_freeze", frozen=False)
    assert unfrozen["score"] == 100
    assert (
        app.settle(dict(value, event_id="new-id-old-frozen-source"), scope, ctx)["applied_delta"]
        == 0
    )
    clock[0] += timedelta(days=3)
    assert app.read(h.private, context(h))["score"] == 100
    clock[0] += timedelta(days=1)
    assert app.read(h.private, context(h))["score"] == 98
    _, noop = command(h, app, "set_freeze", frozen=False)
    clock[0] += timedelta(days=1)
    assert app.read(h.private, context(h))["score"] == 96
    assert noop["decay_cursor"] != stamp(clock[0])


def test_delayed_first_event_from_frozen_interval_never_replays(relationship):
    h, app, clock = relationship
    command(h, app, "set_freeze", frozen=True)
    clock[0] += timedelta(hours=1)
    value, scope, ctx, _ = candidate(h, clock, 1)
    clock[0] += timedelta(hours=1)
    command(h, app, "set_freeze", frozen=False)
    assert app.settle(value, scope, ctx)["outcome"] == "rejected_frozen"
    assert app.read(h.private, context(h))["score"] == 0


def test_actor_person_isolation_public_view_has_no_private_relationship(relationship):
    h, app, _ = relationship
    command(h, app, "set_binding", relationship_type="partner")
    command(h, app, "adjust_affinity", delta=100, reason="private")
    command(h, app, "set_freeze", frozen=True)
    public = app.read(h.group, context(h, True))
    assert set(public) == {
        "view",
        "pair",
        "version",
        "policy_version",
        "expression_hint",
        "checked_at",
    }
    assert "partner" not in canonical(public) and "score" not in public and "frozen" not in public
    other = dict(h.private, actor_id="actor-other")
    other_context = context(h)
    other_context["allowed_scope"] = other
    assert app.read(other, other_context)["score"] == 0
    assert app.read(other, other_context)["frozen"] is False
    with h.store.transaction() as db:
        db.execute("INSERT INTO people VALUES ('person-other')")
    with pytest.raises(Fault):
        app.read(dict(h.private, person_id="person-other"), context(h))


def test_group_default_never_grows(relationship):
    h, app, clock = relationship
    value, scope, ctx, _ = candidate(h, clock, 1, group=True)
    assert app.settle(value, scope, ctx)["applied_delta"] == 0
    assert app.read(h.private, context(h))["score"] == 0


def test_current_source_revocation_invalidates_frozen_projection_and_check(relationship):
    h, app, clock = relationship
    value, scope, ctx, _ = candidate(h, clock, 1)
    app.settle(value, scope, ctx)
    _, frozen = command(h, app, "set_freeze", frozen=True)
    with h.store.transaction() as db:
        db.execute("UPDATE sources SET state='withdrawn' WHERE key=?", (value["source_ref"],))
    current = app.read(h.private, context(h))
    assert current["score"] == 0 and current["frozen"] is True
    with pytest.raises(Fault, match="version_conflict"):
        app.check(h.private, context(h), frozen["version"])
    with pytest.raises(Fault):
        app.settle(value, scope, ctx)
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE kind='source_correction'"
            ).fetchone()[0]
            == 1
        )


def test_clock_backwards_negative_decay_and_subsecond_utc_order(relationship):
    h, app, clock = relationship
    command(h, app, "adjust_affinity", delta=-100, reason="negative baseline")
    clock[0] += timedelta(days=10000)
    assert app.read(h.private, context(h))["score"] == -100
    current = app.read(h.private, context(h))
    clock[0] -= timedelta(days=9999)
    again = app.read(h.private, context(h))
    assert again["score"] == -100 and again["version"] == current["version"]
    assert stamp(datetime(2026, 10, 1, tzinfo=UTC)) < stamp(
        datetime(2026, 10, 1, microsecond=1, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    "score,stage",
    [
        (-1200, "deeply_distant"),
        (-800, "strongly_distant"),
        (-400, "distant"),
        (0, "acquaintance"),
        (200, "familiar"),
        (600, "close"),
        (900, "intimate"),
        (1200, "deeply_intimate"),
    ],
)
def test_configurable_stages_and_reachable_top(score, stage):
    policy = Policy()
    assert policy.stage(score) == stage
    assert policy.stage(1200, "intimate") == "deeply_intimate"
    assert policy.decay(4, 0) == 2 and policy.decay(8, 7) == 5 and policy.decay(15, 14) == 8


def test_migration_retains_raw_values_and_is_idempotent(h):
    h.seed([h.draft(category="relationship", relationship_delta=37)])
    h.store.migrate_profiles(h.directory / "profiles.sqlite")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "sources.sqlite", h.contracts)
    backup = h.directory / "relations.sqlite"
    result = migrate(h.store, backup, clock=h.service.clock)
    assert result["imported"] == 1 and result["pending"] == 0
    repeat = migrate(h.store, backup, clock=h.service.clock)
    assert repeat["already_installed"] is True and repeat["imported"] == 1
    assert h.workflow.relationship_value(h.private) == 37
    with h.store.transaction() as db:
        assert db.execute("SELECT amount FROM relationship_entries").fetchone()[0] == 37
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE kind='legacy_import'"
            ).fetchone()[0]
            == 1
        )
        assert all(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (f"source_revision_{table}_INSERT",)
            ).fetchone()
            for table in TRACKED
        )
    with sqlite3.connect(backup) as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='relationships_schema'").fetchone()
            is None
        )
        assert db.execute("SELECT amount FROM relationship_entries").fetchone()[0] == 37
    assert backup.with_name(backup.name + ".source-guard.json").is_file()


def test_migration_ambiguous_scope_is_pending_without_clearing(h):
    seeded, _, _ = h.seed([h.draft(category="relationship", relationship_delta=37)])
    h.store.migrate_profiles(h.directory / "profiles.sqlite")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "sources.sqlite", h.contracts)
    with h.store.transaction() as db:
        db.execute(
            "UPDATE groups SET scope=? WHERE id=?",
            (canonical({"actor_id": "actor-fixture", "person_id": None}), seeded["group_ids"][0]),
        )
    result = migrate(h.store, h.directory / "relations.sqlite", clock=h.service.clock)
    assert result["pending"] == 1 and result["pending_items"][0]["amount"] == 37
    with h.store.transaction() as db:
        assert db.execute("SELECT amount FROM relationship_entries").fetchone()[0] == 37
        assert migration_report(db)["pending"] == 1


def test_backup_failure_never_installs_schema(h):
    h.store.migrate_profiles(h.directory / "profiles.sqlite")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "sources.sqlite", h.contracts)
    backup = h.directory / "occupied.sqlite"
    backup.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        migrate(h.store, backup, clock=h.service.clock)
    assert backup.read_bytes() == b"keep"
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='relationships_schema'").fetchone()
            is None
        )
