"""Complete source and window proofs share only identical transaction-local reads."""

import copy

import pytest
from test_consolidation import pending, prepare, submit, window, withdraw_source
from test_group_context import owner
from test_memory_raw_sources import check_request, original_turn, raw_check
from test_memory_v2 import candidate, post, request
from test_memory_v2_boundaries import source_calls
from test_memory_v2_domains import private_conversation
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import consolidation_sources
from tianshu_memory.domain import Fault, canonical, source_selector


def summaries(h, actors):
    jobs, manifests = [], []
    for serial, actor in enumerate(actors, 1):
        job = candidate(h, f"第{serial}次讨论海边旅行。", serial, actor, capture=False)[0]
        supplied = window(h, [job], context=jobs if h.scope()["audience"] == "group" else ())
        done, _ = submit(h, pending(h, [job], supplied), f"第{serial}次讨论海边旅行。")
        assert done["result"]["summary_state"] == "committed"
        jobs.append(job)
        manifests.append(supplied["manifest"])
    return jobs, manifests


def observe_collection(h, monkeypatch):
    authority = h.service.source_authority
    coverage, stored, failures = (
        authority.coverage,
        consolidation_sources.stored_windows,
        consolidation_sources.window_failures,
    )
    reads = dict(coverage=[], stored=[], windows=[])

    def covered(db, scope, sources, profile, group_read=False):
        reads["coverage"].append((copy.deepcopy(scope), profile, group_read))
        return coverage(db, scope, sources, profile, group_read)

    def windows(db, scope, *, group_read=False):
        reads["stored"].append((copy.deepcopy(scope), group_read))
        return stored(db, scope, group_read=group_read)

    def checked(service, db, manifests, turns, owner_checks):
        manifests = list(manifests)
        reads["windows"].append({item["window_id"] for item in manifests})
        return failures(service, db, manifests, turns, owner_checks)

    monkeypatch.setattr(authority, "coverage", covered)
    monkeypatch.setattr(consolidation_sources, "stored_windows", windows)
    monkeypatch.setattr(consolidation_sources, "window_failures", checked)
    return reads


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_same_room_collection_keeps_all_owners_raw_checks_and_windows_at_each_checkpoint(
    sync, monkeypatch, phase
):
    if sync.scope()["audience"] != "group":
        pytest.skip("The data domain is the current group with distinct original owners")
    prepare(sync)
    actors = [0, owner(sync), owner(sync)]
    jobs, manifests = summaries(sync, actors)
    selected = post(sync, "query", dict(request(sync, "海边旅行"), retrieval_mode="context"))
    assert selected.status_code == 200, selected.text
    bundle = selected.json()["bundle"]
    catalog = [
        raw_check(
            sync,
            actor,
            turn=original_turn(sync, f"read-domain:{index}", actor=actor),
            identity=f"read-domain:{index}",
        )
        for index, actor in enumerate([*actors, actors[1]])
    ]
    expected_selectors = {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for manifest in manifests
        for member in manifest["members"]
    } | {
        canonical(source_selector(source, check["request"]["requested_scope"]))
        for check in catalog
        for source in check["request"]["source_check"]["sources"]
    }
    expected_turns = {
        member["turn_id"] for manifest in manifests for member in manifest["members"]
    } | {check["request"]["source_check"]["turn_id"] for check in catalog}
    expected_windows = {manifest["window_id"] for manifest in manifests}
    reads = observe_collection(sync, monkeypatch)
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, catalog, phase))
    assert response.status_code == 200, response.text
    assert response.json()["checks"] == [
        dict(check_id=item["check_id"], kind="input_sources", status="valid") for item in catalog
    ]
    observed = next(body for kind, body, _ in sync.calls if kind == "snapshot")
    assert {canonical(item) for item in observed["selectors"]} == expected_selectors
    assert set(observed["turn_ids"]) == expected_turns
    assert reads["windows"] == [expected_windows]
    # One union collection, one fresh primary read and the unchanged commit CAS read.
    assert len(reads["coverage"]) == 3
    assert len(reads["stored"]) == 1
    assert source_calls(sync) == ["snapshot", "current", "head"]

    # Same room/owner is not permission to omit a later check's independent source.
    sync.grants[-1]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    sync.calls.clear()
    rejected = post(
        sync, "verify", check_request(sync, response.json()["bundle"], catalog, "verify")
    )
    assert rejected.status_code == 409, rejected.text
    assert "checks" not in rejected.json()
    assert source_calls(sync) == ["snapshot", "current", "head"]

    # The next boundary must reread all old windows and persist a real owner withdrawal.
    withdraw_source(sync, jobs[0])
    sync.calls.clear()
    for values in reads.values():
        values.clear()
    query = dict(request(sync, "海边旅行"), retrieval_mode="context")
    query["queries"][0]["kinds"] = ["facts"]
    fresh = post(sync, "query", query)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["evidence"] == []
    assert reads["windows"] == [expected_windows]
    assert len(reads["stored"]) == 1
    assert source_calls(sync) == ["snapshot", "current", "head"]
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM groups WHERE state='active'").fetchone()[0] == 0


@pytest.mark.parametrize("difference", ["person", "conversation"])
def test_explicit_private_requirements_keep_distinct_data_and_each_origin(
    sync, monkeypatch, difference
):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Private data queries retain every complete scope field")
    prepare(sync)
    other = (
        owner(sync, audience="self_private", conversation=sync.scope()["conversation_id"])
        if difference == "person"
        else private_conversation(sync, "private:read-domain", same_person=True)
    )
    _, manifests = summaries(sync, [0, other])
    for actor in (0, other):
        admitted = sync.post("memory/source-sync/check", sync.check(actor))
        assert admitted.status_code == 200, admitted.text
    # Exercise the authority's authenticated requirements directly. The application
    # separately limits which linked private scopes a single HTTP caller may request.
    items = [
        dict(
            scope=sync.scope(actor),
            context=copy.deepcopy(sync.contexts[f"synthetic-viewer:{actor}"]),
            profile=False,
            group_read=False,
            source_checks=[sync.check(actor)],
        )
        for actor in (0, other, other)
    ]
    reads = observe_collection(sync, monkeypatch)
    sync.calls.clear()
    authority = sync.service.source_authority
    with authority.bundle_operation(sync.service, items) as db:
        for actor in (0, other):
            with sync.service.operation(sync.scope(actor)) as borrowed:
                assert borrowed is db
                assert sync.service._source_rows(
                    db, sync.check(actor)["sources"], sync.scope(actor)
                )
    observed = next(body for kind, body, _ in sync.calls if kind == "snapshot")
    assert len(observed["selectors"]) == 4
    assert set(observed["turn_ids"]) == {
        *(sync.turns[actor]["turn_id"] for actor in (0, other)),
        *(member["turn_id"] for manifest in manifests for member in manifest["members"]),
    }
    assert reads["windows"] == [{manifest["window_id"] for manifest in manifests}]
    # Two private domains plus the original primary/commit reads, not one room domain.
    assert len(reads["coverage"]) == 4
    assert len(reads["stored"]) == 2
    assert source_calls(sync) == ["snapshot", "current", "head"]

    # A duplicate data domain still needs its own origin decision before any owner I/O.
    invalid = copy.deepcopy(items)
    invalid[-1]["context"]["revoked"] = True
    sync.calls.clear()
    with pytest.raises(Fault) as denied:
        with authority.bundle_operation(sync.service, invalid):
            pytest.fail("The revoked origin must not borrow the prior identical data read")
    assert denied.value.code == "forbidden" and denied.value.status == 403
    assert source_calls(sync) == []
