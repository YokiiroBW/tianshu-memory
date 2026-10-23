"""One complete source boundary, with explicit optional proof outcomes."""

import copy
import sqlite3
import ssl
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_candidate_extraction import PATH
from test_group_context import owner
from test_memory_v2 import candidate, commit, fact, post, query, request, setup
from test_memory_v2_boundaries import attach, source_calls, verify
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import auth
from tianshu_memory.domain import Fault, parse_time, utc
from tianshu_memory.memory_context import preparation, read_v2
from tianshu_memory.memory_reindex import MemoryReindexer


def checks(h, bundle):
    header = request(h, "咖啡")["query"]
    base = dict(query=header, requested_scope=h.scope(), include_associated=False)
    selected = dict(
        base,
        query_text="context validity",
        selection=["identity", "evidence"],
        known_scope_version=bundle["scope_version"],
        budget=dict(tokens=0, bytes=0),
        time_range=None,
        limit=32,
        known_association_version=bundle["association_version"],
        known_scope_checks=bundle["scope_checks"],
    )
    result = [
        dict(
            check_id="check:select",
            kind="select",
            request=selected,
            expected={
                key: bundle[key]
                for key in (
                    "effective_scope",
                    "scope_version",
                    "association_version",
                    "scope_checks",
                )
            },
        )
    ]
    for kind in ("history", "input_history"):
        snapshot = bundle["history_snapshot" if kind == "history" else "input_history_snapshot"]
        result.append(
            dict(
                check_id="check:" + kind,
                kind=kind,
                request=dict(base, expected_snapshot=None, known_snapshot=snapshot),
                expected=dict(effective_scope=h.scope(), snapshot=snapshot),
            )
        )
    profile_request = h.profile_request(h.contexts["synthetic-viewer:0"], budget=0)
    response = h.post("memory/profiles/select", profile_request)
    assert response.status_code == 200, response.text
    profile = response.json()
    profile_request["known_scope_version"] = profile["scope_version"]
    result.append(
        dict(
            check_id="check:profile",
            kind="profile",
            request=profile_request,
            expected={key: profile[key] for key in ("requester_scope", "target", "scope_version")},
        )
    )
    return result


def payload(h, bundle, catalog=(), required=()):
    return dict(
        schema_version=2,
        query=request(h, "咖啡")["query"],
        requested_scope=h.scope(),
        bundle=bundle,
        checks=list(catalog),
        required_check_ids=list(required),
    )


def seeded(h):
    setup(h)
    text = "我喜欢咖啡。"
    job, _ = candidate(h, text)
    assert commit(h, job, text, fact("咖啡"))["state"] == "completed"
    return query(h, "我喜欢什么咖啡")["bundle"]


def optional_origin(h, check, ref="optional:origin"):
    context = copy.deepcopy(h.contexts["synthetic-viewer:0"])
    context["assertion_ref"] = ref
    h.contexts[ref] = context
    check["request"]["query"] = copy.deepcopy(check["request"]["query"])
    check["request"]["query"]["origin"] = dict(assertion_ref=ref)
    return context


def valid(h, response):
    assert response.status_code == 200, response.text
    data = response.json()
    h.contracts.validate("memory-context-v2#prepare_response", data)
    assert data["state"] == "valid", data
    return data


def test_prepare_four_components_and_semantic_duplicates_share_one_boundary(sync, monkeypatch):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)
    # Different IDs and correlation IDs, but the same complete check semantics.
    for index in range(60):
        duplicate = copy.deepcopy(catalog[index % 4])
        duplicate["check_id"] = f"duplicate:{index}"
        duplicate["request"]["query"]["request_id"] = f"request:duplicate:{index}"
        catalog.append(duplicate)
    verified, selected = [], []
    component, snapshot = preparation.verify_component, read_v2.verify_snapshot

    def count_component(*args, **kwargs):
        verified.append(args[1]["kind"])
        return component(*args, **kwargs)

    def count_snapshot(*args, **kwargs):
        selected.append(True)
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(preparation, "verify_component", count_component)
    monkeypatch.setattr(read_v2, "verify_snapshot", count_snapshot)
    sync.calls.clear()
    result = valid(sync, post(sync, "prepare", payload(sync, bundle, catalog)))
    assert result["checks"] == [
        dict(check_id=x["check_id"], kind=x["kind"], status="valid") for x in catalog
    ]
    assert verified == ["select", "history", "input_history", "profile"]
    assert selected == [True]
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert len([call for call in sync.calls if call[0] == "origin"]) == 1


