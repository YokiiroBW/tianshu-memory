"""Strict request-local origin reuse preserves every adopted authorization."""

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_memory_preparation import checks, optional_origin, payload, seeded
from test_memory_v2 import post
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import Fault, utc
from tianshu_memory.memory_context import read_v2
from tianshu_memory.memory_context.http import verification_contexts


def verify_payload(h, bundle, catalog):
    result = payload(h, bundle, catalog)
    result.pop("required_check_ids")
    return result


def test_same_origin_legacy_checks_resolve_once_including_primary_and_preserve_order(sync):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)
    for index in range(8):
        item = copy.deepcopy(catalog[index % 4])
        item["check_id"] = f"same-origin:{index}"
        catalog.append(item)
    sync.calls.clear()
    response = post(sync, "verify", verify_payload(sync, bundle, catalog))
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == [
        dict(check_id=c["check_id"], kind=c["kind"], status="valid") for c in catalog
    ]
    assert len([call for call in sync.calls if call[0] == "origin"]) == 1


@pytest.mark.parametrize("failure", ["expired", "bare403", "bad_json"])
def test_distinct_origin_still_fails_the_whole_strict_verification(sync, failure):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:3]
    context = optional_origin(sync, catalog[0], ref="strict:origin")
    optional_origin(sync, catalog[1], ref="strict:origin")
    if failure == "expired":
        context["expires_at"] = "2020-01-01T00:00:00Z"
        sync.contexts["strict:origin"] = context
    else:
        original = sync.mutate

        def corrupt(kind, body, response):
            if original is not None:
                original(kind, body, response)
            if kind == "origin" and body.get("assertion_ref") == "strict:origin":
                if failure == "bare403":
                    sync.status[kind] = 403
                else:
                    sync.raw_responses[kind] = b'{"context":'

        sync.mutate = corrupt
    response = post(sync, "verify", verify_payload(sync, bundle, catalog))
    assert response.status_code == (503 if failure == "bad_json" else 403), response.text
    assert "checks" not in response.json()


def test_cached_primary_does_not_bypass_profile_permission_or_merge_different_complete_origins():
    calls = []
    auth = SimpleNamespace(resolve=lambda *args: calls.append(args) or object())
    origin = dict(assertion_ref="same")
    request = dict(
        query=dict(origin=origin, request_id="request"),
        checks=[
            dict(kind="history", request=dict(query=dict(origin=origin))),
            dict(kind="history", request=dict(query=dict(origin=dict(assertion_ref="different")))),
            dict(kind="profile", request=dict(query=dict(origin=origin))),
        ],
    )
    with pytest.raises(Fault) as denied:
        verification_contexts(
            auth, "companion", dict(operations=["context_query"]), request, object()
        )
    assert denied.value.status == 403
    assert len(calls) == 1


def test_origin_expiring_during_verification_cannot_return_the_cached_permission(sync, monkeypatch):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:2]
    now = sync.service.clock()
    context = optional_origin(sync, catalog[0], ref="strict:short")
    context["expires_at"] = utc(now + timedelta(seconds=2))
    original = read_v2.verify_legacy

    def advance(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(sync.service, "clock", lambda: now + timedelta(seconds=3))
        return result

    monkeypatch.setattr(read_v2, "verify_legacy", advance)
    response = post(sync, "verify", verify_payload(sync, bundle, catalog))
    assert response.status_code == 403, response.text
    assert "checks" not in response.json()


@pytest.mark.parametrize("shortest", ["main", "check"])
def test_verification_deadline_is_the_minimum_adopted_origin_lease(sync, shortest):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:2]
    now = sync.service.clock()
    check = optional_origin(sync, catalog[0], ref="strict:lease")
    main = sync.contexts["synthetic-viewer:0"]
    main["expires_at"] = utc(now + timedelta(seconds=3 if shortest == "main" else 8))
    check["expires_at"] = utc(now + timedelta(seconds=3 if shortest == "check" else 8))
    response = post(sync, "verify", verify_payload(sync, bundle, catalog))
    assert response.status_code == 200, response.text
    assert response.json()["valid_until"] == utc(now + timedelta(seconds=3))
