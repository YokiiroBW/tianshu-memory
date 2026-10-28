"""Real isolated user/actor stores; original Platform/Core owners are test peers."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from source_sync_harness import SyncHarness
from test_actor_memories import check, derive
from test_actor_memory import actor as actor
from test_actor_memory import actor_storage as actor_storage
from test_actor_memory import execute, http_app, read, request
from test_group_context import owner
from test_segments import MATERIALS, archive, forget_original, front, ready
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples

from tianshu_memory.actor_memory.application import INTERNAL
from tianshu_memory.actor_memory.authority import Authority
from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.observation_actor_sources import ObservationActorSources


@pytest.fixture
def observed(tmp_path, source_contracts, source_examples, certificates, monkeypatch):
    h = SyncHarness(tmp_path / "observed", source_contracts, source_examples, certificates, "group")
    with h.owners(), h.runtime(monkeypatch):
        ready(h)
        yield h


def reference(original, epoch=1):
    return dict(
        producer="observation",
        object_id=original["source_ref"],
        revision=f"{original['scope_version']}:{epoch}",
        content_hash=original["source_digest"],
    )


def source_request(original, *, facts=False, context=None):
    return request(
        "source_facts" if facts else "source_check",
        refs=[reference(original)],
        **({} if facts else dict(context=context or copy.deepcopy(INTERNAL))),
    )


def bind_actor(actor, h):
    authority = Authority(
        h.config_path, actor["contracts"], observation_sources=ObservationActorSources(h.service)
    )
    authority.actor = actor["owner"].actor
    authority.context = actor["owner"].context
    actor["app"].authority = authority
    return authority


def append_observed(actor, original):
    return execute(
        actor,
        request(
            idempotency_key="observer:" + original["source_ref"],
            source=reference(original),
            perspective="observer",
        ),
    )


def user_snapshot(h):
    tables = (
        "metadata",
        "owner_heads",
        "sources",
        "scopes",
        "groups",
        "records",
        "history",
        "physical_sources",
        "source_admissions",
        "memory_materials",
        "memory_material_aliases",
        "suppression",
    )
    with h.service.local_commit() as db:
        result = {
            table: sorted(
                [list(row) for row in db.execute("SELECT * FROM " + table)], key=canonical
            )
            for table in tables
        }
    return fingerprint(result), h.store.recovery_path.read_bytes()


def core_alias(h, original):
    other = owner(h, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(h, original["event"]["text"], "observer-alias", other, accept=False)
    h.passive_owner.resolve[fingerprint(event["sources"][0])] = dict(
        **{key: original[key] for key in ("source_ref", "source_digest", "scope_version")},
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        archive_epoch=1,
    )
    accepted = h.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200, accepted.text
    return other, event


def test_raw_observer_is_independent_of_user_extraction_and_reads_change_no_user_revision(
    observed,
    actor,
):
    h = observed
    _, original = archive(h, text="这段配乐让我想继续了解作曲。", accept_material=False)
    authority = bind_actor(actor, h)
    before = user_snapshot(h)
    receipt = append_observed(actor, original)
    fact = execute(actor, read(receipt))["episode"]["fact"]
    assert fact["source"] == reference(original)
    assert fact["relation"] == "observer" and fact["provenance"] == "observation"
    assert fact["outcome"] == "observed" and fact["content_reads"] == []
    assert original["event"]["text"] in fact["summary"]
    assert fact["occurred_at"] == original["event"]["sent_at"]
    assert fact["source_constraints"] == dict(
        kind="group", conversation_id=h.scope()["conversation_id"]
    )
    assert fact["source_constraints"]["conversation_id"] != original["event"]["conversation_id"]
    checked = authority.sources(source_request(original), [reference(original)], context=INTERNAL)
    assert checked[canonical(reference(original))]["read_allowed"]
    assert user_snapshot(h) == before


@pytest.mark.parametrize("change", ["hash", "revision", "leading_zero", "other_source"])
def test_descriptor_never_substitutes_another_body_or_epoch(observed, change):
    h = observed
    _, original = archive(h, accept_material=False)
    payload = source_request(original)
    ref = payload["refs"][0]
    if change == "hash":
        ref["content_hash"] = "0" * 64
    elif change == "revision":
        ref["revision"] = "1:2"
    elif change == "leading_zero":
        ref["revision"] = "01:1"
    else:
        ref["object_id"] = "obs:" + "0" * 64
    adapter = ObservationActorSources(h.service)
    if change == "leading_zero":
        with pytest.raises(Fault, match="not_found"):
            adapter(payload)
    else:
        checked = adapter(payload)["items"][0]
        assert checked["state"] == "missing" and not checked["read_allowed"]


def test_same_group_only_disclosure_keeps_exact_core_audience(observed):
    h = observed
    _, original = archive(h, accept_material=False)
    adapter = ObservationActorSources(h.service)
    for kind, conversation, allowed in (
        ("group", h.scope()["conversation_id"], True),
        ("group", "different:group", False),
        ("private", h.scope()["conversation_id"], False),
    ):
        audience = dict(
            kind=kind, conversation_id=conversation, assertion_ref="real:checked:elsewhere"
        )
        if kind == "private":
            audience["person_id"] = h.scope()["person_id"]
        checked = adapter(
            source_request(original, context=dict(purpose="dialogue", audience=audience))
        )["items"][0]
        assert checked["read_allowed"] and checked["disclosure_allowed"] is allowed


def test_changed_original_payload_cannot_supply_model_or_episode_facts(observed):
    h = observed
    _, original = archive(h, accept_material=False)
    replaced = copy.deepcopy(original)
    replaced["event"]["text"] = "This was never the owner-attested original."
    with h.service.local_commit() as db:
        db.execute(
            "UPDATE observation_payloads SET payload=? WHERE source_ref=?",
            (canonical(replaced), original["source_ref"]),
        )
    with pytest.raises(Fault, match="dependency_unavailable"):
        ObservationActorSources(h.service)(source_request(original, facts=True))


@pytest.mark.parametrize("direction", ["observation", "core"])
def test_original_and_alias_forget_invalidate_actor_derived_without_user_writes(
    observed,
    actor,
    direction,
):
    h = observed
    material, original = archive(h, text="请以后忘记这次配乐话题。")
    other, event = core_alias(h, original)
    bind_actor(actor, h)
    episode = append_observed(actor, original)
    derived = execute(actor, derive(episode, text="我想进一步了解配乐。"))
    forgotten = (
        material
        if direction == "observation"
        else dict(
            material, source_key=h.service.source_identity(event["sources"][0], event["scope"])
        )
    )
    forget_original(h, forgotten, other)
    before = user_snapshot(h)
    result = check(actor, derived["ref"])["items"][0]
    assert result["state"] == "withdrawn" and not result["read_allowed"]
    with pytest.raises(Fault, match="not_found"):
        execute(
            actor,
            request(
                idempotency_key="observer:late", source=reference(original), perspective="observer"
            ),
        )
    assert user_snapshot(h) == before


def test_fresh_core_alias_withdrawal_is_read_without_advancing_user_heads(observed, actor):
    h = observed
    _, original = archive(h)
    _, event = core_alias(h, original)
    bind_actor(actor, h)
    episode = append_observed(actor, original)
    derived = execute(actor, derive(episode))
    physical_key = {
        key: value for key, value in event["sources"][0]["message_key"].items() if key != "revision"
    }
    physical = next(value for value in h.physicals if value["key"] == physical_key)
    physical.update(
        state="withdrawn",
        kind="retract",
        revision=2,
        physical_receipt_id="synthetic-withdrawal",
        content_digest="b" * 64,
    )
    h.core_head["sequence"] += 1
    before = user_snapshot(h)
    result = check(actor, derived["ref"])["items"][0]
    assert result["state"] == "withdrawn" and not result["read_allowed"]
    assert user_snapshot(h) == before


def test_owner_revocation_and_rebinding_are_distinct_from_transport_failure(observed, actor):
    h = observed
    _, original = archive(h, accept_material=False)
    bind_actor(actor, h)
    episode = append_observed(actor, original)
    derived = execute(actor, derive(episode))
    original_verifier = h.passive_owner

    class Unavailable:
        def __call__(self, body):
            raise Fault("dependency_unavailable", 503)

    h.service.observations.verifier = Unavailable()
    with pytest.raises(Fault, match="dependency_unavailable"):
        check(actor, derived["ref"])
    h.service.observations.verifier = original_verifier
    assert check(actor, derived["ref"])["items"][0]["state"] == "current"
    h.passive_owner.actor = "actor:b"
    assert not check(actor, derived["ref"])["items"][0]["read_allowed"]
    h.passive_owner.actor = "actor:a"
    assert not check(actor, derived["ref"])["items"][0]["read_allowed"]


def test_archive_epoch_revocation_and_mid_read_change_never_return_stale_facts(observed):
    h = observed
    _, original = archive(h, accept_material=False)
    adapter = ObservationActorSources(h.service)
    base = h.passive_owner
    calls = 0

    class Moving:
        def __call__(self, body):
            nonlocal calls
            result = base(body)
            if body["operation"] == "segment":
                calls += 1
                if calls == 1:
                    base.revision += 1
            return result

    h.service.observations.verifier = Moving()
    with pytest.raises(Fault, match="source_changed"):
        adapter(source_request(original, facts=True))
    h.service.observations.verifier = base
    base.archive_epoch = 2
    checked = adapter(source_request(original))["items"][0]
    assert checked["state"] == "withdrawn" and not checked["read_allowed"]


def test_http_pre_model_source_read_reuses_actor_permissions_without_creating_episode(
    observed,
    actor,
):
    h = observed
    _, original = archive(h, accept_material=False)
    bind_actor(actor, h)
    app, _ = http_app(actor, h.contracts)
    path = "/internal/v1/actor-self-memory/observation-source"
    header = {"Authorization": "Bearer test-actor-token"}
    before = user_snapshot(h)
    with TestClient(app) as client:
        # Occupying user admission never consumes the actor read port.
        held = [
            app.state.chat_requests.claim() for _ in range(app.state.chat_requests.ordinary_limit)
        ]
        try:
            checked = client.post(path, json=source_request(original), headers=header)
            assert checked.status_code == 200, checked.text
            assert checked.json()["items"][0]["read_allowed"]
            facts = client.post(path, json=source_request(original, facts=True), headers=header)
            assert facts.status_code == 200, facts.text
            assert facts.json()["facts"][0]["relation"] == "observer"
            for body in (
                dict(source_request(original), actor_id="actor:b"),
                dict(
                    source_request(original), refs=[dict(reference(original), producer="companion")]
                ),
                source_request(
                    original,
                    context=dict(
                        purpose="dialogue",
                        audience=dict(
                            kind="group",
                            conversation_id=h.scope()["conversation_id"],
                            assertion_ref="untrusted:assertion",
                        ),
                    ),
                ),
            ):
                response = client.post(path, json=body, headers=header)
                assert response.status_code == 403, response.text
            actor["owner"].allowed = False
            denied = client.post(path, json=source_request(original), headers=header)
            assert denied.status_code == 403, denied.text
            actor["owner"].allowed = True
            config_path = actor["tmp"] / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["callers"]["companion"]["actor_memory_operations"] = ["append_episode"]
            config_path.write_text(canonical(config), encoding="utf-8")
            denied = client.post(path, json=source_request(original), headers=header)
            assert denied.status_code == 403, denied.text
        finally:
            for lease in held:
                lease.finish()
        with actor["store"].transaction() as db:
            assert db.execute("SELECT count(*) FROM actor_episodes").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM actor_receipts").fetchone()[0] == 0
    assert user_snapshot(h) == before


def test_http_source_read_rechecks_role_after_owner_work(observed, actor):
    h = observed
    _, original = archive(h, accept_material=False)
    authority = bind_actor(actor, h)
    before = user_snapshot(h)
    actor_check = actor["owner"].actor

    def revoke_after_first_role(request):
        result = actor_check(request)
        actor["owner"].allowed = False
        return result

    authority.actor = revoke_after_first_role
    app, _ = http_app(actor, h.contracts)
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/actor-self-memory/observation-source",
            json=source_request(original, facts=True),
            headers={"Authorization": "Bearer test-actor-token"},
        )
        assert response.status_code == 403 and "facts" not in response.json()
    assert user_snapshot(h) == before
