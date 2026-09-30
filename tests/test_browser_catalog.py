"""Real Memory HTTP and SQLite with isolated synthetic HTTPS owner services."""

import copy
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from source_sync_harness import SyncHarness
from test_auth_https import certificates as certificates
from test_process import server
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory.app import create_app
from tianshu_memory.auth import Authenticator
from tianshu_memory.domain import canonical
from tianshu_memory.role_grants import RoleGrants


@pytest.fixture
def catalog(tmp_path, source_contracts, source_examples, certificates, monkeypatch):
    h = SyncHarness(tmp_path, source_contracts, source_examples, certificates)
    h.config["callers"]["platform"] = {
        "token": "synthetic-platform-browser-only-credential",
        "issuer": "platform",
        "issuer_token": "synthetic-origin-token",
        "issuer_ca_file": str(certificates / "ca.pem"),
        "allowed_actors": ["actor:a", "actor:b"],
        "operations": ["browse"],
    }
    # Nonempty before composition so the pinned profile contract is loaded.
    h.config["browser_readers"] = {"platform": {}}
    with h.owners():
        h.config["callers"]["platform"]["issuer_url"] = h.config["callers"]["companion"][
            "issuer_url"
        ]
        h.save()
        with h.runtime(monkeypatch):
            context = copy.deepcopy(h.contexts["synthetic-viewer:0"])
            context.update(assertion_ref="synthetic-browser", authenticated_service="platform")
            h.contexts["synthetic-browser"] = context
            h.config["browser_readers"]["platform"] = {
                "account": h.account,
                "actor_id": "actor:a",
                "scopes": [h.scope()],
            }
            h.save()
            yield h


def body(h, **fields):
    value = {
        "schema_version": 1,
        "request_id": h.command()["request_id"],
        "origin": {"assertion_ref": "synthetic-browser"},
        "scope": h.scope(),
    }
    value.update(fields)
    return value


def post(h, operation, value=None, token="synthetic-platform-browser-only-credential"):
    return h.client.post(
        f"/internal/v1/memory/browser/{operation}",
        json=value if value is not None else body(h),
        headers={"Authorization": f"Bearer {token}"},
    )


def assert_failed(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "synthetic-origin-token" not in response.text


def test_real_catalog_empty_then_current_and_revoked(catalog):
    empty = post(catalog, "records")
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == [] and empty.json()["next_cursor"] is None
    seeded, _, _ = catalog.seed()
    response = post(catalog, "records")
    assert response.status_code == 200, response.text
    group = response.json()["items"][0]
    assert group["semantic_group_id"] == seeded["group_ids"][0]
    assert group["units"][0]["record_id"] == seeded["record_ids"][0]
    assert "sources" not in group["units"][0]
    assert "synthetic-core-token" not in response.text
    assert post(catalog, "overview").json()["memory_group_count"] == 1
    # Revoking the current source grant is observed by the real barrier before the next read.
    catalog.grants[0]["state"] = "denied"
    catalog.platform_head["sequence"] += 1
    hidden = post(catalog, "records")
    assert hidden.status_code == 200 and hidden.json()["items"] == []


def test_credential_scope_and_owner_fail_closed(catalog):
    assert_failed(post(catalog, "records", token="wrong"), 401, "unauthorized")
    other_scope = body(catalog)
    other_scope["scope"] = catalog.scope(1)
    assert_failed(post(catalog, "records", other_scope), 403, "forbidden")
    context = catalog.contexts["synthetic-browser"]
    context["verified_account"] = {"namespace": "web", "immutable_account_id": "other"}
    assert_failed(post(catalog, "records"), 403, "forbidden")
    context["verified_account"] = catalog.account
    catalog.status["current"] = 503
    assert_failed(post(catalog, "records"), 503, "dependency_unavailable")
    catalog.status.clear()
    catalog.config["callers"]["platform"]["token"] = "rotated-platform-browser-credential"
    catalog.save()
    assert_failed(post(catalog, "records"), 401, "unauthorized")


@pytest.mark.parametrize("change", ["revoked", "scope", "credential"])
def test_mid_barrier_identity_change_refuses_before_read(catalog, change):
    catalog.seed()
    changed = False

    def during_access(kind, request, response):
        nonlocal changed
        if kind != "current" or changed:
            return
        changed = True
        if change == "revoked":
            catalog.contexts["synthetic-browser"]["revoked"] = True
        elif change == "scope":
            catalog.contexts["synthetic-browser"]["allowed_scope"] = catalog.scope(1)
        else:
            catalog.config["callers"]["platform"]["token"] = "rotated-platform-browser-credential"
            catalog.save()

    catalog.mutate = during_access
    response = post(catalog, "records")
    assert changed
    assert response.status_code in {400, 401, 403}, response.text
    assert "items" not in response.json()
    assert "咖啡" not in response.text


def test_owner_negative_commits_when_origin_revoked_during_barrier(catalog):
    seeded, _, _ = catalog.seed()

    def during_access(kind, request, response):
        if kind == "current":
            response["grants"][0]["state"] = "denied"
            catalog.grants[0]["state"] = "denied"
            catalog.platform_head["sequence"] += 1
            response["head"]["sequence"] = catalog.platform_head["sequence"]
            catalog.contexts["synthetic-browser"]["revoked"] = True

    catalog.mutate = during_access
    response = post(catalog, "records")
    assert response.status_code in {400, 403}, response.text
    assert "items" not in response.json()
    with catalog.store.transaction() as db:
        group = db.execute(
            "SELECT state FROM groups WHERE id=?", (seeded["group_ids"][0],)
        ).fetchone()
        assert group["state"] != "active"


def test_records_cursor_is_complete_and_invalidated_by_source_change(catalog):
    seeded, _, _ = catalog.seed()
    first_group = seeded["group_ids"][0]
    first_record = seeded["record_ids"][0]
    with catalog.store.transaction() as db:
        original = db.execute("SELECT * FROM groups WHERE id=?", (first_group,)).fetchone()
        record = db.execute("SELECT * FROM records WHERE id=?", (first_record,)).fetchone()
        lineage = db.execute("SELECT * FROM lineage WHERE group_id=?", (first_group,)).fetchone()
        second_group, second_record = "z-synthetic-group", "z-synthetic-record"
        db.execute(
            "INSERT INTO groups VALUES (?,?,?,?,?,?,?)",
            (
                second_group,
                original["scope"],
                "active",
                canonical([second_record]),
                original["category"],
                original["field_key"],
                original["item_key"],
            ),
        )
        unit = json.loads(record["payload"])
        unit.update(record_id=second_record, semantic_group_id=second_group)
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?,?)",
            (second_record, second_group, record["version"], "active", canonical(unit)),
        )
        db.execute(
            "INSERT INTO lineage VALUES (?,?,?,?)",
            (second_group, lineage["source_key"], lineage["revision"], lineage["epoch"]),
        )
    first = post(catalog, "records", body(catalog, limit=1))
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 1 and first.json()["next_cursor"]
    second = post(catalog, "records", body(catalog, limit=1, cursor=first.json()["next_cursor"]))
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1 and second.json()["next_cursor"] is None
    assert {
        first.json()["items"][0]["semantic_group_id"],
        second.json()["items"][0]["semantic_group_id"],
    } == {first_group, second_group}
    assert_failed(
        post(
            catalog,
            "records",
            body(
                catalog,
                limit=1,
                cursor=first.json()["next_cursor"],
                subject={"kind": "person", "person_id": "unshared-person"},
            ),
        ),
        400,
        "invalid_input",
    )
    catalog.grants[0]["state"] = "denied"
    catalog.platform_head["sequence"] += 1
    assert_failed(
        post(catalog, "records", body(catalog, cursor=first.json()["next_cursor"])),
        409,
        "scope_changed",
    )


