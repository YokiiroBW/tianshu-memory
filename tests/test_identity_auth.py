import copy
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tianshu_memory.app import create_app
from tianshu_memory.service import MemoryService


def test_unregistered_then_register_and_group_private_identity(h):
    account = {"namespace": "qq", "immutable_account_id": "10002"}
    first = dict(h.private, person_id=None, conversation_id=None)
    h.add_origin("new", first, account)
    h.save_config()
    result = h.post("identity/resolve", {"query": h.query("new"), "account": account})
    assert result.json()["state"] == "unregistered" and result.json()["binding_version"] == 0
    command = h.command("new", "stable")
    request = {"command": command, "account": account, "display_name": "First nickname"}
    created = h.post("identity/register", request).json()
    replay = copy.deepcopy(request)
    replay["command"]["request_id"] = "retry-request"
    assert h.post("identity/register", replay).json() == dict(created, request_id="retry-request")
    changed = copy.deepcopy(replay)
    changed["display_name"] = "Changed nickname"
    assert h.post("identity/register", changed).json()["code"] == "idempotency_conflict"
    changed["command"] = h.command("new")
    updated = h.post("identity/register", changed).json()
    assert updated["person_id"] == created["person_id"] and not updated["created"]
    for audience in ("group", "self_private"):
        h.add_origin(
            "new", dict(first, audience=audience, conversation_id="different-channel"), account
        )
        h.save_config()
        assert (
            h.post("identity/resolve", {"query": h.query("new"), "account": account}).json()[
                "person_id"
            ]
            == created["person_id"]
        )


def test_cross_platform_never_linked_by_nickname_and_link_unavailable(h):
    account = dict(h.account, namespace="tg")
    h.add_origin("telegram", dict(h.private, person_id=None), account)
    h.save_config()
    other = h.post(
        "identity/register",
        {"command": h.command("telegram"), "account": account, "display_name": "Same"},
    )
    assert other.status_code == 200 and other.json()["person_id"] != h.person
    request = {
        "command": h.command(),
        "source_account": h.account,
        "target_account": account,
        "source_binding_version": 1,
        "target_binding_version": 1,
        "verification_ref": "invented-proof",
    }
    assert h.post("identity/link", request).json()["code"] == "dependency_unavailable"


def test_concurrent_registration_one_binding(h):
    context = h.config["origins"]["origin-first"]
    requests = [{"command": h.command("origin-first"), "account": h.account} for _ in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda r: h.service.register(r, context), requests))
    assert {r["person_id"] for r in results} == {h.person}
    with h.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_auth_account_scope_expiry_revocation_and_replay_reauthorization(h):
    payload = {"query": h.query(), "account": dict(h.account, immutable_account_id="10004")}
    assert h.post("identity/resolve", payload).status_code == 403
    request = {"command": h.command(), "account": h.account}
    assert h.post("identity/register", request).status_code == 200
    h.config["origins"]["origin-private"]["expires_at"] = "2025-01-01T00:00:00Z"
    h.save_config()
    assert h.post("identity/register", request).status_code == 403
    assert h.client.post("/internal/v1/identity/register", json=request).status_code == 401
    h.config["callers"]["companion"]["token"] = "revoked-and-replaced"
    h.save_config()
    assert h.post("identity/register", request).status_code == 401


def test_unknown_schema_deadline_and_client_cannot_supply_context(h):
    request = {"command": h.command(), "account": h.account}
    request["command"]["deadline_at"] = "2020-01-01T00:00:00Z"
    assert h.post("identity/register", request).json()["code"] == "timeout"
    request["command"]["schema_version"] = 2
    assert h.post("identity/register", request).json()["code"] == "unsupported_version"
    request["command"]["schema_version"] = 1
    request["trusted_context"] = h.config["origins"]["origin-private"]
    assert h.post("identity/register", request).status_code == 400


def test_unconfigured_app_and_source_backend_fail_closed(h):
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 503
        assert client.post("/internal/v1/memory/select", json=h.selection()).status_code == 503
    service = MemoryService(h.store, h.contracts, clock=h.clock)
    with TestClient(create_app(service=service, auth=h.auth)) as client:
        response = client.post(
            "/internal/v1/memory/select",
            json=h.selection(),
            headers={"Authorization": "Bearer test-only-companion-secret"},
        )
        assert response.status_code == 503


def test_first_select_waits_for_issuer_conversation_mapping_without_widening_scope(h):
    h.seed()
    h.add_origin("origin-private", dict(h.private, person_id=None, conversation_id=None))
    h.save_config()
    request = h.selection(budget=0)
    unresolved = h.post("memory/select", request)
    assert unresolved.status_code == 503
    assert unresolved.json()["code"] == "dependency_unavailable"
    assert "selected_units" not in unresolved.json()
    wrong_actor = h.selection(budget=0)
    wrong_actor["requested_scope"]["actor_id"] = "actor-other"
    assert h.post("memory/select", wrong_actor).status_code == 403
    # Synthetic issuer models a trusted ingest receipt binding its verified channel.
    h.add_origin("origin-private", dict(h.private, person_id=None))
    h.save_config()
    resolved = h.post("memory/select", h.selection())
    assert resolved.status_code == 200 and len(resolved.json()["selected_units"]) == 1
    h.add_origin("origin-private", dict(h.private, conversation_id="wrong-known-conversation"))
    h.save_config()
    mismatch = h.post("memory/select", request)
    assert mismatch.status_code == 403 and mismatch.json()["code"] == "forbidden"
