"""Actual standalone Knowledge CLI process, with no Memory identity DB mounted."""

import copy
import hashlib
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

from tianshu_memory.domain import canonical
from tianshu_memory.knowledge_content_migration import migrate as migrate_content
from tianshu_memory.knowledge_migration import migrate as migrate_knowledge
from tianshu_memory.store import Store

PATH = "/internal/v1/knowledge/content/"
ROLE = "/internal/v1/role-runtime/authorize"


def independent(h):
    config = copy.deepcopy(h.config)
    store = Store(h.directory / "independent-knowledge.sqlite")
    store.migrate_profiles(h.directory / "knowledge-before-profiles.sqlite")
    h.contracts.load_sources()
    store.migrate_sources(h.directory / "knowledge-before-sources.sqlite", h.contracts)
    migrate_knowledge(store, h.directory / "knowledge-before-project.sqlite")
    migrate_content(store, h.directory / "knowledge-before-content.sqlite")
    config["database_path"] = str(store.path)
    config["role_grants_database_path"] = str(h.directory / "knowledge-roles.sqlite")
    config["knowledge"] = {
        "projects": {},
        "clients": {
            "legacy": {
                "credential_sha256": hashlib.sha256(b"legacy-test-secret").hexdigest(),
                "projects": [],
                "permissions": ["query"],
            }
        },
    }
    config["callers"]["companion"].update(
        operations=[
            "content_" + op
            for op in ("uploads", "upload_status", "acquire", "read", "original", "access")
        ],
        runtime_content=True,
        allow_runtime_roles=True,
    )
    config["callers"]["platform"] = {"token": "platform-knowledge-test-secret", "role_admin": True}
    logs = h.directory / "knowledge-logs"
    logs.mkdir()
    config["log_directory"] = str(logs)
    path = h.directory / "knowledge-config.json"
    path.write_text(canonical(config), encoding="utf-8")
    return path, config, store


@contextmanager
def process(path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tianshu_memory.knowledge_cli",
            "--config",
            str(path),
            "serve",
            "--client",
            "legacy",
            "--port",
            str(port),
        ],
        env=dict(os.environ, PYTHONUTF8="1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=5,
        trust_env=False,
        headers={"Authorization": "Bearer test-only-companion-secret"},
    ) as client:
        try:
            deadline = time.monotonic() + 15
            while True:
                if worker.poll() is not None:
                    raise AssertionError(worker.stdout.read().decode("utf-8", errors="replace"))
                try:
                    if client.get("/health/live").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "Knowledge CLI did not listen"
                time.sleep(0.05)
            yield client
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
            worker.stdout.close()


def user(h, origin="origin-private", scope=None):
    return {"kind": "user", "query": h.query(origin), "scope": scope or h.private}


def post(client, operation, body):
    answer = client.post(PATH + operation, json=body)
    assert answer.status_code == 200, answer.text
    assert answer.headers["cache-control"] == "no-store"
    return answer.json()


