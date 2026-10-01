"""Real loopback HTTPS owners and live auth; all facts/principals are synthetic."""

import copy

import pytest
from source_sync_harness import SyncHarness
from test_auth_https import certificates as certificates
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory.relationship_migration import migrate


@pytest.fixture
def synced(tmp_path, source_contracts, source_examples, certificates, monkeypatch):
    harness = SyncHarness(tmp_path, source_contracts, source_examples, certificates)
    for index, turn in enumerate(harness.turns):
        replies = [f"synthetic-affinity-reply:{index}"]
        turn.update(phase="sent", delivery_state="sent", reply_ids=replies)
        turn["committed_event"].update(delivery_state="sent", reply_ids=replies)
    with harness.owners(), harness.runtime(monkeypatch):
        migrate(
            harness.store, tmp_path / "relationships-backup.sqlite", clock=harness.service.clock
        )
        caller = harness.config["callers"]["companion"]
        caller["operations"].extend(
            [
                "relationships.read",
                "relationships.check",
                "relationships.settle",
                "relationships.manage",
            ]
        )
        caller["role_admin"] = True
        for context in harness.contexts.values():
            context["principal_id"] = "synthetic-console-admin"
        harness.save()
        yield harness


def body(h, **values):
    command = h.command()
    return dict({k: command[k] for k in ("schema_version", "request_id", "origin")}, **values)


def proof(h, event):
    return dict(
        event_id="synthetic-affinity",
        pair={k: h.scope()[k] for k in ("actor_id", "person_id")},
        kind="conversation_completed",
        turn_id=event["aggregate_id"],
        source_ref=h.service.source_identity(event["sources"][0], h.scope()),
        source_revision=event["sources"][0]["message_key"]["revision"],
        occurred_at=event["occurred_at"],
    )


def test_https_read_settle_freeze_and_owner_revocation(synced):
    h = synced
    _, _, event = h.seed()
    pair = {k: h.scope()[k] for k in ("actor_id", "person_id")}
    result = h.post("relationships/settle", body(h, candidate=proof(h, event)))
    assert result.status_code == 200, result.text
    assert result.json()["settlement"]["applied_delta"] == 1
    current = h.post("relationships/read", body(h, pair=pair)).json()["projection"]
    command = body(h)
    command["command"] = dict(
        request_id=command["request_id"],
        pair=pair,
        expected_version=current["version"],
        operation="set_freeze",
        frozen=True,
    )
    assert h.post("relationships/manage", command).status_code == 200
    frozen = h.post("relationships/read", body(h, pair=pair)).json()["projection"]
    h.physicals[0].update(
        revision=2,
        state="withdrawn",
        kind="retract",
        content_digest="b" * 64,
        physical_receipt_id="synthetic-affinity-withdrawal",
    )
    h.core_head["sequence"] += 1
    response = h.post("relationships/read", body(h, pair=pair))
    assert response.status_code == 200, response.text
    assert response.json()["projection"]["score"] == 0
    assert response.json()["projection"]["frozen"] is True
    assert (
        h.post(
            "relationships/check", body(h, pair=pair, expected_version=frozen["version"])
        ).status_code
        == 409
    )


def test_https_source_outage_never_serves_cached_private_projection(synced):
    h = synced
    _, _, event = h.seed()
    assert h.post("relationships/settle", body(h, candidate=proof(h, event))).status_code == 200
    pair = {k: h.scope()[k] for k in ("actor_id", "person_id")}
    h.status["snapshot"] = 503
    result = h.post("relationships/read", body(h, pair=pair))
    assert result.status_code == 503 and "projection" not in result.json()


def test_https_origin_revocation_rejects_existing_receipt(synced):
    h = synced
    _, _, event = h.seed()
    payload = body(h, candidate=proof(h, event))
    assert h.post("relationships/settle", payload).status_code == 200
    h.status["origin"] = 403
    assert h.post("relationships/settle", payload).status_code == 403


def test_https_other_conversation_source_is_refreshed_before_pair_projection(synced):
    h = synced
    # Admit a distinct synthetic physical in another conversation for the same
    # role/person. Both original admissions otherwise refer to one physical.
    physical = copy.deepcopy(h.physicals[0])
    physical["key"]["message_id"] = "synthetic-other-message"
    physical["key"]["channel"]["channel_conversation_id"] = "synthetic-other-channel"
    physical.update(
        conversation_id="synthetic-other-conversation",
        physical_receipt_id="synthetic-other-physical",
    )
    h.physicals.append(physical)
    admission = h.admissions[1]
    admission["scope"].update(
        actor_id=h.scope()["actor_id"], conversation_id=physical["conversation_id"]
    )
    admission["selector"] = dict(key=copy.deepcopy(physical["key"]), actor_id=h.scope()["actor_id"])
    admission["source"]["message_key"] = dict(copy.deepcopy(physical["key"]), revision=1)
    admission["source"]["receipt_id"] = "synthetic-other-admission"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    admission["accepted_origin"]["assertion_ref"] = "actor-origin:actor:a:other"
    context = h.contexts["synthetic-viewer:1"]
    context["allowed_scope"] = copy.deepcopy(admission["scope"])
    context["verified_channel"] = copy.deepcopy(physical["key"]["channel"])
    h.grants[1]["selector"] = copy.deepcopy(admission["selector"])
    h.turns[1]["scope"] = copy.deepcopy(admission["scope"])
    h.turns[1]["input_sources"] = [copy.deepcopy(admission["source"])]
    h.turns[1]["committed_event"].update(
        scope=copy.deepcopy(admission["scope"]),
        conversation_id=physical["conversation_id"],
        sources=[copy.deepcopy(admission["source"])],
    )
    h.config["callers"]["companion"]["event_scopes"] = [h.scope(0), h.scope(1)]
    h.save()
    _, _, event = h.seed(1)
    value = proof(h, event)
    value["source_ref"] = h.service.source_identity(event["sources"][0], h.scope(1))
    payload = body(h, candidate=value)
    payload["origin"]["assertion_ref"] = "synthetic-viewer:1"
    assert h.post("relationships/settle", payload).status_code == 200
    h.physicals[1].update(
        revision=2,
        state="withdrawn",
        kind="retract",
        content_digest="c" * 64,
        physical_receipt_id="synthetic-other-withdrawal",
    )
    h.core_head["sequence"] += 1
    pair = {k: h.scope()[k] for k in ("actor_id", "person_id")}
    result = h.post("relationships/read", body(h, pair=pair))
    assert result.status_code == 200, result.text
    assert result.json()["projection"]["score"] == 0


def test_https_platform_grant_denial_corrects_score_and_rejects_replay(synced):
    h = synced
    _, _, committed = h.seed()
    payload = body(h, candidate=proof(h, committed))
    assert h.post("relationships/settle", payload).status_code == 200
    h.grants[0]["state"] = "denied"
    h.platform_head["sequence"] += 1
    pair = {k: h.scope()[k] for k in ("actor_id", "person_id")}
    result = h.post("relationships/read", body(h, pair=pair))
    assert result.status_code == 200, result.text
    assert result.json()["projection"]["score"] == 0
    replay = h.post("relationships/settle", payload)
    assert replay.status_code in {403, 409}
    assert "settlement" not in replay.json()
