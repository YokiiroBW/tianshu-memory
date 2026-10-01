"""Race, persistence, provenance and HTTP authority regressions for TS-114."""

import asyncio
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from test_relationships import candidate, command, context
from test_relationships import relationship as relationship

from tianshu_memory.app import create_app
from tianshu_memory.domain import Fault
from tianshu_memory.relationship_migration import migrate
from tianshu_memory.relationships import Policy, Relationships
from tianshu_memory.relationships.timestamps import stamp
from tianshu_memory.store import Store

TOKEN = "Bearer test-only-companion-secret"


@pytest.fixture
def http_relationship(relationship):
    h, application, clock = relationship
    h.config["callers"]["companion"]["operations"].extend(
        ["relationships.read", "relationships.check", "relationships.settle"]
    )
    h.save_config()
    with TestClient(
        create_app(service=h.service, auth=h.auth, relationships=application)
    ) as client:
        yield h, application, clock, client


def request(h, **extra):
    return dict(h.query(), **extra)


def post(client, operation, body, **headers):
    return client.post(
        "/internal/v1/relationships/" + operation,
        json=body,
        headers={"Authorization": TOKEN, **headers},
    )


def test_http_four_ports_and_candidate_schema(http_relationship):
    h, app, clock, client = http_relationship
    pair = {k: h.private[k] for k in ("actor_id", "person_id")}
    response = post(client, "read", request(h, pair=pair))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    initial = response.json()["projection"]
    body = request(h)
    body["command"] = dict(
        request_id=body["request_id"],
        pair=pair,
        expected_version=initial["version"],
        operation="set_binding",
        relationship_type="partner",
    )
    managed = post(client, "manage", body)
    assert managed.status_code == 200
    assert post(client, "manage", body).json() == managed.json()
    proof, _, _, _ = candidate(h, clock, 1)
    settled = post(client, "settle", request(h, candidate=proof))
    assert settled.status_code == 200 and settled.json()["settlement"]["applied_delta"] == 1
    current = post(client, "read", request(h, pair=pair)).json()["projection"]
    assert (
        post(
            client, "check", request(h, pair=pair, expected_version=current["version"])
        ).status_code
        == 200
    )
    assert (
        post(
            client, "check", request(h, pair=pair, expected_version=initial["version"])
        ).status_code
        == 409
    )
    # Validate actual application output against the published coordinator contract, offline.
    from jsonschema import Draft202012Validator

    directory = h.contracts.directory.parents[1] / "role-relationship/v1"
    schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for value in (initial, managed.json()["projection"], current, settled.json()["settlement"]):
        validator.validate(value)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_token",
        "operation",
        "role",
        "principal",
        "expired",
        "revoked",
        "actor",
        "person",
        "origin",
        "cookie",
        "operator",
    ],
)
def test_http_rejects_untrusted_or_revoked_authority(http_relationship, mutation):
    h, _, _, client = http_relationship
    pair = {k: h.private[k] for k in ("actor_id", "person_id")}
    body = request(h, pair=pair)
    headers = {}
    if mutation == "missing_token":
        headers["Authorization"] = "Bearer unrelated-test-token"
    if mutation == "operation":
        h.config["callers"]["companion"]["operations"].remove("relationships.read")
    if mutation in {"role", "principal"}:
        body["managed"] = True
        if mutation == "role":
            h.config["callers"]["companion"]["role_admin"] = False
        else:
            h.config["origins"]["origin-private"]["principal_id"] = None
    if mutation == "expired":
        h.config["origins"]["origin-private"]["expires_at"] = "2025-01-01T00:00:00Z"
    if mutation == "revoked":
        h.config["origins"]["origin-private"]["revoked"] = True
    if mutation == "actor":
        pair["actor_id"] = "actor-other"
    if mutation == "person":
        pair["person_id"] = "person-other"
    if mutation == "origin":
        headers["Origin"] = "https://untrusted.example"
    if mutation == "cookie":
        headers["Cookie"] = "session=synthetic"
    if mutation == "operator":
        body["operator"] = "synthetic-admin"
    h.save_config()
    response = post(client, "read", body, **headers)
    assert response.status_code in {400, 401, 403}
    assert "projection" not in response.json()


