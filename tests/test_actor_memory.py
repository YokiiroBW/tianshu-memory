"""Real isolated actor transactions; source/role owners are explicit test peers."""

import asyncio
import copy
import json
import os
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import contract_directory
from fastapi.testclient import TestClient

from tianshu_memory.actor_memory.application import INTERNAL, Application
from tianshu_memory.actor_memory.authority import Authority
from tianshu_memory.actor_memory.contracts import Contracts
from tianshu_memory.actor_memory.store import Store
from tianshu_memory.app import create_app
from tianshu_memory.auth import Authenticator
from tianshu_memory.domain import Fault, canonical, require

OPERATIONS = [
    "append_episode",
    "get_episode",
    "check_episode",
    "get_receipt",
    "withdraw_episode",
    "apply_source_revision",
    "append_derived",
    "query",
    "check_memory",
    "withdraw_memory",
]


class Owner:
    """A controllable source owner, never a production authority default."""

    def __init__(self):
        self.sequence = 1
        self.role_version = 1
        self.allowed = True
        self.context_allowed = True
        self.states = {}
        self.calls = []
        self.hook = None
        self.fact = dict(
            source=dict(
                producer="companion", object_id="reading:one", revision="1", content_hash="a" * 64
            ),
            actor_id="actor:a",
            relation="observer",
            provenance="digital_action",
            occurred_at="2026-10-26T00:00:00Z",
            learned_at="2026-10-26T00:00:00Z",
            outcome="completed",
            summary="I read the first two pages, with a gap on page three.",
            source_generation="generation:one",
            source_sequence=1,
            access_version="access:one",
            source_constraints=dict(kind="actor_only"),
            content_reads=[],
        )

    def actor(self, request):
        require(self.allowed and request["role_version"] == self.role_version, "forbidden", 403)
        require(request["operation"] in OPERATIONS, "forbidden", 403)
        return dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            role_version=self.role_version,
            state="enabled",
            allowed_operations=OPERATIONS,
            policy_version="policy:one",
        )

    def context(self, request, context):
        if context["audience"]["kind"] == "actor_only":
            return None
        require(self.context_allowed, "forbidden", 403)
        return dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            role_version=self.role_version,
            context=context,
            allowed=True,
            access_version="context:one",
        )

    def sources(self, request, refs, *, facts=False, context=None):
        self.calls.append(
            ("facts" if facts else "check", copy.deepcopy(refs), copy.deepcopy(context))
        )
        result = {}
        for source in refs:
            if facts:
                fact = copy.deepcopy(self.fact)
                fact.update(actor_id=request["actor_id"], source_sequence=self.sequence)
                result[canonical(source)] = fact
            else:
                state = self.states.get(canonical(source), "current")
                result[canonical(source)] = dict(
                    source=copy.deepcopy(source),
                    state=state,
                    actor_known=True,
                    read_allowed=state == "current",
                    disclosure_allowed=state == "current",
                    source_generation="generation:one",
                    source_sequence=self.sequence,
                    access_version="access:one",
                )
        if self.hook is not None:
            self.hook(facts)
        return result

    def close(self):
        pass


@pytest.fixture
def actor_storage():
    """The native PG bridge may supply its already-owned ordinary storage settings."""
    return {}