def test_forged_cursor_and_expired_origin_never_read(catalog):
    catalog.seed()
    first = post(catalog, "records", body(catalog, limit=1))
    assert_failed(post(catalog, "records", body(catalog, cursor="forged")), 400, "invalid_input")
    assert first.status_code == 200
    catalog.contexts["synthetic-browser"]["expires_at"] = "2000-01-01T00:00:00Z"
    response = post(catalog, "records")
    assert response.status_code in {400, 403}
    assert "items" not in response.json() and "咖啡" not in response.text


def test_response_budget_refuses_oversized_complete_group(catalog, monkeypatch):
    from tianshu_memory import browser

    catalog.seed()
    monkeypatch.setattr(browser, "MAX_RESPONSE_BYTES", 128)
    assert_failed(post(catalog, "records"), 413, "response_too_large")


def test_shared_subjects_and_actual_http_process(catalog):
    catalog.profile_fixture()
    # The pinned browser identity can see the current share, but never its private source refs.
    subjects = post(catalog, "subjects")
    assert subjects.status_code == 200, subjects.text
    assert subjects.json()["items"][0]["subject"]["kind"] == "person"
    subject = subjects.json()["items"][0]["subject"]
    items = post(catalog, "records", body(catalog, subject=subject))
    assert items.status_code == 200, items.text
    assert len(items.json()["items"]) == 1
    assert "sources" not in items.text
    with server(catalog.config_path) as client:
        live = client.post(
            "/internal/v1/memory/browser/subjects",
            json=body(catalog),
            headers={"Authorization": "Bearer synthetic-platform-browser-only-credential"},
        )
        assert live.status_code == 200, live.text
        assert live.json()["items"] == subjects.json()["items"]
        assert live.headers["cache-control"] == "no-store"