def acquire(client, principal, raw=b"Independent Knowledge actual original.\n"):
    upload = post(
        client,
        "uploads",
        {
            "principal": principal,
            "filename": "entry.txt",
            "media_type": "text/plain",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    header = {"Content-Type": "application/octet-stream"}
    if principal["kind"] == "user":
        header["X-Tianshu-Assertion-Ref"] = principal["query"]["origin"]["assertion_ref"]
    else:
        header.update(
            {
                "X-Tianshu-Actor-Id": principal["actor_id"],
                "X-Tianshu-Operation-Ref": principal["operation_ref"],
            }
        )
    answer = client.put(PATH + "uploads/" + upload["upload_id"], content=raw, headers=header)
    assert answer.status_code == 200, answer.text
    answer = post(
        client,
        "acquire",
        {
            "principal": principal,
            "source": {"kind": "upload", "upload_id": upload["upload_id"]},
            "purpose": "save",
        },
    )
    return answer["content_ref"]


def test_standalone_first_upload_exact_scope_grants_and_revoke_without_local_people(h):
    path, config, store = independent(h)
    other_account = {"namespace": "qq", "immutable_account_id": "10002"}
    h.add_origin(
        "origin-other-first", dict(h.private, person_id=None, conversation_id=None), other_account
    )
    h.save_config()
    registered = h.post(
        "identity/register", {"command": h.command("origin-other-first"), "account": other_account}
    )
    assert registered.status_code == 200
    other_scope = dict(
        h.private, person_id=registered.json()["person_id"], conversation_id="conversation-other"
    )
    h.add_origin("origin-other", other_scope, other_account)
    config["origins"] = copy.deepcopy(h.config["origins"])
    path.write_text(canonical(config), encoding="utf-8")
    with process(path) as client:
        assert client.get("/health").status_code == 200
        # The legacy action remains registered, with its original fixed-client authorization.
        legacy = client.post(
            "/local/v1/project-knowledge/action",
            json={
                "operation": "query",
                "project_id": "absent",
                "arguments": {"text": "original", "budget_bytes": 1000},
            },
            headers={"Authorization": "Bearer legacy-test-secret"},
        )
        assert legacy.status_code == 422 and legacy.json()["code"] != "not_found"
        ref = acquire(client, user(h))
        original = client.post(
            PATH + "original", json={"principal": user(h), "content_ref": ref, "range": None}
        )
        assert (
            original.status_code == 200
            and original.content == b"Independent Knowledge actual original.\n"
        )
        assert original.headers["x-content-sha256"] == ref["sha256"]
        for headers in ({"Host": "unregistered.invalid"}, {"Origin": "https://browser.invalid"}):
            assert (
                client.post(
                    PATH + "original",
                    json={"principal": user(h), "content_ref": ref, "range": None},
                    headers=headers,
                ).status_code
                == 400
            )
        phantom = dict(
            h.private, person_id="person-fabricated", conversation_id="fake-conversation"
        )
        post(
            client,
            "access",
            {
                "principal": user(h),
                "content_ref": ref,
                "action": "grant",
                "reader_scope": phantom,
                "expected_access_version": 1,
            },
        )
        forged = client.post(
            PATH + "original",
            json={"principal": user(h, scope=phantom), "content_ref": ref, "range": None},
        )
        assert forged.status_code == 403
        assert (
            client.post(
                PATH + "original",
                json={
                    "principal": user(h, "origin-other", other_scope),
                    "content_ref": ref,
                    "range": None,
                },
            ).status_code
            == 403
        )
        post(
            client,
            "access",
            {
                "principal": user(h),
                "content_ref": ref,
                "action": "grant",
                "reader_scope": other_scope,
                "expected_access_version": 2,
            },
        )
        assert (
            client.post(
                PATH + "original",
                json={
                    "principal": user(h, "origin-other", other_scope),
                    "content_ref": ref,
                    "range": None,
                },
            ).status_code
            == 200
        )
        post(
            client,
            "access",
            {
                "principal": user(h),
                "content_ref": ref,
                "action": "revoke",
                "reader_scope": other_scope,
                "expected_access_version": 3,
            },
        )
        assert (
            client.post(
                PATH + "original",
                json={
                    "principal": user(h, "origin-other", other_scope),
                    "content_ref": ref,
                    "range": None,
                },
            ).status_code
            == 403
        )
        config["origins"]["origin-private"]["revoked"] = True
        path.write_text(canonical(config), encoding="utf-8")
        assert (
            client.post(
                PATH + "original", json={"principal": user(h), "content_ref": ref, "range": None}
            ).status_code
            == 403
        )
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_standalone_dynamic_actor_grants_restart_and_disable(h):
    path, config, store = independent(h)
    actor = {
        "kind": "actor",
        "request_id": "actual-activity-1",
        "actor_id": "actor:independent",
        "operation_ref": "activity:one",
    }
    admin = {"Authorization": "Bearer platform-knowledge-test-secret"}
    with process(path) as client:
        assert (
            client.post(
                ROLE, json={"operation": "status", "actor_id": actor["actor_id"]}, headers=admin
            ).json()["version"]
            == 0
        )
        assert (
            client.post(
                ROLE,
                json={
                    "request_id": "enable-actor",
                    "actor_id": actor["actor_id"],
                    "expected_version": 0,
                    "enabled": True,
                    "legacy": False,
                },
                headers=admin,
            ).status_code
            == 200
        )
        ref = acquire(client, actor)
        assert (
            client.post(
                ROLE, json={"operation": "status", "actor_id": actor["actor_id"]}
            ).status_code
            == 403
        )
    with process(path) as client:
        read = post(
            client,
            "read",
            {
                "principal": actor,
                "content_ref": ref,
                "range": {"unit": "characters", "start": 0, "end": ref["coverage"]["end"]},
                "budget_bytes": 8192,
            },
        )
        assert "actual original" in read["text"] and read["complete"]
        assert (
            client.post(
                ROLE,
                json={
                    "request_id": "disable-actor",
                    "actor_id": actor["actor_id"],
                    "expected_version": 1,
                    "enabled": False,
                    "legacy": False,
                },
                headers=admin,
            ).status_code
            == 200
        )
        assert (
            client.post(
                PATH + "original", json={"principal": actor, "content_ref": ref, "range": None}
            ).status_code
            == 403
        )
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