@pytest.fixture
def actor(tmp_path, monkeypatch, actor_storage):
    for name in ("TIANSHU_POSTGRES_URL", "TIANSHU_REDIS_URL", "TIANSHU_STORAGE_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)
    directory = Path(
        os.environ.get(
            "TIANSHU_ACTOR_CONTRACT_DIRECTORY",
            contract_directory().parents[1] / "actor-self-memory/candidate-v1",
        )
    )
    contracts = Contracts(directory)
    user_path = tmp_path / "user.sqlite"
    user_path.write_bytes(b"unchanged independent user authority")
    config = dict(
        database_path=str(user_path),
        storage_backends=actor_storage,
        actor_memory=dict(
            database_path=str(tmp_path / "actor.sqlite"), contract_directory=str(directory)
        ),
    )
    Store.initialize(config, tmp_path / "initial-backup.json")
    store = Store(config)
    owner = Owner()
    application = Application(
        store, contracts, owner, clock=lambda: datetime(2026, 10, 26, tzinfo=UTC)
    )
    yield dict(
        app=application, store=store, owner=owner, config=config, contracts=contracts, tmp=tmp_path
    )
    application.close()
    assert user_path.read_bytes() == b"unchanged independent user authority"


def request(operation="append_episode", **values):
    payload = dict(
        schema_version=1,
        request_id="req:one",
        actor_id="actor:a",
        role_version=1,
        operation=operation,
    )
    payload.update(values)
    return payload


def append(actor, **values):
    return request(
        idempotency_key="append:one",
        source=copy.deepcopy(actor["owner"].fact["source"]),
        perspective="observer",
        **values,
    )


def execute(actor, payload, *, caller="companion"):
    return actor["app"].execute(payload, caller=caller, recheck=lambda: caller)


def read(receipt, **values):
    return request(
        "get_episode",
        episode_id=receipt["episode_id"],
        expected_revision=receipt["revision"],
        context=copy.deepcopy(INTERNAL),
        **values,
    )


def test_episode_receipt_is_idempotent_and_original_receipt_is_immutable(actor):
    first_request = append(actor)
    first = execute(actor, first_request)
    replay = execute(actor, {**first_request, "request_id": "req:retry"})
    assert replay == {**first, "request_id": "req:retry"}
    original = execute(
        actor,
        request(
            "get_receipt",
            command="append_episode",
            idempotency_key=first_request["idempotency_key"],
        ),
    )
    assert original == first
    duplicate = execute(actor, {**first_request, "idempotency_key": "different-key"})
    assert duplicate["state"] == "duplicate" and duplicate["episode_id"] == first["episode_id"]
    assert execute(actor, read(first))["episode"]["fact"] == actor["owner"].fact
    with actor["store"].transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM actor_episodes").fetchone()[0] == 1


def test_same_key_different_source_conflicts_and_other_caller_cannot_read_receipt(actor):
    payload = append(actor)
    execute(actor, payload)
    changed = {**payload, "source": {**payload["source"], "revision": "2"}}
    with pytest.raises(Fault, match="idempotency_conflict"):
        execute(actor, changed)
    with pytest.raises(Fault, match="not_found"):
        execute(
            actor,
            request("get_receipt", command="append_episode", idempotency_key="append:one"),
            caller="other-service",
        )


def test_different_actors_do_not_share_episodes_or_receipts(actor):
    first = execute(actor, append(actor))
    second = execute(actor, {**append(actor), "actor_id": "actor:b"})
    assert first["episode_id"] != second["episode_id"]
    with pytest.raises(Fault, match="not_found"):
        execute(actor, {**read(first), "actor_id": "actor:b"})


@pytest.mark.parametrize("reason", ["forgotten", "incorrect", "retired"])
def test_withdraw_is_durable_and_late_append_cannot_resurrect(actor, reason):
    original = execute(actor, append(actor))
    result = execute(
        actor,
        request(
            "withdraw_episode",
            idempotency_key="forget:one",
            episode_id=original["episode_id"],
            expected_revision=1,
            reason=reason,
        ),
    )
    assert result["state"] == "withdrawn" and result["revision"] == 2
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, {**append(actor), "idempotency_key": "late:one"})
    # A completed receipt stays completed even after later forgetting.
    assert execute(actor, append(actor)) == original
    with pytest.raises(Fault):
        execute(actor, read(original))


@pytest.mark.parametrize("negative", ["withdrawn", "superseded", "missing"])
def test_source_negative_is_committed_and_next_checkpoint_cannot_use_old_positive(actor, negative):
    original = execute(actor, append(actor))
    actor["owner"].states[canonical(actor["owner"].fact["source"])] = negative
    check = request(
        "check_episode",
        episodes=[dict(episode_id=original["episode_id"], expected_revision=1)],
        context=copy.deepcopy(INTERNAL),
    )
    result = execute(actor, check)
    assert result["items"][0]["state"] == "withdrawn"
    assert not result["items"][0]["read_allowed"]
    actor["owner"].states.clear()
    assert execute(actor, check)["items"][0]["state"] == "withdrawn"