def test_http_group_and_managed_other_person_are_distinct(http_relationship):
    h, _, _, client = http_relationship
    pair = {k: h.private[k] for k in ("actor_id", "person_id")}
    private = post(client, "read", request(h, pair=pair)).json()["projection"]
    body = request(h, pair=pair)
    body["origin"]["assertion_ref"] = "origin-group"
    public = post(client, "read", body).json()["projection"]
    assert public["view"] == "public" and "score" not in public and "frozen" not in public
    with h.store.transaction() as db:
        db.execute("INSERT INTO people VALUES ('person-other')")
    other = dict(pair, person_id="person-other")
    assert post(client, "read", request(h, pair=other)).status_code == 403
    result = post(client, "read", request(h, pair=other, managed=True))
    assert result.status_code == 200 and result.json()["projection"]["score"] == 0
    body = request(h)
    body["command"] = dict(
        request_id=body["request_id"],
        pair=other,
        expected_version=1,
        operation="adjust_affinity",
        delta=100,
        reason="explicit synthetic admin action",
    )
    assert post(client, "manage", body).json()["projection"]["score"] == 100
    assert (
        post(client, "read", request(h, pair=pair)).json()["projection"]["score"]
        == private["score"]
    )


@pytest.mark.parametrize(
    "bad", [None, True, 12, {}, "not-a-time", "2026-10-01T00:00:00", "2026-10-02T00:00:00Z"]
)
def test_http_settlement_timestamp_is_persisted_turn_evidence(http_relationship, bad):
    h, _, clock, client = http_relationship
    proof, _, _, _ = candidate(h, clock, 1)
    proof["occurred_at"] = bad
    assert post(client, "settle", request(h, candidate=proof)).status_code in {400, 409}
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE kind='automatic'"
            ).fetchone()[0]
            == 0
        )


def test_http_replay_still_requires_current_authority(http_relationship):
    h, _, clock, client = http_relationship
    proof, _, _, _ = candidate(h, clock, 1)
    body = request(h, candidate=proof)
    assert post(client, "settle", body).status_code == 200
    h.config["origins"]["origin-private"]["revoked"] = True
    h.save_config()
    assert post(client, "settle", body).status_code == 403


def test_parallel_same_event_commits_once(relationship):
    h, app, clock = relationship
    proof, scope, ctx, _ = candidate(h, clock, 1)
    barrier = Barrier(8)

    def run():
        barrier.wait()
        return app.settle(proof, scope, ctx)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: run(), range(8)))
    assert all(r == results[0] for r in results)
    assert app.read(h.private, context(h))["score"] == 1
    with h.store.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE kind='automatic'"
            ).fetchone()[0]
            == 1
        )