@pytest.mark.parametrize("required", [False, True])
def test_changed_dependency_has_exact_optional_or_required_outcome(sync, required):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)
    catalog[0]["request"]["known_scope_version"] += 1
    response = post(
        sync,
        "prepare",
        payload(sync, bundle, catalog, [catalog[0]["check_id"]] if required else []),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    sync.contracts.validate("memory-context-v2#prepare_response", result)
    if required:
        assert result["state"] == "invalid" and result["component"] == "required_check"
        assert result["check_id"] == "check:select"
        assert result["failure"]["code"] == "scope_changed"
        assert result["failure"]["execution_state"] == "not_started"
        assert "checks" not in result and "bundle" not in result
    else:
        assert result["checks"][0]["status"] == "rejected"
        assert result["checks"][0]["reason"] == "dependency_changed"
        assert all(item["status"] == "valid" for item in result["checks"][1:])


def test_retired_reply_keeps_input_only_but_source_withdrawal_rejects_both(sync):
    old = seeded(sync)
    catalog = checks(sync, old)[1:3]
    with sync.store.transaction() as db:
        sync.service._bump(db, sync.scope(), source_history_changed=False)
    fresh = query(sync, "我喜欢什么咖啡")["bundle"]
    result = valid(sync, post(sync, "prepare", payload(sync, fresh, catalog)))
    assert [item["status"] for item in result["checks"]] == ["rejected", "valid"]
    sync.grants[-1]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    response = post(sync, "prepare", payload(sync, fresh, catalog))
    assert response.status_code == 200, response.text
    assert response.json()["component"] == "current_selection"
    latest = query(sync, "我喜欢什么咖啡")["bundle"]
    result = valid(sync, post(sync, "prepare", payload(sync, latest, catalog)))
    assert [item["status"] for item in result["checks"]] == ["rejected", "rejected"]


def test_forged_negative_selection_is_typed_current_failure(sync):
    bundle = seeded(sync)
    bundle = copy.deepcopy(bundle)
    bundle["evidence_ids"] = []
    response = post(sync, "prepare", payload(sync, bundle))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "invalid"
    assert response.json()["component"] == "current_selection"
    assert response.json()["check_id"] is None
    assert "bundle" not in response.json()


def test_each_old_author_keeps_its_own_origin_and_current_viewer_scope(sync):
    seeded(sync)
    audience = sync.scope()["audience"]
    other = owner(
        sync, audience=audience, conversation=(None if audience == "group" else "private:unlinked")
    )
    candidate(sync, "我喜欢绿茶。", 2, other)
    history_request = dict(
        query=request(sync, "绿茶", actor=other)["query"],
        requested_scope=sync.scope(other),
        include_associated=False,
        expected_snapshot=None,
        known_snapshot=None,
    )
    response = sync.post("memory/context/input-history", history_request)
    assert response.status_code == 200, response.text
    old = response.json()
    history_request["known_snapshot"] = old["snapshot"]
    check = dict(
        check_id="other:history",
        kind="input_history",
        request=history_request,
        expected=dict(effective_scope=sync.scope(other), snapshot=old["snapshot"]),
    )
    wrong = copy.deepcopy(check)
    wrong["check_id"] = "other:wrong-origin"
    wrong["request"]["query"]["origin"] = dict(assertion_ref="synthetic-viewer:0")
    current = query(sync, "我喜欢什么咖啡")["bundle"]
    result = valid(sync, post(sync, "prepare", payload(sync, current, [check, wrong])))
    assert result["checks"][0]["status"] == ("valid" if audience == "group" else "rejected")
    if audience != "group":
        assert result["checks"][0]["reason"] == "dependency_changed"
    assert result["checks"][1]["status"] == "rejected"
    assert result["checks"][1]["reason"] == "origin_denied"


@pytest.mark.parametrize("denial", ["expired", "actor"])
@pytest.mark.parametrize("required", [False, True])
def test_complete_origin_denial_is_classified_without_borrowing_main_permission(
    sync, denial, required
):
    bundle = seeded(sync)
    check = checks(sync, bundle)[1]
    context = optional_origin(sync, check)
    if denial == "expired":
        context["expires_at"] = "2020-01-01T00:00:00Z"
    else:
        context["allowed_scope"]["actor_id"] = "actor:not-approved"
    response = post(
        sync, "prepare", payload(sync, bundle, [check], [check["check_id"]] if required else [])
    )
    assert response.status_code == 200, response.text
    result = response.json()
    if required:
        assert result["component"] == "required_check"
        assert result["failure"]["code"] == "forbidden"
    else:
        assert result["checks"] == [
            dict(
                check_id=check["check_id"],
                kind="history",
                status="rejected",
                reason="origin_denied",
            )
        ]


@pytest.mark.parametrize(
    "failure",
    [
        "request_id",
        "assertion_ref",
        "issuer",
        "authenticated_service",
        "audience_service",
        "bare403",
        "bare404",
        "revoked",
        "json",
        "schema",
    ],
)
@pytest.mark.parametrize("necessary", ["optional", "required", "shared_required", "main"])
def test_unproved_origin_drops_all_optional_checks_but_never_main_or_required(
    sync, monkeypatch, failure, necessary
):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:3]
    for check in catalog:
        optional_origin(sync, check)
    required = [] if necessary in {"optional", "main"} else [catalog[-1]["check_id"]]
    if necessary == "required":
        catalog = catalog[-1:]
    ref = "synthetic-viewer:0" if necessary == "main" else "optional:origin"
    verified, component = [], preparation.verify_component

    def observe(*args, **kwargs):
        verified.append(args[1]["check_id"])
        return component(*args, **kwargs)

    monkeypatch.setattr(preparation, "verify_component", observe)

    def corrupt(kind, body, response):
        if kind != "origin":
            return
        sync.status.pop("origin", None)
        sync.raw_responses.pop("origin", None)
        if body["assertion_ref"] != ref:
            return
        if failure == "request_id":
            response["request_id"] = "request:wrong"
        elif failure in {"bare403", "bare404"}:
            sync.status[kind] = int(failure[4:])
        elif failure == "json":
            sync.raw_responses[kind] = b'{"context":'
        elif failure == "schema":
            response["context"].pop("expires_at")
        elif failure == "revoked":
            # The unchanged common contract has revoked const:false. An issuer
            # cannot turn a malformed positive envelope into a typed denial.
            response["context"]["revoked"] = True
        elif failure == "issuer":
            response["context"][failure] = "nonebot"
        else:
            response["context"][failure] = "untrusted:wrong"

    sync.mutate = corrupt
    sync.calls.clear()
    response = post(sync, "prepare", payload(sync, bundle, catalog, required))
    assert verified == []  # Unproved origins never reach any legacy data selector.
    if necessary == "optional":
        result = valid(sync, response)
        assert result["checks"] == [
            dict(
                check_id=item["check_id"],
                kind=item["kind"],
                status="unavailable",
                reason="origin_unavailable",
            )
            for item in catalog
        ]
        # The unavailable origin shares this physical source with main, whose
        # complete source proof and lease must remain independently intact.
        assert source_calls(sync) == ["snapshot", "current", "head"]
        assert result["bundle"]["bundle_id"] == bundle["bundle_id"]
        assert result["valid_until"] == result["bundle"]["valid_until"]
    else:
        expected = {"json": 503, "revoked": 400, "schema": 400}.get(failure, 403)
        assert response.status_code == expected, response.text
        assert "component" not in response.json() and "checks" not in response.json()
        assert source_calls(sync) == []


