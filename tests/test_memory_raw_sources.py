"""Raw input components use the real authenticated bundle boundary and guarded store."""

import copy
import json
from datetime import timedelta

import pytest
from test_group_context import owner
from test_memory_preparation import checks, optional_origin, payload, seeded, valid
from test_memory_v2 import post, query, request, setup
from test_memory_v2_boundaries import source_calls
from test_memory_verification_contexts import verify_payload
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, fingerprint, utc
from tianshu_memory.role_grants import RoleGrants
from tianshu_memory.sources import LocalFixtureSources
from tianshu_memory.workflow import LocalWorkflow


def raw_check(h, actor=0, *, turn=None, identity="raw:input"):
    source = h.check(actor)
    if turn is not None:
        source.update(
            turn_id=turn["turn_id"],
            input_revision=turn["input_revision"],
            scope=copy.deepcopy(turn["scope"]),
            sources=copy.deepcopy(turn["input_sources"]),
        )
    return dict(
        check_id=identity,
        kind="input_sources",
        request=dict(
            query=request(h, "原文", actor=actor)["query"],
            requested_scope=h.scope(actor),
            source_check=source,
        ),
        expected={},
    )


def original_turn(h, serial, *, actor=0, count=1):
    """Install only remote owner facts; do not admit, consume or capture them in Memory."""
    sources = []
    for index in range(count):
        physical, admission, grant = [
            copy.deepcopy(items[actor]) for items in (h.physicals, h.admissions, h.grants)
        ]
        suffix = f"raw:{serial}:{index}"
        key = dict(physical["key"], message_id=suffix)
        physical.update(key=key, physical_receipt_id="physical:" + suffix)
        admission["selector"]["key"] = key
        admission["source"]["message_key"] = dict(key, revision=physical["revision"])
        admission["source"]["receipt_id"] = "admission:" + suffix
        admission["physical_receipt_id"] = physical["physical_receipt_id"]
        grant.update(selector=admission["selector"], entry_id="grant:" + suffix)
        h.physicals.append(physical)
        h.admissions.append(admission)
        h.grants.append(grant)
        sources.append(copy.deepcopy(admission["source"]))
    original = h.turns[actor]
    turn = json.loads(canonical(original).replace(original["turn_id"], f"turn:raw:{serial}"))
    turn["input_sources"] = sources
    turn["committed_event"].update(event_id=f"event:raw:{serial}", sources=sources)
    h.turns.append(turn)
    h.core_head["sequence"] += 1
    h.platform_head["sequence"] += 1
    return turn


