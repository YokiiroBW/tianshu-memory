import copy

import pytest
from fastapi.testclient import TestClient
from test_process import server
from test_profile_queries import profile_request, reader
from test_profiles import profile_draft, publish

from tianshu_memory.app import create_app
from tianshu_memory.contracts import Contracts


def enable_profiles(h, *, migrate=True):
    if migrate:
        h.store.migrate_profiles(h.directory / "before-profile-http.sqlite")
    h.service.contracts = Contracts(h.contracts.directory)
    h.service.contracts.load_profiles()
    h.client.close()
    h.client = TestClient(create_app(service=h.service, auth=h.auth))
    h.config["callers"]["companion"]["operations"].append("select_profiles")
    h.save_config()
    reader(h)


@pytest.fixture
def p(h):
    enable_profiles(h)
    h.seed()
    publish(h, profile_draft(h))
    return h


def test_profile_http_auth_current_audience_and_schema(p):
    h = p
    request = profile_request(h)
    response = h.post("memory/profiles/select", request)
    assert response.status_code == 200, response.text
    h.service.contracts.validate("profiles#select_response", response.json())
    assert response.json()["version_domain"] == "profile-memory/v1"
    assert response.headers["cache-control"] == "no-store"
    assert h.client.post("/internal/v1/memory/profiles/select", json=request).status_code == 401
    for target in (
        {"kind": "person", "display_name": "B"},
        {"kind": "group", "person_id": h.person},
    ):
        assert h.post("memory/profiles/select", dict(request, target=target)).status_code == 400
    for field in ("person_id", "actor_id", "conversation_id", "audience"):
        changed = copy.deepcopy(request)
        changed["requester_scope"][field] = "self_private" if field == "audience" else "forged"
        assert h.post("memory/profiles/select", changed).status_code == 403
    group = dict(
        request, target={"kind": "group", "conversation_id": "other-group"}, selection=["topic"]
    )
    assert h.post("memory/profiles/select", group).status_code == 403
    h.config["origins"]["reader-group"]["revoked"] = True
    h.save_config()
    assert h.post("memory/profiles/select", request).status_code == 403


def test_profile_http_requires_migration_and_separate_service_operation(h):
    enable_profiles(h, migrate=False)
    request = profile_request(h)
    assert h.post("memory/profiles/select", request).status_code == 503
    h.config["callers"]["companion"]["operations"].remove("select_profiles")
    h.save_config()
    assert h.post("memory/profiles/select", request).status_code == 403
    h.seed()
    assert h.select()["selected_units"]


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_profile_local_http_restart_rebuild_and_atomic_revision(p, kind):
    h = p
    publish(
        h,
        profile_draft(h, sharing="group_only", conversation=h.group["conversation_id"]),
        "origin-group",
    )
    h.add_origin("owner-other", dict(h.group, conversation_id="group-other"))
    publish(h, profile_draft(h, sharing="group_only", conversation="group-other"), "owner-other")
    h.save_config()
    v1 = h.select()
    h.workflow.rebuild_index()
    requests = [
        profile_request(h, origin=o) for o in ("reader-group", "reader-other", "reader-private")
    ]
    with server(h.config_path) as client:
        versions = []
        for request in requests:
            response = client.post("/internal/v1/memory/profiles/select", json=request)
            assert response.status_code == 200, response.text
            assert response.json()["selected_units"]
            versions.append(response.json()["scope_version"])
        assert (
            client.post("/internal/v1/memory/select", json=h.selection()).json()["selected_units"]
            == v1["selected_units"]
        )
        revision = h.revision(v1["selected_units"][0]["record_id"], kind)
        assert client.post("/internal/v1/memory/revise", json=revision).status_code == 200
    with server(h.config_path) as client:
        for request, version in zip(requests, versions, strict=True):
            response = client.post("/internal/v1/memory/profiles/select", json=request)
            assert response.status_code == 200, response.text
            assert response.json()["selected_units"] == []
            probe = dict(request, known_scope_version=version, budget={"tokens": 0, "bytes": 0})
            assert (
                client.post("/internal/v1/memory/profiles/select", json=probe).json()["code"]
                == "scope_changed"
            )
        assert (
            client.post("/internal/v1/memory/select", json=h.selection()).json()["selected_units"]
            == []
        )
        assert (
            client.post("/internal/v1/memory/revise", json=revision).json()["record_version"] == 2
        )


@pytest.mark.parametrize("withdrawal", ["withdrawn", "correct", "forget"])
def test_withdrawn_profiles_do_not_leak_later_private_source_versions(p, withdrawal):
    h = p
    publish(
        h,
        profile_draft(h, sharing="group_only", conversation=h.group["conversation_id"]),
        "origin-group",
    )
    requests = [
        profile_request(h, origin=origin)
        for origin in ("reader-group", "reader-other", "reader-private")
    ]
    before = [h.post("memory/profiles/select", request).json() for request in requests]
    assert all(result["selected_units"] for result in before)
    private_id = h.select()["selected_units"][0]["record_id"]
    if withdrawal == "withdrawn":
        h.workflow.observe_source(h.source(), h.private, state="withdrawn")
    else:
        assert h.post("memory/revise", h.revision(private_id, withdrawal)).status_code == 200
    versions = []
    for request, previous in zip(requests, before, strict=True):
        result = h.post("memory/profiles/select", request).json()
        assert result["selected_units"] == []
        assert result["scope_version"] > previous["scope_version"]
        versions.append(result["scope_version"])
    # Fresh private revisions restore/change the source ledger, not its old sharing approvals.
    for revision in (2, 3):
        h.workflow.observe_source(h.source(revision=revision), h.private)
        for request, version in zip(requests, versions, strict=True):
            result = h.post("memory/profiles/select", request).json()
            assert result["selected_units"] == [] and result["scope_version"] == version
            probe = dict(request, known_scope_version=version, budget={"tokens": 0, "bytes": 0})
            response = h.post("memory/profiles/select", probe)
            assert response.status_code == 200, response.text
            assert response.json()["budget_used"] == {"tokens": 0, "bytes": 0}
    with h.store.transaction() as db:
        # Retain authoritative source/history updates and a prior forget's tombstone.
        assert db.execute("SELECT MAX(revision) FROM sources").fetchone()[0] == 3
        record = db.execute(
            "SELECT version,state FROM records WHERE id=?", (private_id,)
        ).fetchone()
        assert record["version"] == 4
        assert record["state"] == ("tombstoned" if withdrawal == "forget" else "invalidated")
    publish(h, profile_draft(h, units=[h.unit(h.source(revision=3))]))
    for request, version in zip(requests, versions, strict=True):
        result = h.post("memory/profiles/select", request).json()
        assert len(result["selected_units"]) == 1 and result["scope_version"] > version