def test_freeze_and_automatic_event_linearize(relationship):
    h, app, clock = relationship
    initial = app.read(h.private, context(h))
    proof, scope, ctx, _ = candidate(h, clock, 1)
    freeze = dict(
        request_id="race-freeze",
        pair=initial["pair"],
        expected_version=initial["version"],
        operation="set_freeze",
        frozen=True,
    )
    barrier = Barrier(2)

    def freezing():
        barrier.wait()
        try:
            return app.manage(freeze, authorization=TOKEN, assertion_ref="origin-private")
        except Fault as error:
            assert error.code == "version_conflict"
            freeze["expected_version"] = app.read(scope, ctx)["version"]
            return app.manage(freeze, authorization=TOKEN, assertion_ref="origin-private")

    def settling():
        barrier.wait()
        return app.settle(proof, scope, ctx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(freezing), pool.submit(settling)
        frozen, result = left.result(), right.result()
    current = app.read(scope, ctx)
    assert (
        current["frozen"] is True and current["score"] == result["applied_delta"] == frozen["score"]
    )
    assert result["applied_delta"] in {0, 1}


def test_freeze_restart_watermark_backwards_clock_and_no_catchup(relationship):
    h, app, clock = relationship
    command(h, app, "adjust_affinity", delta=100, reason="baseline")
    command(h, app, "set_freeze", frozen=True)
    clock[0] += timedelta(days=200)
    assert app.read(h.private, context(h))["score"] == 100
    head = clock[0]
    h.service.store = Store(h.store.path, recovery_path=h.store.recovery_path)
    app = Relationships(h.service, auth=h.auth)
    clock[0] -= timedelta(days=199)
    _, result = command(h, app, "set_freeze", frozen=False)
    assert result["decay_cursor"] == stamp(head) and result["score"] == 100
    clock[0] = head + timedelta(days=4)
    assert app.read(h.private, context(h))["score"] == 98


@pytest.mark.parametrize("delta", [37, -37])
def test_old_workflow_delayed_frozen_event_does_not_score_after_unfreeze(relationship, delta):
    h, app, clock = relationship
    command(h, app, "set_freeze", frozen=True)
    clock[0] += timedelta(hours=1)
    _, _, _, event = candidate(h, clock, 1)
    with h.store.transaction() as db:
        job = db.execute(
            "SELECT id FROM jobs WHERE json_extract(event,'$.event_id')=?", (event["event_id"],)
        ).fetchone()[0]
    clock[0] += timedelta(hours=1)
    command(h, app, "set_freeze", frozen=False)
    draft = h.draft(
        units=[h.unit(event["sources"][0])], category="relationship", relationship_delta=delta
    )
    result = h.workflow.commit_candidate(job, [draft])
    assert result["state"] == "committed"
    assert app.read(h.private, context(h))["score"] == 0
    assert h.workflow.commit_candidate(job, [draft]) == result
    with h.store.transaction() as db:
        assert db.execute("SELECT amount FROM relationship_entries").fetchone()[0] == delta
        assert db.execute(
            "SELECT outcome,delta FROM relationship_events WHERE kind='automatic'"
        ).fetchone()[:] == ("rejected_frozen", 0)


def test_old_and_new_paths_share_one_source_budget_and_forget_invalidates(relationship):
    h, app, clock = relationship
    proof, scope, ctx, event = candidate(h, clock, 1)
    assert app.settle(proof, scope, ctx)["applied_delta"] == 1
    with h.store.transaction() as db:
        job = db.execute(
            "SELECT id FROM jobs WHERE json_extract(event,'$.event_id')=?", (event["event_id"],)
        ).fetchone()[0]
    seeded = h.workflow.commit_candidate(
        job,
        [
            h.draft(
                units=[h.unit(event["sources"][0])], category="relationship", relationship_delta=37
            )
        ],
    )
    assert app.read(scope, ctx)["score"] == h.workflow.relationship_value(scope) == 1
    command(h, app, "set_freeze", frozen=True)
    request = h.revision(seeded["record_ids"][0], kind="forget")
    request["evidence_refs"] = event["sources"]
    request["confirmation_ref"] = "confirmation-affinity-forget"
    h.workflow.confirm_revision(request, h.account, scope, "2027-01-01T00:00:00Z")
    h.service.revise(request, ctx)
    assert app.read(scope, ctx)["score"] == 0
    with pytest.raises(Fault):
        app.settle(proof, scope, ctx)


def test_invalidated_decayed_positive_does_not_turn_negative(relationship):
    h, app, clock = relationship
    proof, scope, ctx, _ = candidate(h, clock, 1)
    app.settle(proof, scope, ctx)
    clock[0] += timedelta(days=4)
    assert app.read(scope, ctx)["score"] == 0
    with h.store.transaction() as db:
        db.execute("UPDATE sources SET state='withdrawn' WHERE key=?", (proof["source_ref"],))
    assert app.read(scope, ctx)["score"] == 0


def test_top_stage_reachable_through_audited_commands(relationship):
    h, app, _ = relationship
    for _ in range(12):
        command(h, app, "adjust_affinity", delta=100, reason="stage boundary")
    current = app.read(h.private, context(h))
    assert current["score"] == 1200 and current["stage"] == "deeply_intimate"
    assert current["relationship_type"] == "unspecified"


def test_migration_transaction_failure_and_complete_backup_restore(h, monkeypatch):
    h.seed([h.draft(category="relationship", relationship_delta=37)])
    h.store.migrate_profiles(h.directory / "profiles.sqlite")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "sources.sqlite", h.contracts)
    guard = h.store.recovery_path.read_bytes()
    import tianshu_memory.relationship_migration as module

    real = module.import_legacy

    def interrupted(*args):
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(module, "import_legacy", interrupted)
    backup = h.directory / "before-relationship.sqlite"
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        migrate(h.store, backup, clock=h.service.clock)
    assert h.store.recovery_path.read_bytes() == guard
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='relationships_schema'").fetchone()
            is None
        )
    monkeypatch.setattr(module, "import_legacy", real)
    migrate(h.store, h.directory / "retry.sqlite", clock=h.service.clock)
    shutil.copyfile(backup, h.store.path)
    shutil.copyfile(backup.with_name(backup.name + ".source-guard.json"), h.store.recovery_path)
    with h.store.transaction() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='relationships_schema'").fetchone()
            is None
        )
        assert db.execute("SELECT amount FROM relationship_entries").fetchone()[0] == 37
    assert (
        migrate(h.store, h.directory / "after-restore.sqlite", clock=h.service.clock)["imported"]
        == 1
    )