def check_request(h, bundle, catalog, phase="prepare", required=()):
    return (
        payload(h, bundle, catalog, required)
        if phase == "prepare"
        else verify_payload(h, bundle, catalog)
    )


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_never_admitted_raw_source_is_explicitly_observed_and_checked(sync, phase):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    turn = original_turn(sync, "cold")
    check = raw_check(sync, turn=turn)
    key = sync.service.source_identity(turn["input_sources"][0], sync.scope())
    with sync.store.transaction() as db:
        assert db.execute("SELECT 1 FROM source_admissions WHERE key=?", (key,)).fetchone() is None
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, [check], phase))
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == [
        dict(check_id="raw:input", kind="input_sources", status="valid")
    ]
    assert source_calls(sync) == ["snapshot", "current", "head"]
    observed = next(body for kind, body, _ in sync.calls if kind == "snapshot")
    assert observed["turn_ids"] == [turn["turn_id"]]
    assert observed["selectors"] == [sync.admissions[-1]["selector"]]
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT verified FROM source_admissions WHERE key=?", (key,)).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("phase", ["prepare", "verify", "standalone"])
def test_unsynchronized_active_local_source_cannot_prove_a_raw_turn(sync, monkeypatch, phase):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    check = raw_check(sync)
    monkeypatch.setattr(sync.service, "source_authority", LocalFixtureSources())
    source = check["request"]["source_check"]["sources"][0]
    LocalWorkflow(sync.service).observe_source(source, sync.scope())
    assert not sync.service.synchronized
    with sync.store.transaction() as db:
        rows = sync.service._source_rows(db, [source], sync.scope())
        assert len(rows) == 1 and rows[0]["state"] == "active"
    sync.calls.clear()
    response = (
        sync.post("memory/source-sync/check", check["request"]["source_check"])
        if phase == "standalone"
        else post(sync, phase, check_request(sync, bundle, [check], phase))
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "dependency_unavailable"
    assert "checks" not in response.json() and "component" not in response.json()
    assert source_calls(sync) == []


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_multiple_real_owners_and_old_four_kinds_share_one_complete_barrier(sync, phase):
    if sync.scope()["audience"] != "group":
        pytest.skip("Different people share only the current group domain")
    bundle = seeded(sync)
    actors = [0, owner(sync), owner(sync), owner(sync)]
    raw = [
        raw_check(
            sync,
            actor,
            turn=original_turn(sync, f"owner-{actor}", actor=actor),
            identity=f"raw:{actor}",
        )
        for actor in actors
    ]
    bundle = query(sync, "咖啡")["bundle"]
    catalog = checks(sync, bundle) + raw
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, catalog, phase))
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == [
        dict(check_id=item["check_id"], kind=item["kind"], status="valid") for item in catalog
    ]
    assert source_calls(sync) == ["snapshot", "current", "head"]
    origins = [body["assertion_ref"] for kind, body, _ in sync.calls if kind == "origin"]
    assert set(origins) == {f"synthetic-viewer:{actor}" for actor in actors}
    assert len(origins) == 4
    observed = next(body for kind, body, _ in sync.calls if kind == "snapshot")
    assert set(observed["turn_ids"]) == {item["request"]["source_check"]["turn_id"] for item in raw}


def assert_raw_pages(sync, phase, invalid_last, sources_per_turn):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    catalog = [
        raw_check(
            sync, turn=original_turn(sync, index, count=sources_per_turn), identity=f"raw:{index}"
        )
        for index in range(33)
    ]
    if invalid_last:
        catalog[-1]["request"]["source_check"]["input_revision"] += 1
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, catalog, phase))
    if invalid_last:
        assert response.status_code == 503, response.text
        assert "checks" not in response.json()
    else:
        assert response.status_code == 200, response.text
        assert len(response.json()["checks"]) == 33
        assert all(item["status"] == "valid" for item in response.json()["checks"])
    observed = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    assert [len(body["turn_ids"]) for body in observed] == [32, 1]
    assert [len(body["selectors"]) for body in observed] == (
        [256, 41] if sources_per_turn == 9 else [33, 0]
    )
    assert {turn for body in observed for turn in body["turn_ids"]} == {
        item["request"]["source_check"]["turn_id"] for item in catalog
    }
    expected = 33 * sources_per_turn
    assert len({canonical(ref) for body in observed for ref in body["selectors"]}) == expected
    assert source_calls(sync) == ["snapshot", "current", "snapshot", "current", "current", "head"]
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM source_admissions WHERE verified=1").fetchone()[0]
            == expected
        )


@pytest.mark.parametrize("phase", ["prepare", "verify"])
@pytest.mark.parametrize("invalid_last", [False, True])
def test_all_raw_turns_and_sources_survive_32_turn_and_256_selector_pages(
    sync, phase, invalid_last
):
    assert_raw_pages(sync, phase, invalid_last, 9)


@pytest.mark.parametrize("phase", ["prepare", "verify"])
@pytest.mark.parametrize("invalid_last", [False, True])
def test_turn_only_tail_keeps_all_raw_inputs_and_relationships(sync, phase, invalid_last):
    assert_raw_pages(sync, phase, invalid_last, 1)