def test_transient_source_unavailability_does_not_become_durable_withdrawal(actor):
    original = execute(actor, append(actor))
    actor["owner"].states[canonical(actor["owner"].fact["source"])] = "unavailable"
    with pytest.raises(Fault, match="dependency_unavailable"):
        execute(actor, read(original))
    actor["owner"].states.clear()
    assert execute(actor, read(original))["episode"]["state"] == "active"


def test_unrelated_owner_head_advance_preserves_current_same_exact_source(actor):
    original = execute(actor, append(actor))
    actor["owner"].sequence += 1
    assert execute(actor, read(original))["episode"]["state"] == "active"
    actor["owner"].sequence -= 1
    with pytest.raises(Fault, match="dependency_unavailable"):
        execute(actor, read(original))


def test_current_read_does_not_advance_actor_domain_or_change_original_fact(actor):
    original = execute(actor, append(actor))
    with actor["store"].transaction() as db:
        before = actor["store"].version(db)
    result = execute(actor, read(original))
    assert result["domain_version"] == before
    with actor["store"].transaction() as db:
        assert actor["store"].version(db) == before


def test_revision_notification_does_not_invalidate_new_current_episode(actor):
    old_source = copy.deepcopy(actor["owner"].fact["source"])
    old = execute(actor, append(actor))
    actor["owner"].sequence = 2
    actor["owner"].fact["source"].update(revision="2", content_hash="c" * 64)
    new_request = {**append(actor), "idempotency_key": "append:new"}
    new = execute(actor, new_request)
    actor["owner"].states[canonical(old_source)] = "superseded"
    receipt = execute(
        actor, request("apply_source_revision", source=old_source, idempotency_key="source:old")
    )
    assert receipt["state"] == "invalidated"
    assert execute(actor, read(new))["episode"]["fact"]["source"]["revision"] == "2"
    with pytest.raises(Fault):
        execute(actor, read(old))


def test_withdraw_during_remote_proof_rejects_body_then_persists_stable_negative(actor):
    original = execute(actor, append(actor))

    def changed(facts):
        if not facts:
            actor["owner"].hook = None
            actor["owner"].states[canonical(actor["owner"].fact["source"])] = "withdrawn"

    actor["owner"].hook = changed
    with pytest.raises(Fault, match="source_changed"):
        execute(actor, read(original))
    result = execute(
        actor,
        request(
            "check_episode",
            episodes=[dict(episode_id=original["episode_id"], expected_revision=1)],
            context=copy.deepcopy(INTERNAL),
        ),
    )
    assert result["items"][0]["state"] == "withdrawn"


def test_content_dependency_is_checked_and_withdrawal_invalidates_reading(actor):
    content = dict(
        producer="knowledge", object_id="content:one", revision="1", content_hash="b" * 64
    )
    actor["owner"].fact["content_reads"] = [
        dict(
            content_ref=content,
            coverage=[dict(unit="pages", start=1, end=2, complete=False, gaps=[])],
        )
    ]
    original = execute(actor, append(actor))
    assert any(content in refs for kind, refs, _ in actor["owner"].calls if kind == "check")
    actor["owner"].states[canonical(content)] = "withdrawn"
    with pytest.raises(Fault):
        execute(actor, read(original))
    with actor["store"].transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM actor_episodes WHERE id=?", (original["episode_id"],)
            ).fetchone()[0]
            == "unavailable"
        )


@pytest.mark.parametrize(
    "restriction,audience",
    [
        (
            dict(kind="actor_only"),
            dict(kind="group", conversation_id="conv:a", assertion_ref="origin:one"),
        ),
        (
            dict(kind="private", person_id="person:a", conversation_id="conv:private"),
            dict(kind="group", conversation_id="conv:a", assertion_ref="origin:one"),
        ),
    ],
)
def test_source_constraints_never_expand_when_disclosure_owner_claims_yes(
    actor, restriction, audience
):
    actor["owner"].fact["source_constraints"] = restriction
    original = execute(actor, append(actor))
    with pytest.raises(Fault, match="forbidden"):
        execute(actor, {**read(original), "context": dict(purpose="dialogue", audience=audience)})
    assert execute(actor, read(original))["episode"]["state"] == "active"