@pytest.mark.parametrize(
    "raw,media,status",
    [
        (b"{", "application/json", 400),
        (b"\xff", "application/json", 400),
        (b'{"schema_version":1,"schema_version":1}', "application/json", 400),
        (b"NaN", "application/json", 400),
        (b"x" * 16385, "application/json", 413),
        (b"{}", "text/plain", 415),
    ],
)
def test_http_bounded_strict_body(http_relationship, raw, media, status):
    _, _, _, client = http_relationship
    result = client.post(
        "/internal/v1/relationships/read",
        content=raw,
        headers={"Authorization": TOKEN, "Content-Type": media},
    )
    assert result.status_code == status
    assert "projection" not in result.json()


def test_http_auth_denial_precedes_first_body_read(http_relationship):
    _, _, _, client = http_relationship
    received, responses = [], []

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"{", "more_body": False}

    async def send(message):
        responses.append(message)

    scope = dict(
        type="http",
        asgi={"version": "3.0"},
        http_version="1.1",
        method="POST",
        scheme="http",
        path="/internal/v1/relationships/read",
        raw_path=b"/internal/v1/relationships/read",
        query_string=b"",
        root_path="",
        headers=[
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer synthetic-invalid-credential"),
        ],
        client=("127.0.0.1", 12345),
        server=("testserver", 80),
        state={},
    )
    asyncio.run(client.app(scope, receive, send))
    assert received == []
    assert responses[0]["status"] == 401


def test_legacy_first_read_uses_assembled_policy(h):
    h.store.migrate_profiles(h.directory / "custom-profiles.sqlite")
    h.contracts.load_sources()
    h.store.migrate_sources(h.directory / "custom-sources.sqlite", h.contracts)
    policy = Policy(daily_positive_limit=3, positive_limit=2)
    migrate(
        h.store, h.directory / "custom-relationships.sqlite", clock=h.service.clock, policy=policy
    )
    application = Relationships(h.service, auth=h.auth, policy=policy)
    with TestClient(create_app(service=h.service, auth=h.auth, relationships=application)):
        assert h.workflow.relationship_value(h.private) == 0
        value = application.read(h.private, context(h))
        assert value["policy_version"] == policy.version


@pytest.mark.parametrize("invalid", ["has space", "_leading", "中文标识", "with/slash"])
def test_relationship_ids_match_candidate_contract(http_relationship, invalid):
    h, application, clock, client = http_relationship
    proof, scope, ctx, _ = candidate(h, clock, 1)
    with pytest.raises(Fault) as error:
        application.settle(dict(proof, event_id=invalid), scope, ctx)
    assert error.value.status == 400
    current = application.read(scope, ctx)
    action = dict(
        request_id=invalid,
        pair=current["pair"],
        expected_version=current["version"],
        operation="set_freeze",
        frozen=True,
    )
    with pytest.raises(Fault) as error:
        application.manage(action, authorization=TOKEN, assertion_ref="origin-private")
    assert error.value.status == 400
    payload = request(h, pair=current["pair"])
    payload["request_id"] = invalid
    assert post(client, "read", payload).status_code == 400
    assert application.read(scope, ctx)["score"] == 0
    assert application.read(scope, ctx)["frozen"] is False