def interrupt_origin(monkeypatch, ref, error):
    original = httpx.Client.stream

    def stream(client, method, url, **kwargs):
        if kwargs.get("json", {}).get("assertion_ref") == ref:
            raise error
        return original(client, method, url, **kwargs)

    monkeypatch.setattr(httpx.Client, "stream", stream)


@pytest.mark.parametrize("necessary", ["optional", "required", "shared_required", "main"])
def test_complete_origin_body_with_invalid_utf8_is_always_a_whole_fault(sync, necessary):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:3]
    for check in catalog:
        optional_origin(sync, check)
    required = [] if necessary in {"optional", "main"} else [catalog[-1]["check_id"]]
    if necessary == "required":
        catalog = catalog[-1:]
    ref = "synthetic-viewer:0" if necessary == "main" else "optional:origin"

    def corrupt(kind, body, response):
        if kind == "origin":
            sync.raw_responses.pop("origin", None)
            if body["assertion_ref"] == ref:
                # A complete response with its actual Content-Length, delivered
                # over loopback HTTPS. Invalid wire encoding is not JSON syntax.
                sync.raw_responses[kind] = b'{"context":"\xff"}'

    sync.mutate = corrupt
    sync.calls.clear()
    response = post(sync, "prepare", payload(sync, bundle, catalog, required))
    assert response.status_code == 503, response.text
    assert "checks" not in response.json() and "component" not in response.json()
    assert source_calls(sync) == []