def test_current_context_revocation_and_role_change_are_rechecked(actor):
    actor["owner"].fact["source_constraints"] = dict(kind="public")
    original = execute(actor, append(actor))
    context = dict(
        purpose="dialogue",
        audience=dict(kind="group", conversation_id="conv:a", assertion_ref="origin:one"),
    )
    actor["owner"].context_allowed = False
    with pytest.raises(Fault, match="forbidden"):
        execute(actor, {**read(original), "context": context})
    actor["owner"].context_allowed = True
    actor["owner"].role_version = 2
    with pytest.raises(Fault, match="forbidden"):
        execute(actor, read(original))


def test_source_change_between_proofs_never_commits_episode(actor):
    def changed(facts):
        if not facts:
            actor["owner"].hook = None
            actor["owner"].sequence += 1

    actor["owner"].hook = changed
    with pytest.raises(Fault, match="source_changed"):
        execute(actor, append(actor))
    with actor["store"].transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM actor_episodes").fetchone()[0] == 0


def test_changed_local_domain_rejects_proof_instead_of_rebasing(actor):
    with closing(Store(actor["config"])) as concurrent:

        def changed(facts):
            if not facts:
                actor["owner"].hook = None
                with concurrent.transaction(write=True):
                    pass

        actor["owner"].hook = changed
        with pytest.raises(Fault, match="source_changed"):
            execute(actor, append(actor))
    with actor["store"].transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM actor_episodes").fetchone()[0] == 0


def test_restored_old_actor_database_cannot_bypass_new_guard(actor):
    with actor["store"].transaction() as db:
        before = list(db.execute("SELECT key,value FROM actor_metadata"))
    execute(actor, append(actor))
    with closing(actor["store"].database.connect(isolation_level=None)) as db:
        db.execute(
            "UPDATE actor_metadata SET value=? WHERE key='revision'", (dict(before)["revision"],)
        )
    with pytest.raises(Fault, match="dependency_unavailable"):
        with actor["store"].transaction():
            pass


def test_actor_initialization_is_explicit_and_user_service_is_not_constructed(
    tmp_path, monkeypatch
):
    for name in ("TIANSHU_POSTGRES_URL", "TIANSHU_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    config = dict(
        database_path=str(tmp_path / "user.sqlite"),
        actor_memory=dict(database_path=str(tmp_path / "actor.sqlite")),
    )
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(config)
    assert not Path(config["database_path"]).exists()
    assert not Path(config["actor_memory"]["database_path"]).exists()
    Store.initialize(config, tmp_path / "before.json")
    assert Store.initialize(config, tmp_path / "unused-backup.json")["already_installed"]
    assert not (tmp_path / "unused-backup.json").exists()


def test_uninitialized_actor_failure_keeps_existing_user_identity_working(h, tmp_path):
    h.config["callers"]["companion"]["actor_memory_operations"] = OPERATIONS
    h.config["callers"]["companion"]["allowed_actors"].append("actor:a")
    directory = Path(
        os.environ.get(
            "TIANSHU_ACTOR_CONTRACT_DIRECTORY",
            contract_directory().parents[1] / "actor-self-memory/candidate-v1",
        )
    )
    h.config["actor_memory"] = dict(
        database_path=str(tmp_path / "not-initialized.sqlite"), contract_directory=str(directory)
    )
    h.save_config()
    payload = request("get_receipt", command="append_episode", idempotency_key="unknown")
    result = h.post("actor-self-memory/operate", payload)
    assert result.status_code == 503, result.text
    existing = h.post("identity/resolve", {"query": h.query(), "account": h.account})
    assert existing.status_code == 200 and existing.json()["person_id"] == h.person


def test_active_actor_store_cannot_silently_follow_storage_registration_change(actor, contracts):
    path = actor["tmp"] / "registration.json"
    config = {**actor["config"], "callers": {}}
    path.write_text(canonical(config), encoding="utf-8")
    auth = Authenticator(path, contracts, lambda: datetime.now(UTC))
    app = create_app(auth=auth)
    with TestClient(app):
        endpoint = app.state.actor_memory
        original = endpoint.get()
        config["storage_backends"] = dict(namespace="different-actor-owner")
        path.write_text(canonical(config), encoding="utf-8")
        with pytest.raises(Fault, match="source_changed"):
            endpoint.get()
        assert endpoint.application is original


