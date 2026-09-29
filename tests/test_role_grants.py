"""Exact actor grants are persistent and cannot replace static deployment grants."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from tianshu_memory.app import create_app
from tianshu_memory.auth import Authenticator
from tianshu_memory.domain import Fault
from tianshu_memory.role_grants import RoleGrants

CONTRACT = Path(__file__).resolve().parents[1] / "docs/contracts-candidates/role-runtime/v1"


def test_candidate_memory_exchange_matches_schema():
    schema = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
    examples = json.loads((CONTRACT / "examples.json").read_text(encoding="utf-8"))
    for name in ("memory_apply", "memory_result"):
        Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"}).validate(examples[name])


def test_grant_replay_revoke_and_restart(tmp_path):
    path = tmp_path / "role-grants.sqlite"
    grants = RoleGrants(path)
    create = {
        "request_id": "role-a-create",
        "actor_id": "actor:role-a",
        "expected_version": 0,
        "enabled": True,
        "legacy": False,
    }
    assert grants.apply(create, {"actor:household"}) == {
        "actor_id": "actor:role-a",
        "version": 1,
        "enabled": True,
    }
    assert grants.apply(create, {"actor:household"})["version"] == 1
    assert grants.active("actor:role-a")
    assert not grants.active("actor:role-b")
    with pytest.raises(Fault) as reused:
        grants.apply({**create, "enabled": False}, set())
    assert reused.value.code == "idempotency_conflict"
    with pytest.raises(Fault) as stale:
        grants.apply({**create, "request_id": "new", "enabled": False}, set())
    assert stale.value.code == "version_conflict"
    with pytest.raises(Fault):
        grants.apply({**create, "request_id": "static", "actor_id": "actor:household"},
                     {"actor:household"})
    revoke = {**create, "request_id": "role-a-revoke", "expected_version": 1, "enabled": False}
    assert grants.apply(revoke, set())["version"] == 2
    assert not RoleGrants(path).active("actor:role-a")
    assert RoleGrants(path).status("actor:role-a") == {
        "actor_id": "actor:role-a",
        "version": 2,
        "enabled": False,
    }


def test_explicit_static_role_adoption_can_deny_existing_grant(tmp_path):
    grants = RoleGrants(tmp_path / "role-grants.sqlite")
    actor = "actor:household"
    assert grants.decision(actor) is None
    adopted = {
        "request_id": "adopt-static", "actor_id": actor, "expected_version": 0,
        "enabled": True, "legacy": True,
    }
    assert grants.apply(adopted, {actor})["enabled"]
    assert grants.decision(actor) is True
    disabled = {**adopted, "request_id": "disable-static", "expected_version": 1,
                "enabled": False}
    assert not grants.apply(disabled, {actor})["enabled"]
    assert grants.decision(actor) is False
    assert grants.inactive_ids() == [actor]
    assert grants.apply(adopted, {actor})["enabled"]
    assert grants.decision(actor) is False


def test_adopted_static_role_remains_denied_after_auth_restart(h, tmp_path):
    path = tmp_path / "grants.sqlite"
    h.config["role_grants_database_path"] = str(path)
    actor = "actor:household"
    scope = {**h.private, "actor_id": actor}
    h.add_origin("origin-household", scope)
    h.config["callers"]["companion"]["allowed_actors"].append(actor)
    h.save_config()
    grants = RoleGrants(path)
    grants.apply({"request_id": "adopt", "actor_id": actor, "expected_version": 0,
                  "enabled": True, "legacy": True}, {actor})
    grants.apply({"request_id": "disable", "actor_id": actor, "expected_version": 1,
                  "enabled": False, "legacy": True}, {actor})
    for _ in range(2):
        h.auth = Authenticator(h.config_path, h.contracts, h.clock)
        h.client = TestClient(create_app(service=h.service, auth=h.auth))
        request = h.selection(scope=scope)
        request["query"]["origin"]["assertion_ref"] = "origin-household"
        denied = h.post("memory/select", request)
        assert denied.status_code == 403
        assert denied.json()["code"] == "forbidden"
    assert RoleGrants(path).decision(actor) is False


def test_same_person_conversation_static_and_dynamic_actors_cannot_cross_read(h, tmp_path):
    h.seed()
    path = tmp_path / "grants.sqlite"
    h.config["role_grants_database_path"] = str(path)
    h.config["callers"]["companion"]["allow_runtime_roles"] = True
    other = {**h.private, "actor_id": "actor:other"}
    h.add_origin("origin-other", other)
    h.save_config()
    grants = RoleGrants(path)
    grants.apply({"request_id": "grant:other", "actor_id": other["actor_id"],
                  "expected_version": 0, "enabled": True, "legacy": False},
                 {h.private["actor_id"]})
    h.auth = Authenticator(h.config_path, h.contracts, h.clock)
    h.client = TestClient(create_app(service=h.service, auth=h.auth))
    own_a = h.post("memory/select", h.selection())
    assert own_a.status_code == 200 and own_a.json()["selected_units"]
    own_b = h.selection(scope=other)
    own_b["query"]["origin"]["assertion_ref"] = "origin-other"
    result_b = h.post("memory/select", own_b)
    assert result_b.status_code == 200 and not result_b.json()["selected_units"]
    crossed_a = h.selection(scope=other)
    crossed_b = h.selection()
    crossed_b["query"]["origin"]["assertion_ref"] = "origin-other"
    assert h.post("memory/select", crossed_a).status_code == 403
    assert h.post("memory/select", crossed_b).status_code == 403