@pytest.mark.parametrize("necessary", ["optional", "required", "shared_required", "main"])
def test_only_independent_optional_origin_transport_can_be_unavailable(
    sync, monkeypatch, necessary
):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:3]
    for check in catalog:
        optional_origin(sync, check)
    required = [] if necessary in {"optional", "main"} else [catalog[-1]["check_id"]]
    ref = "synthetic-viewer:0" if necessary == "main" else "optional:origin"
    if necessary == "required":
        catalog = catalog[-1:]
    interrupt_origin(monkeypatch, ref, httpx.ReadTimeout("synthetic transport timeout"))
    sync.calls.clear()
    response = post(sync, "prepare", payload(sync, bundle, catalog, required))
    if necessary == "optional":
        result = valid(sync, response)
        assert [item["status"] for item in result["checks"]] == ["unavailable", "unavailable"]
        assert all(item["reason"] == "origin_unavailable" for item in result["checks"])
        # Main evidence still has exactly its full source proof, despite a
        # second, unavailable origin referencing the same physical source.
        assert source_calls(sync) == ["snapshot", "current", "head"]
        assert result["bundle"]["bundle_id"] == bundle["bundle_id"]
    else:
        assert response.status_code == 503, response.text
        assert "component" not in response.json()


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("bad framing"),
        httpx.ConnectError("certificate failure"),
        httpx.PoolTimeout("shared pool"),
    ],
)
def test_optional_protocol_or_shared_connection_unknown_remains_whole_fault(
    sync, monkeypatch, error
):
    bundle = seeded(sync)
    check = checks(sync, bundle)[1]
    optional_origin(sync, check)
    interrupt_origin(monkeypatch, "optional:origin", error)
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    assert response.status_code == 503, response.text
    assert "component" not in response.json()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_optional_shared_issuer_or_unclassified_http_failure_remains_whole_fault(sync, status):
    bundle = seeded(sync)
    check = checks(sync, bundle)[1]
    optional_origin(sync, check)

    def fail_optional(kind, body, response):
        if kind == "origin" and body["assertion_ref"] == "optional:origin":
            sync.status[kind] = status

    sync.mutate = fail_optional
    sync.calls.clear()
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    assert response.status_code == (403 if status == 401 else 503), response.text
    assert "checks" not in response.json()
    assert source_calls(sync) == []