@pytest.mark.parametrize("change", ["owner_head", "local_revision"])
def test_raw_pages_preserve_same_head_no_prefix_and_local_revision_retry(sync, change):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    catalog = [
        raw_check(sync, turn=original_turn(sync, index, count=9), identity=f"raw:{index}")
        for index in range(33)
    ]
    changed = False

    def concurrent(kind, body, response):
        nonlocal changed
        if change == "owner_head" and kind == "snapshot":
            sync.core_head["sequence"] += 1
        elif (
            change == "local_revision"
            and kind == "current"
            and not body["admissions"]
            and not changed
        ):
            changed = True
            with sync.store.transaction() as db:
                db.execute("INSERT INTO people VALUES ('person:concurrent-raw-check')")

    sync.mutate = concurrent
    sync.calls.clear()
    response = post(sync, "prepare", payload(sync, bundle, catalog))
    observed = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    if change == "owner_head":
        assert response.status_code == 503, response.text
        assert len(observed) == 6
        expected = 0
    else:
        assert valid(sync, response)["checks"][-1]["status"] == "valid"
        assert len(observed) == 4
        expected = 297
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM source_admissions").fetchone()[0] == expected


@pytest.mark.parametrize(
    "forgery", ["scope", "turn", "revision", "receipt", "order", "missing_ref"]
)
def test_raw_request_keeps_exact_scope_turn_revision_and_ordered_refs(sync, forgery):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    check = raw_check(sync, turn=original_turn(sync, "exact", count=2))
    original = check["request"]["source_check"]
    if forgery == "scope":
        original["scope"]["person_id"] = "person:forged"
    elif forgery == "turn":
        original["turn_id"] = sync.turns[1]["turn_id"]
    elif forgery == "revision":
        original["input_revision"] += 1
    elif forgery == "receipt":
        original["sources"][0]["receipt_id"] = "admission:forged"
    elif forgery == "order":
        original["sources"].reverse()
    else:
        original["sources"].pop()
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    assert response.status_code == (400 if forgery == "scope" else 503), response.text
    assert "checks" not in response.json() and "component" not in response.json()


@pytest.mark.parametrize("permission", ["operation", "event_scope", "service"])
@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_context_permission_cannot_replace_original_source_publisher_gate(sync, permission, phase):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    check = raw_check(sync)
    caller = sync.config["callers"]["companion"]
    if permission == "operation":
        caller["operations"].remove("check_sources")
    elif permission == "event_scope":
        caller["event_scopes"] = []
    else:
        sync.config["callers"]["reader"] = sync.config["callers"].pop("companion")
        for context in sync.contexts.values():
            context["authenticated_service"] = "reader"
    sync.save()
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, [check], phase))
    assert response.status_code == 403, response.text
    assert "checks" not in response.json()
    assert source_calls(sync) == []


@pytest.mark.parametrize("enabled", [True, False])
def test_raw_source_publisher_uses_live_role_grants_and_revocation(sync, enabled):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    check = raw_check(sync)
    grants = RoleGrants(sync.directory / "raw-role-grants.sqlite")
    actor = sync.scope()["actor_id"]
    grants.apply(
        dict(
            request_id="raw:role", actor_id=actor, expected_version=0, enabled=enabled, legacy=False
        ),
        set(),
    )
    sync.client.app.state.auth.role_grants = grants
    caller = sync.config["callers"]["companion"]
    caller.update(event_scopes=[], allowed_actors=[], allow_runtime_roles=True)
    sync.save()
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    if enabled:
        assert valid(sync, response)["checks"][0]["status"] == "valid"
    else:
        assert response.status_code == 403, response.text
        assert "checks" not in response.json()


