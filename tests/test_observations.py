"""Passive source migration, attestation, replay and bounded read tests."""

import copy

import pytest

from tianshu_memory.domain import Fault, fingerprint
from tianshu_memory.observations import ObservationLedger
from tianshu_memory.store import Store


def event(*, self_id="10001", conversation="group:20002", author="30003", native="44"):
    return {
        "schema_version": 2,
        "platform_id": "instance-a",
        "self_id": self_id,
        "namespace": "qq",
        "conversation_id": conversation,
        "account_id": author,
        "event_id": native,
        "revision": 1,
        "sent_at": "2026-09-29T01:00:00Z",
        "text": "hello",
        "content_state": "text",
        "mentioned": False,
        "scope_revision": 1,
    }


def source(value):
    return {
        "event": value,
        "source_ref": "obs:"
        + fingerprint(
            [
                value["platform_id"],
                value["self_id"],
                value["conversation_id"],
                value["account_id"],
                value["event_id"],
            ]
        )[:64],
        "source_digest": fingerprint(value),
        "scope_version": 1,
    }


class Verifier:
    def __init__(self):
        self.values = {}
        self.archive_epoch = 1
        self.ingest_allowed = True
        self.read_allowed = True

    def __call__(self, request):
        if request["operation"] == "scope":
            if not self.read_allowed:
                raise Fault("forbidden", 403)
            return {"valid": True, "archive_epoch": self.archive_epoch}
        value = self.values.get(request["source_ref"])
        if not value or not self.ingest_allowed or self.archive_epoch != 1:
            raise Fault("dependency_unavailable", 503)
        event = value["event"]
        return {
            "valid": True,
            "source_ref": value["source_ref"],
            "source_digest": value["source_digest"],
            "scope_version": 1,
            "archive_epoch": 1,
            "instance_id": event["platform_id"],
            "self_id": event["self_id"],
            "conversation_id": event["conversation_id"],
            "account_id": event["account_id"],
            "event_id": event["event_id"],
        }


@pytest.fixture
def ledger(tmp_path, contracts):
    store = Store(tmp_path / "memory.sqlite")
    store.migrate_profiles(tmp_path / "profiles.bak")
    contracts.load_sources()
    store.migrate_sources(tmp_path / "sources.bak", contracts)
    result = store.migrate_observations(tmp_path / "observations.bak")
    assert result["observation_schema"] == 1
    assert (tmp_path / "observations.bak").is_file()
    verifier = Verifier()
    return ObservationLedger(store, verifier), verifier


def test_archive_requires_attestation_and_is_idempotent(ledger):
    app, verifier = ledger
    value = source(event())
    with pytest.raises(Fault):
        app.ingest("platform", value)
    with pytest.raises(Fault):
        app.ingest("companion", value)
    verifier.values[value["source_ref"]] = value
    assert app.ingest("companion", value)["state"] == "accepted"
    assert app.ingest("companion", value)["state"] == "duplicate"
    bad = copy.deepcopy(value)
    bad["event"]["text"] = "different"
    with pytest.raises(Fault):
        app.ingest("companion", bad)
    with app.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM observation_sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM turn_inputs").fetchone()[0] == 0


def test_isolation_pagination_pause_and_revocation(ledger):
    app, verifier = ledger
    values = [
        source(event(native="44")),
        source(event(native="45")),
        source(event(self_id="90009", native="44")),
        source(event(conversation="group:70007", native="44")),
    ]
    for value in values:
        verifier.values[value["source_ref"]] = value
    # Simulated pause: old accepted sources remain eligible to finish archiving.
    for value in values:
        assert app.ingest("companion", value)["archive_state"] == "archived"
    request = {
        "instance_id": "instance-a",
        "self_id": "10001",
        "conversation_id": "group:20002",
        "limit": 1,
        "cursor": None,
    }
    page = app.query("companion", request)
    assert len(page["items"]) == 1 and page["next_cursor"]
    request["cursor"] = page["next_cursor"]
    assert len(app.query("companion", request)["items"]) == 1
    verifier.archive_epoch = 2
    request["cursor"] = None
    assert app.query("companion", request)["items"] == []
    with pytest.raises(Fault):
        app.ingest("companion", values[0])