@pytest.mark.parametrize("failure", ["role_grants", "contracts"])
def test_local_authority_storage_or_contract_registry_failure_is_not_origin_unavailable(
    sync, monkeypatch, failure
):
    bundle = seeded(sync)
    check = checks(sync, bundle)[1]
    optional_origin(sync, check)
    if failure == "role_grants":
        decisions = []

        def decision(actor):
            decisions.append(actor)
            if len(decisions) == 2:
                raise sqlite3.OperationalError("synthetic local grants unavailable")
            return None

        monkeypatch.setattr(
            sync.client.app.state.auth, "role_grants", SimpleNamespace(decision=decision)
        )
    else:
        original = sync.service.contracts.validate

        def validate(name, document):
            if (
                name == "common#origin_resolve_response"
                and document["context"]["assertion_ref"] == "optional:origin"
            ):
                raise ValueError("synthetic contract registry unavailable")
            return original(name, document)

        monkeypatch.setattr(sync.service.contracts, "validate", validate)
    sync.calls.clear()
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    assert response.status_code == (503 if failure == "role_grants" else 400), response.text
    assert "checks" not in response.json()
    assert source_calls(sync) == []


@pytest.mark.parametrize("failure", ["barrier", "sql", "generic_scope_changed"])
def test_shared_unknown_is_never_hidden_as_optional_failure(sync, monkeypatch, failure):
    bundle = seeded(sync)
    catalog = checks(sync, bundle)[1:2]
    if failure == "barrier":
        sync.status["current"] = 503
    else:

        def fail(*args, **kwargs):
            if failure == "sql":
                raise sqlite3.OperationalError("synthetic database failure")
            raise Fault("scope_changed", 409)

        monkeypatch.setattr(sync.service.memory_context, "history", fail)
    response = post(sync, "prepare", payload(sync, bundle, catalog))
    assert response.status_code == (409 if failure == "generic_scope_changed" else 503), (
        response.text
    )
    assert "component" not in response.json()


@pytest.mark.parametrize("expired", [False, True])
def test_lease_is_minimum_of_valid_origins_and_expired_optional_is_not_adopted(
    sync, monkeypatch, expired
):
    bundle = seeded(sync)
    check = checks(sync, bundle)[1]
    context = optional_origin(sync, check)
    now = sync.service.clock()
    context["expires_at"] = utc(now + timedelta(seconds=5))
    if expired:
        original = preparation.verify_component

        def advance(*args, **kwargs):
            result = original(*args, **kwargs)
            monkeypatch.setattr(sync.service, "clock", lambda: now + timedelta(seconds=6))
            return result

        monkeypatch.setattr(preparation, "verify_component", advance)
    result = valid(sync, post(sync, "prepare", payload(sync, bundle, [check])))
    if expired:
        assert result["checks"][0]["status"] == "rejected"
        assert parse_time(result["valid_until"]) > parse_time(context["expires_at"])
    else:
        assert result["valid_until"] == context["expires_at"]


@pytest.mark.parametrize("failure", ["duplicate", "missing_required", "too_many", "bytes"])
def test_catalog_and_transport_bounds_reject_instead_of_partial_result(sync, failure):
    bundle = seeded(sync)
    check = checks(sync, bundle)[1]
    data = payload(sync, bundle, [check])
    if failure == "duplicate":
        data["checks"].append(copy.deepcopy(check))
    elif failure == "missing_required":
        data["required_check_ids"] = ["missing:check"]
    elif failure == "too_many":
        data["checks"] = [dict(check, check_id=f"check:{index}") for index in range(65)]
    else:
        # Legal check count does not bypass the existing raw transport cap.
        return_response = sync.client.post(
            "/internal/v2/memory/context/prepare",
            content=b" " * 262145,
            headers={"Content-Type": "application/json"},
        )
        assert return_response.status_code == 400
        return
    response = post(sync, "prepare", data)
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("budget", [0, 100000])
def test_profiles_batch_preserves_each_original_projection_in_one_barrier(sync, budget):
    setup(sync)
    reader = sync.profile_fixture()
    requests = [sync.profile_request(reader, budget=budget) for _ in range(4)]
    prior = [sync.post("memory/profiles/select", item).json() for item in requests]
    data = dict(
        schema_version=2,
        query=requests[0]["query"],
        requested_scope=reader["allowed_scope"],
        requests=requests,
    )
    sync.calls.clear()
    response = post(sync, "profiles", data)
    assert response.status_code == 200, response.text
    result = response.json()
    sync.contracts.validate("memory-context-v2#profile_batch_response", result)
    assert [item["request_id"] for item in result["responses"]] == [
        item["query"]["request_id"] for item in requests
    ]
    for before, after in zip(prior, result["responses"], strict=True):
        assert before["selected_units"] == after["selected_units"]
        assert before["target"] == after["target"]
        assert before["budget_used"] == after["budget_used"]
    assert source_calls(sync) == ["snapshot", "current", "head"]