def http_app(actor, contracts, *, execute_timeout=15):
    config = {
        **actor["config"],
        "callers": {
            "companion": dict(
                token="test-actor-token",
                allowed_actors=["actor:a"],
                actor_memory_operations=OPERATIONS,
            )
        },
    }
    path = actor["tmp"] / "config.json"
    path.write_text(canonical(config), encoding="utf-8")
    auth = Authenticator(path, contracts, lambda: datetime.now(UTC))
    app = create_app(auth=auth, execute_timeout=execute_timeout)
    endpoint = app.state.actor_memory
    endpoint.get = lambda: actor["app"]
    return app, endpoint


def test_http_transport_is_separate_from_user_admission_and_denies_wrong_actor(actor, contracts):
    app, endpoint = http_app(actor, contracts)
    with TestClient(app) as client:
        held = [
            app.state.chat_requests.claim() for _ in range(app.state.chat_requests.ordinary_limit)
        ]
        try:
            response = client.post(
                "/internal/v1/actor-self-memory/operate",
                json=append(actor),
                headers={"Authorization": "Bearer test-actor-token"},
            )
            assert response.status_code == 200, response.text
            response = client.post(
                "/internal/v1/actor-self-memory/operate",
                json={**append(actor), "actor_id": "actor:b"},
                headers={"Authorization": "Bearer test-actor-token"},
            )
            assert response.status_code == 403
            assert endpoint.admission.active == 0
        finally:
            for lease in held:
                lease.finish()


def test_timeout_keeps_actual_worker_and_original_receipt_recovery(actor, contracts):
    started, release = threading.Event(), threading.Event()

    def block(facts):
        if facts:
            started.set()
            assert release.wait(3)

    actor["owner"].hook = block
    app, endpoint = http_app(actor, contracts, execute_timeout=0.02)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver"
            ) as client:
                result = await client.post(
                    "/internal/v1/actor-self-memory/operate",
                    json=append(actor),
                    headers={"Authorization": "Bearer test-actor-token"},
                )
                assert result.status_code == 503 and result.json()["execution"] == "unknown"
                assert started.is_set() and endpoint.admission.active == 1
                release.set()
                for _ in range(100):
                    if endpoint.admission.active == 0:
                        break
                    await asyncio.sleep(0.01)
                assert endpoint.admission.active == 0
                actor["owner"].hook = None
                receipt = await client.post(
                    "/internal/v1/actor-self-memory/operate",
                    json=request(
                        "get_receipt", command="append_episode", idempotency_key="append:one"
                    ),
                    headers={"Authorization": "Bearer test-actor-token"},
                )
                assert receipt.status_code == 200, receipt.text
                assert receipt.json()["state"] == "accepted"

    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize("malformation", ["missing", "duplicate", "wrong-actor", "invalid-date"])
def test_real_authority_rejects_unbound_owner_response(actor, monkeypatch, malformation):
    config = {
        **actor["config"],
        "actor_memory": {
            **actor["config"]["actor_memory"],
            "authorities": {
                "actor_check": dict(url="https://platform/authority", token="owner-token"),
                "producers": {
                    "companion": dict(
                        source_facts_url="https://companion/source",
                        source_check_url="https://companion/source",
                        token="owner-token",
                    )
                },
            },
        },
    }
    path = actor["tmp"] / "authority.json"
    path.write_text(canonical(config), encoding="utf-8")
    authority = Authority(path, actor["contracts"])

    def respond(req):
        body = json.loads(req.content)
        fact = copy.deepcopy(actor["owner"].fact)
        facts = [fact]
        if malformation == "missing":
            facts = []
        elif malformation == "duplicate":
            facts *= 2
        elif malformation == "wrong-actor":
            fact["actor_id"] = "actor:b"
        else:
            fact["learned_at"] = "2026-02-30T00:00:00Z"
        return httpx.Response(
            200,
            json=dict(
                schema_version=1,
                request_id=body["request_id"],
                actor_id=body["actor_id"],
                facts=facts,
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    from contextlib import contextmanager

    @contextmanager
    def connection(*args, target):
        assert target == "companion"
        yield client

    monkeypatch.setattr(authority.pool, "client", connection)
    try:
        with pytest.raises(Fault, match="dependency_unavailable"):
            authority.sources(append(actor), [actor["owner"].fact["source"]], facts=True)
    finally:
        client.close()
        authority.close()