@pytest.mark.parametrize("mode", ["optional", "required", "strict"])
def test_real_memory_forget_with_active_core_source_is_a_known_raw_negative(sync, mode):
    setup(sync)
    stored, _, _ = sync.seed()
    check = raw_check(sync)
    revision = sync.revision(stored["record_ids"][0])
    forgotten = sync.post("memory/revise", revision)
    assert forgotten.status_code == 200, forgotten.text
    assert sync.physicals[0]["state"] == "active"
    bundle = query(sync, "原文")["bundle"]
    phase = "verify" if mode == "strict" else "prepare"
    required = [check["check_id"]] if mode == "required" else []
    response = post(sync, phase, check_request(sync, bundle, [check], phase, required))
    if mode == "strict":
        assert response.status_code == 409 and response.json()["code"] == "scope_changed", (
            response.text
        )
        assert "checks" not in response.json()
    elif mode == "required":
        assert response.status_code == 200, response.text
        assert response.json()["component"] == "required_check"
        assert response.json()["check_id"] == check["check_id"]
        assert response.json()["failure"]["code"] == "scope_changed"
    else:
        assert valid(sync, response)["checks"] == [
            dict(
                check_id=check["check_id"],
                kind="input_sources",
                status="rejected",
                reason="dependency_changed",
            )
        ]
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["withdrawn", "transport"])
def test_owner_negative_is_durable_but_owner_or_shared_failure_remains_global(sync, failure):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    check = raw_check(sync)
    if failure == "withdrawn":
        sync.physicals[0].update(
            revision=2,
            state="withdrawn",
            kind="retract",
            physical_receipt_id="raw:physical:retracted",
            content_digest="b" * 64,
        )
        sync.core_head["sequence"] += 1
    else:
        sync.status["current"] = 503
    response = post(sync, "prepare", payload(sync, bundle, [check]))
    assert response.status_code == 503, response.text
    assert "checks" not in response.json() and "component" not in response.json()
    if failure == "withdrawn":
        with sync.store.transaction() as db:
            key = fingerprint(sync.physicals[0]["key"])
            assert (
                db.execute("SELECT state FROM physical_sources WHERE key=?", (key,)).fetchone()[0]
                == "withdrawn"
            )
            assert db.execute("SELECT state FROM sources").fetchone()[0] == "withdrawn"


@pytest.mark.parametrize("necessary", [False, True])
def test_raw_author_origin_is_independent_and_does_not_borrow_primary_lease(sync, necessary):
    setup(sync)
    bundle = query(sync, "原文")["bundle"]
    check = raw_check(sync)
    context = optional_origin(sync, check)
    context["expires_at"] = "2020-01-01T00:00:00Z"
    response = post(
        sync, "prepare", payload(sync, bundle, [check], [check["check_id"]] if necessary else [])
    )
    assert response.status_code == 200, response.text
    if necessary:
        assert response.json()["component"] == "required_check"
        assert response.json()["failure"]["code"] == "forbidden"
    else:
        assert valid(sync, response)["checks"][0]["reason"] == "origin_denied"


def test_raw_check_is_fresh_again_at_the_next_checkpoint_and_lease_is_bounded(sync):
    setup(sync)
    selected = post(sync, "query", dict(request(sync, "原文"), retrieval_mode="context"))
    assert selected.status_code == 200, selected.text
    bundle = selected.json()["bundle"]
    check = raw_check(sync)
    context = optional_origin(sync, check)
    context["expires_at"] = utc(sync.service.clock() + timedelta(seconds=5))
    first = valid(sync, post(sync, "prepare", payload(sync, bundle, [check])))
    assert first["valid_until"] == context["expires_at"]
    sync.grants[0]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    sync.calls.clear()
    # A later checkpoint still performs the complete owner barrier, then rejects
    # the adopted raw source even though the first proof's lease has not expired.
    response = post(sync, "verify", verify_payload(sync, first["bundle"], [check]))
    assert response.status_code == 409, response.text
    assert source_calls(sync) == ["snapshot", "current", "head"]


def test_unrelated_private_person_is_not_a_raw_context_domain(sync):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("The group domain deliberately includes its real authors")
    setup(sync)
    actor = owner(sync, audience="self_private", conversation="private:unlinked")
    check = raw_check(sync, actor, turn=original_turn(sync, "private", actor=actor))
    bundle = query(sync, "原文")["bundle"]
    sync.calls.clear()
    result = valid(sync, post(sync, "prepare", payload(sync, bundle, [check])))
    assert result["checks"][0]["status"] == "rejected"
    assert result["checks"][0]["reason"] == "dependency_changed"
    observed = next(body for kind, body, _ in sync.calls if kind == "snapshot")
    assert observed["selectors"] == observed["turn_ids"] == []