@pytest.mark.parametrize("failure", ["origin", "scope", "permission", "version", "barrier"])
def test_profiles_batch_has_no_partial_or_permission_bypass(sync, failure):
    setup(sync)
    reader = sync.profile_fixture()
    requests = [sync.profile_request(reader, budget=0) for _ in range(2)]
    data = dict(
        schema_version=2,
        query=copy.deepcopy(requests[0]["query"]),
        requested_scope=copy.deepcopy(reader["allowed_scope"]),
        requests=requests,
    )
    expected = 400
    if failure == "origin":
        requests[-1]["query"]["origin"] = dict(assertion_ref="synthetic-viewer:0")
    elif failure == "scope":
        requests[-1]["requester_scope"] = sync.scope()
    elif failure == "permission":
        sync.config["callers"]["companion"]["operations"].remove("select_profiles")
        sync.save()
        expected = 403
    elif failure == "version":
        requests[-1]["known_scope_version"] = 999999
        expected = 409
    else:
        sync.status["current"] = 503
        expected = 503
    response = post(sync, "profiles", data)
    assert response.status_code == expected, response.text
    assert "responses" not in response.json()


@pytest.mark.parametrize("external", [None, "embedding", "rerank"])
def test_room_empty_source_registration_does_not_change_queryable_domain_until_capture(
    sync, external
):
    if sync.scope()["audience"] != "group":
        pytest.skip("Room source discovery has multiple authors")
    bundle = seeded(sync)
    other = owner(sync)
    before = query(sync, "群里谁喜欢咖啡")
    job = None

    def register_only():
        nonlocal job
        job, _ = candidate(sync, "我带了雨伞。", 2, other, capture=False)

    if external:
        backend = attach(
            sync, "hybrid" if external == "embedding" else "keyword", external == "rerank"
        )
        if external == "embedding":
            assert MemoryReindexer(sync.service).index_step()["indexed"] == 2
        backend.hook = register_only
        after = query(sync, "群里谁喜欢咖啡")
        assert after["bundle"]["selection_constraints"] == before["bundle"]["selection_constraints"]
    else:
        register_only()
    assert job is not None
    assert verify(sync, before["bundle"]).status_code == 200
    assert verify(sync, bundle).status_code == 200
    pending = sync.post(PATH, dict(job_id=job, output=None, supported_extraction_schemas=[3, 4]))
    assert pending.status_code == 200, pending.text
    assert verify(sync, before["bundle"]).status_code == 409
    assert verify(sync, bundle).status_code == 200  # self owner domain remains independent


def test_transport_classification_requires_a_known_socket_cause():
    interrupted = httpx.ReadError("socket failed")
    interrupted.__cause__ = ConnectionResetError()
    assert auth.interrupted_transport(interrupted)
    assert not auth.interrupted_transport(httpx.ReadError("unclassified failure"))
    assert not auth.interrupted_transport(httpx.RemoteProtocolError("bad framing"))
    interrupted.__cause__.__cause__ = ssl.SSLError("bad TLS record")
    assert not auth.interrupted_transport(interrupted)
    timeout = httpx.ReadTimeout("TLS failure during read")
    timeout.__cause__ = ssl.SSLError("bad TLS record")
    assert not auth.interrupted_transport(timeout)
