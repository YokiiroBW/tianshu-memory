"""Explicit Platform delegation over the existing browser and source checkpoints."""

import copy

import pytest
from fastapi.testclient import TestClient
from test_browser_catalog import body, post
from test_browser_catalog import catalog as catalog
from test_browser_extensions import bootstrap, committed_topic, topic_request
from test_segments import forget_original, front
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.app import create_app
from tianshu_memory.auth import Authenticator
from tianshu_memory.role_grants import RoleGrants


def added_group(h, *, actor="actor:a"):
    target = dict(h.scope(), actor_id=actor, audience="group", conversation_id="group:new")
    h.contexts["synthetic-browser"]["allowed_scope"] = copy.deepcopy(target)
    h.config["browser_readers"]["platform"]["allow_runtime_groups"] = True
    h.save()
    return body(h, scope=target, limit=20, cursor=None)


@pytest.mark.parametrize("operation", ["records", "topics"])
def test_dynamic_group_reads_actual_records_and_topics_and_respects_forget(sync, operation):
    if sync.scope()["audience"] != "group":
        pytest.skip("Dynamic delegation is group-only")
    bootstrap(sync)
    original, _ = front(sync, "We discussed a sourced walk.", "dynamic:group")
    _, receipt = committed_topic(sync, [original], "segment:dynamic")
    reader = sync.config["browser_readers"]["platform"]
    # The real operator's registered template is private. Only Platform's
    # independently resolved group origin can introduce the actual group CID.
    reader["scopes"] = [
        dict(sync.scope(), audience="self_private", conversation_id="private:operator")
    ]
    sync.save()
    request = topic_request(sync) if operation == "topics" else body(sync, limit=20, cursor=None)
    assert post(sync, operation, request).status_code == 403
    reader["allow_runtime_groups"] = True
    sync.save()
    selected = post(sync, operation, request)
    assert selected.status_code == 200, selected.text
    assert selected.json()["scope"] == sync.scope()
    assert selected.json()["items"]
    if operation == "records":
        expected = {
            ref["record_id"] for result in receipt["results"] for ref in result["memory_refs"]
        }
        assert {
            unit["record_id"] for group in selected.json()["items"] for unit in group["units"]
        } == expected
    else:
        assert selected.json()["items"][0]["segment_id"] == "segment:dynamic"
    forget_original(sync, original, 0)
    forgotten = post(sync, operation, request)
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["items"] == []


@pytest.mark.parametrize(
    "change",
    [
        "missing_flag",
        "false_flag",
        "truthy_flag",
        "private",
        "person",
        "account",
        "actor",
        "template_person",
        "template_actor",
        "origin_scope",
    ],
)
def test_dynamic_group_never_delegates_another_identity_actor_or_private_scope(catalog, change):
    request = added_group(catalog)
    reader = catalog.config["browser_readers"]["platform"]
    context = catalog.contexts["synthetic-browser"]
    if change == "missing_flag":
        del reader["allow_runtime_groups"]
    elif change == "false_flag":
        reader["allow_runtime_groups"] = False
    elif change == "truthy_flag":
        reader["allow_runtime_groups"] = "true"
    elif change == "private":
        request["scope"]["audience"] = "self_private"
        context["allowed_scope"] = copy.deepcopy(request["scope"])
    elif change == "person":
        request["scope"]["person_id"] = "person:someone-else"
        context["allowed_scope"] = copy.deepcopy(request["scope"])
    elif change == "account":
        context["verified_account"] = dict(namespace="web", immutable_account_id="someone-else")
    elif change == "actor":
        request["scope"]["actor_id"] = "actor:b"
        context["allowed_scope"] = copy.deepcopy(request["scope"])
    elif change == "template_person":
        reader["scopes"][0]["person_id"] = "person:someone-else"
    elif change == "template_actor":
        reader["scopes"][0]["actor_id"] = "actor:b"
    else:
        context["allowed_scope"]["conversation_id"] = "group:another"
    catalog.save()
    response = post(catalog, "records", request)
    assert response.status_code in {400, 403}, response.text
    assert "items" not in response.json()


def test_dynamic_group_runtime_actor_keeps_both_flags_and_live_grant(catalog, tmp_path):
    request = added_group(catalog, actor="actor:b")
    path = tmp_path / "dynamic-role-grants.sqlite"
    reader = catalog.config["browser_readers"]["platform"]
    caller = catalog.config["callers"]["platform"]
    reader["allow_runtime_roles"] = True
    caller.update(allow_runtime_roles=True, allowed_actors=["actor:a"])
    catalog.config["role_grants_database_path"] = str(path)
    catalog.save()
    grants = RoleGrants(path)
    grants.apply(
        dict(
            request_id="runtime:on",
            actor_id="actor:b",
            expected_version=0,
            enabled=True,
            legacy=False,
        ),
        {"actor:a"},
    )
    auth = Authenticator(catalog.config_path, catalog.contracts, catalog.service.clock)
    previous = catalog.client
    with TestClient(create_app(service=catalog.service, auth=auth)) as client:
        catalog.client = client
        try:
            accepted = post(catalog, "records", request)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["scope"] == request["scope"]
            for entry in (reader, caller):
                entry["allow_runtime_roles"] = False
                catalog.save()
                assert post(catalog, "records", request).status_code == 403
                entry["allow_runtime_roles"] = True
            catalog.save()
            grants.apply(
                dict(
                    request_id="runtime:off",
                    actor_id="actor:b",
                    expected_version=1,
                    enabled=False,
                    legacy=False,
                ),
                {"actor:a"},
            )
            assert post(catalog, "records", request).status_code == 403
        finally:
            catalog.client = previous


def test_dynamic_group_delegation_is_rechecked_after_remote_barrier(catalog):
    request = added_group(catalog)
    changed = False

    def revoke(kind, _, response):
        nonlocal changed
        if kind == "current" and not changed:
            changed = True
            catalog.config["browser_readers"]["platform"]["allow_runtime_groups"] = False
            catalog.save()

    catalog.mutate = revoke
    response = post(catalog, "records", request)
    assert changed
    assert response.status_code == 403, response.text
    assert "items" not in response.json()