@contextmanager
def deployment_process(catalog):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log_directory = catalog.directory / "diagnostics"
    log_directory.mkdir()
    catalog.config["log_directory"] = str(log_directory)
    catalog.save()
    diagnostic_contract = catalog.contracts.directory.parent.parent / "diagnostics" / "v1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tianshu_memory.cli",
            "--config",
            str(catalog.config_path),
            "serve",
            "--port",
            str(port),
            "--diagnostics-contract",
            str(diagnostic_contract),
        ],
        env=dict(os.environ, PYTHONUTF8="1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False)
    try:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stdout.read().decode("utf-8", errors="replace"))
            try:
                if client.get("/health/live").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("Memory deployment process did not start")
            time.sleep(0.05)
        yield client
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def test_existing_serve_runtime_exposes_catalog_without_browser_origin(catalog):
    catalog.seed()
    with deployment_process(catalog) as client:
        response = client.post(
            "/internal/v1/memory/browser/records",
            json=body(catalog),
            headers={"Authorization": "Bearer synthetic-platform-browser-only-credential"},
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        refused = client.post(
            "/internal/v1/memory/browser/records",
            json=body(catalog),
            headers={
                "Authorization": "Bearer synthetic-platform-browser-only-credential",
                "Origin": "https://untrusted.example",
            },
        )
        assert refused.status_code == 400
        assert refused.json()["code"] == "browser_origin_refused"


def test_runtime_role_browser_requires_exact_grant_and_template(catalog, tmp_path):
    first, _, _ = catalog.seed(0)
    second, _, _ = catalog.seed(1)
    assert catalog.scope(0)["person_id"] == catalog.scope(1)["person_id"]
    assert catalog.scope(0)["conversation_id"] == catalog.scope(1)["conversation_id"]
    path = tmp_path / "browser-role-grants.sqlite"
    catalog.config["role_grants_database_path"] = str(path)
    catalog.config["callers"]["platform"]["allowed_actors"] = ["actor:a"]
    catalog.config["callers"]["platform"]["allow_runtime_roles"] = True
    reader = catalog.config["browser_readers"]["platform"]
    reader["allow_runtime_roles"] = True
    context = copy.deepcopy(catalog.contexts["synthetic-browser"])
    context.update(assertion_ref="synthetic-browser-b", allowed_scope=catalog.scope(1))
    catalog.contexts["synthetic-browser-b"] = context
    catalog.save()
    grants = RoleGrants(path)
    grants.apply(
        {
            "request_id": "browser-b-on",
            "actor_id": "actor:b",
            "expected_version": 0,
            "enabled": True,
            "legacy": False,
        },
        {"actor:a"},
    )
    auth = Authenticator(catalog.config_path, catalog.contracts, catalog.service.clock)
    original = catalog.client
    with TestClient(create_app(service=catalog.service, auth=auth)) as client:
        catalog.client = client
        try:
            b = body(catalog, limit=20, cursor=None)
            b["origin"]["assertion_ref"] = "synthetic-browser-b"
            b["scope"] = catalog.scope(1)
            a_groups = post(catalog, "records", body(catalog, limit=20, cursor=None))
            b_groups = post(catalog, "records", b)
            assert a_groups.status_code == 200 and b_groups.status_code == 200
            assert [g["semantic_group_id"] for g in a_groups.json()["items"]] == first["group_ids"]
            assert [g["semantic_group_id"] for g in b_groups.json()["items"]] == second["group_ids"]
            assert post(catalog, "overview").json()["memory_group_count"] == 1
            assert (
                post(
                    catalog,
                    "overview",
                    {k: b[k] for k in ("schema_version", "request_id", "origin", "scope")},
                ).json()["memory_group_count"]
                == 1
            )
            forged = copy.deepcopy(b)
            forged["origin"]["assertion_ref"] = "synthetic-browser"
            assert_failed(post(catalog, "records", forged), 403, "forbidden")
            reader["allow_runtime_roles"] = False
            catalog.save()
            assert_failed(post(catalog, "records", b), 403, "forbidden")
            reader["allow_runtime_roles"] = True
            catalog.save()
            catalog.config["callers"]["platform"]["allow_runtime_roles"] = False
            catalog.save()
            assert_failed(post(catalog, "records", b), 403, "forbidden")
            catalog.config["callers"]["platform"]["allow_runtime_roles"] = True
            catalog.save()
            wrong_conversation = copy.deepcopy(b)
            wrong_conversation["scope"]["conversation_id"] = "conversation:other"
            catalog.contexts["synthetic-browser-b"]["allowed_scope"] = wrong_conversation["scope"]
            assert_failed(post(catalog, "records", wrong_conversation), 403, "forbidden")
            catalog.contexts["synthetic-browser-b"]["allowed_scope"] = catalog.scope(1)
            grants.apply(
                {
                    "request_id": "browser-b-off",
                    "actor_id": "actor:b",
                    "expected_version": 1,
                    "enabled": False,
                    "legacy": False,
                },
                {"actor:a"},
            )
            assert_failed(post(catalog, "records", b), 403, "forbidden")
            assert post(catalog, "records", body(catalog, limit=20, cursor=None)).status_code == 200
            catalog.config["callers"]["platform"]["operations"] = []
            catalog.save()
            assert_failed(
                post(catalog, "records", body(catalog, limit=20, cursor=None)), 403, "forbidden"
            )
        finally:
            catalog.client = original
