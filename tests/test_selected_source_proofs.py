"""Selected context proves its full DB closure without borrowing a full domain."""

import copy
import json

import pytest
from test_consolidation import assistant_member, pending, prepare, submit, window, withdraw_source
from test_contextual_recall import contextual_query, experience
from test_group_context import owner
from test_memory_context import ProofIssuer
from test_memory_preparation import checks, payload
from test_memory_raw_sources import check_request, original_turn, raw_check
from test_memory_v2 import candidate, post, request
from test_memory_v2_boundaries import verify
from test_memory_v2_domains import private_conversation
from test_segments import archive
from test_segments import front as segment_front
from test_segments import output as segment_output
from test_segments import pending as segment_pending
from test_segments import ready as segment_ready
from test_segments import seal as segment_seal
from test_segments import submit as segment_submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import consolidation_sources
from tianshu_memory.domain import Fault, canonical, source_selector
from tianshu_memory.memory_context.read_bundle import operation, requirements


def summaries(h):
    prepare(h)
    first = candidate(h, "河边散步。", 1, capture=False)[0]
    selected_window = window(h, [first])
    submit(h, pending(h, [first], selected_window), "河边散步。")
    bundle = contextual_query(h, "河边散步", limits=dict(facts=8, episodes=0))["bundle"]
    assert bundle["group_dependencies"] and not bundle["episode_dependencies"]
    second = candidate(h, "图书馆阅读。", 2, capture=False)[0]
    unrelated_window = window(h, [second])
    submit(h, pending(h, [second], unrelated_window), "图书馆阅读。")
    return bundle, first, selected_window["manifest"], second, unrelated_window["manifest"]


def snapshot_selectors(h):
    return {
        canonical(selector)
        for kind, body, _ in h.calls
        if kind == "snapshot"
        for selector in body["selectors"]
    }


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_unselected_window_and_archive_failure_do_not_enter_selected_proof(
    sync, monkeypatch, phase
):
    if sync.scope()["audience"] != "group":
        pytest.skip("Unrelated authors share a physical group")
    bundle, job, selected, _, unrelated = summaries(sync)
    segment_ready(sync)
    material, _ = archive(sync, text="另一个人准备爬山。")
    prepared = segment_pending(sync, segment_seal(sync, [material], "segment:unrelated")[0])
    assert segment_submit(sync, prepared, segment_output(prepared))[0].status_code == 200
    sync.passive_owner.calls.clear()
    sync.passive_owner.read_allowed = False
    checked = []
    original = consolidation_sources.window_failures

    def selected_windows(service, db, manifests, turns, owners):
        manifests = list(manifests)
        ids = {item["window_id"] for item in manifests}
        checked.append(ids)
        if unrelated["window_id"] in ids:
            raise Fault("dependency_unavailable", 503)
        return original(service, db, manifests, turns, owners)

    monkeypatch.setattr(consolidation_sources, "window_failures", selected_windows)
    raw = raw_check(sync, turn=original_turn(sync, "selected-only", count=2))
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, [raw], phase))
    assert response.status_code == 200 and response.json()["state"] == "valid", response.text
    assert response.json()["bundle"]["bundle_id"] == bundle["bundle_id"]
    expected = {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in selected["members"]
    } | {
        canonical(source_selector(source, raw["request"]["requested_scope"]))
        for source in raw["request"]["source_check"]["sources"]
    }
    assert snapshot_selectors(sync) == expected
    assert checked == [{selected["window_id"]}]
    assert sync.passive_owner.calls == []
    withdraw_source(sync, job)
    rejected = post(sync, phase, check_request(sync, bundle, [raw], phase))
    if phase == "prepare":
        assert rejected.status_code == 200 and rejected.json()["component"] == "current_selection"
    else:
        assert rejected.status_code == 409, rejected.text


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_foreign_context_leaf_is_still_proved_and_its_withdrawal_rejects_selected_owner(
    sync, phase
):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two true authors in a single selected saved window")
    prepare(sync)
    other = owner(sync)
    context = candidate(sync, "约在河边见。", 1, other, capture=False)[0]
    current = candidate(sync, "九点去河边散步。", 2, capture=False)[0]
    supplied = window(sync, [current], context=[context])
    submit(sync, pending(sync, [current], supplied), "本人约九点去河边散步。")
    bundle = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))["bundle"]
    assert {item["owner_scope"]["person_id"] for item in bundle["source_dependencies"]} == {
        sync.scope()["person_id"],
        sync.scope(other)["person_id"],
    }
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, [], phase))
    assert response.status_code == 200 and response.json()["state"] == "valid", response.text
    assert snapshot_selectors(sync) == {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in supplied["manifest"]["members"]
    }
    withdraw_source(sync, context)
    rejected = post(sync, phase, check_request(sync, bundle, [], phase))
    assert (
        (rejected.status_code == 200 and rejected.json().get("state") == "invalid")
        if phase == "prepare"
        else rejected.status_code == 409
    )


@pytest.mark.parametrize("phase", ["prepare", "verify"])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_scope_full_component_wins_without_losing_raw_or_selected_obligations(
    sync, phase, reverse
):
    bundle, _, selected, _, unrelated = summaries(sync)
    fresh = contextual_query(sync, "河边散步")["bundle"]
    history = checks(sync, fresh)[1]
    raw = raw_check(sync, turn=original_turn(sync, "full-priority"))
    catalog = [raw, history] if reverse else [history, raw]
    sync.calls.clear()
    response = post(sync, phase, check_request(sync, bundle, catalog, phase))
    assert response.status_code == 200 and response.json()["state"] == "valid", response.text
    expected = {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for manifest in (selected, unrelated)
        for member in manifest["members"]
    } | {
        canonical(source_selector(source, raw["request"]["requested_scope"]))
        for source in raw["request"]["source_check"]["sources"]
    }
    assert expected <= snapshot_selectors(sync)
    assert all(item["status"] == "valid" for item in response.json()["checks"])


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_mixed_full_unlink_invalidates_only_unselected_bot_inherited_window(sync, phase):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Explicit private association controls the BOT's inherited source")
    prepare(sync)
    experience(sync, "本人在河边散步。")
    selected = contextual_query(sync, "河边散步", limits=dict(facts=1, episodes=0))["bundle"]
    linked = private_conversation(sync, "private:inherited-context", same_person=False)
    old = candidate(sync, "去年去过海边旅行。", 2, linked, capture=False)[0]
    submit(sync, pending(sync, [old], window(sync, [old])), "去年去过海边旅行。")
    sync.config["callers"]["companion"]["operations"].append("context_association")
    sync.save()
    proof = ProofIssuer()
    sync.service.memory_context.proofs = proof
    link = dict(
        command=sync.command(),
        action="link",
        source_account=sync.account,
        target_account=sync.physicals[linked]["author"],
        association_id=None,
        expected_version=0,
        scopes=[sync.scope(), sync.scope(linked)],
        proof_ref="context:inherited-association",
    )
    proof.issue(
        link, "association", [link["source_account"], link["target_account"]], link["scopes"]
    )
    linked_response = sync.post("memory/context/association", link)
    assert linked_response.status_code == 200, linked_response.text
    recalled = contextual_query(
        sync, "海边旅行", include_associated=True, limits=dict(facts=1, episodes=0)
    )["bundle"]
    assert {item["owner_scope"]["person_id"] for item in recalled["source_dependencies"]} == {
        sync.scope(linked)["person_id"]
    }
    new = candidate(sync, "换个话题继续聊天。", 3, capture=False)[0]
    human = window(sync, [new])["manifest"]["members"][0]
    assistant = assistant_member(
        sync,
        human,
        "你提到过那次海边旅行。",
        memory_dependencies={
            key: recalled[key]
            for key in ("group_dependencies", "episode_dependencies", "source_dependencies")
        },
    )
    sync.core_head["sequence"] += 1  # This acknowledged BOT response is a new owner state.
    supplied = window(sync, [new], assistant=[assistant])
    done, _ = submit(sync, pending(sync, [new], supplied), "之后继续聊天。")
    history = checks(sync, contextual_query(sync, "河边散步")["bundle"])[1]
    revoke = dict(
        command=sync.command(),
        action="revoke",
        source_account=sync.account,
        target_account=None,
        association_id=linked_response.json()["association_id"],
        expected_version=1,
        scopes=[],
        proof_ref=None,
    )
    assert sync.post("memory/context/association", revoke).status_code == 200
    with sync.store.transaction() as db:
        unrelated_group = db.execute(
            "SELECT group_id FROM records WHERE id=?", (done["result"]["record_ids"][0],)
        ).fetchone()[0]
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (unrelated_group,)).fetchone()[0]
            == "active"
        )
    result = post(sync, phase, check_request(sync, selected, [history], phase))
    if phase == "prepare":
        assert result.status_code == 200 and result.json()["state"] == "valid", result.text
        assert result.json()["checks"][0]["status"] == "rejected"
    else:
        assert result.status_code == 409, result.text
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (unrelated_group,)).fetchone()[0]
            != "active"
        )
        assert (
            db.execute(
                "SELECT state FROM groups WHERE id=?",
                (selected["group_dependencies"][0]["group"]["semantic_group_id"],),
            ).fetchone()[0]
            == "active"
        )
    assert verify(sync, selected).status_code == 200


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_full_fallback_keeps_selected_observation_episode_without_any_summary(sync, phase):
    segment_ready(sync)
    material, archived = archive(sync, text="本人在河边晨跑。", replies=())
    scope = dict(material["frame"], person_id=material["person_id"])
    context = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
    context.update(
        assertion_ref="synthetic-passive-viewer",
        verified_account=dict(namespace="qq", immutable_account_id=archived["event"]["account_id"]),
        allowed_scope=scope,
    )
    sync.contexts[context["assertion_ref"]] = context
    body = request(sync, "河边晨跑")
    body["query"]["origin"] = dict(assertion_ref=context["assertion_ref"])
    body["requested_scope"] = scope
    body["retrieval_mode"] = "context"
    body["queries"][0]["kinds"] = ["episodes"]
    response = post(sync, "query", body)
    assert response.status_code == 200, response.text
    selected = response.json()["bundle"]
    assert selected["episode_dependencies"] and not selected["group_dependencies"]
    assert {dep["source_key"] for dep in selected["source_dependencies"]} == {
        material["source_key"]
    }
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_summaries").fetchone()[0] == 0
    history = dict(
        check_id="check:history",
        kind="history",
        request=dict(
            query=body["query"],
            requested_scope=scope,
            include_associated=False,
            expected_snapshot=None,
            known_snapshot=selected["history_snapshot"],
        ),
        expected=dict(effective_scope=scope, snapshot=selected["history_snapshot"]),
    )

    def proof_request():
        return dict(
            check_request(sync, selected, [history], phase),
            query=body["query"],
            requested_scope=scope,
        )

    sync.passive_owner.calls.clear()
    proved = post(sync, phase, proof_request())
    assert proved.status_code == 200 and proved.json()["state"] == "valid", proved.text
    assert len(sync.passive_owner.calls) == 2
    assert all(
        check["sources"][0]["source_ref"] == sync.passive_owner.calls[0]["sources"][0]["source_ref"]
        for check in sync.passive_owner.calls
    )
    sync.passive_owner.archive_epoch += 1
    sync.passive_owner.revision += 1
    invalid = post(sync, phase, proof_request())
    if phase == "prepare":
        assert invalid.status_code == 200 and invalid.json()["state"] == "invalid", invalid.text
    else:
        assert invalid.status_code == 409, invalid.text


def test_selected_transaction_cannot_be_borrowed_as_an_unproved_full_read(sync):
    bundle, *_ = summaries(sync)
    context = copy.deepcopy(sync.contexts["synthetic-viewer:0"])
    request = payload(sync, bundle)
    items = requirements(sync.service, request, context)
    with operation(sync.service, items):
        with pytest.raises(Fault) as raised:
            with sync.service.operation(
                scope=sync.scope(),
                context=context,
                group_read=sync.scope()["audience"] == "group",
            ):
                pytest.fail("A selected proof cannot authorize a full domain read")
        assert (raised.value.code, raised.value.status) == ("dependency_unavailable", 503)


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_forged_selected_closure_is_rejected_before_any_owner_source_request(sync, phase):
    prepare(sync)
    experience(sync)
    bundle = contextual_query(sync, "河边散步")["bundle"]
    bundle["source_dependencies"] = []
    sync.calls.clear()
    rejected = post(sync, phase, check_request(sync, bundle, [], phase))
    if phase == "prepare":
        assert rejected.status_code == 200 and rejected.json()["component"] == "current_selection"
    else:
        assert rejected.status_code == 409, rejected.text
    assert not [kind for kind, _, _ in sync.calls if kind != "origin"]


def test_selected_dependency_graph_is_rebuilt_at_the_commit_cas(sync):
    bundle, _, selected, _, unrelated = summaries(sync)
    identity = bundle["group_dependencies"][0]["group"]["semantic_group_id"]
    member = unrelated["members"][0]
    key = sync.service.source_identity(member["reference"]["source"], member["source_scope"])
    moved = False
    original = sync.mutate

    def concurrent(kind, body, response):
        nonlocal moved
        if original is not None:
            original(kind, body, response)
        if kind == "head" and not moved:
            moved = True
            with sync.store.transaction() as db:
                source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
                db.execute(
                    "INSERT INTO memory_summary_sources VALUES (?,?,?,?,?,?)",
                    (
                        identity,
                        key,
                        source["revision"],
                        source["epoch"],
                        source["scope"],
                        "context",
                    ),
                )

    sync.mutate = concurrent
    sync.calls.clear()
    rejected = verify(sync, bundle)
    assert moved and rejected.status_code == 409, rejected.text
    snapshots = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    assert len(snapshots) >= 2
    added = canonical(source_selector(member["reference"]["source"], member["source_scope"]))
    assert added not in {canonical(item) for item in snapshots[0]["selectors"]}
    assert added in {canonical(item) for item in snapshots[-1]["selectors"]}
    assert selected["window_id"] != unrelated["window_id"]


def test_selected_portrait_keeps_windows_and_sources_from_its_actual_historical_basis(
    sync, monkeypatch
):
    prepare(sync)
    jobs, manifests = [], []
    for serial in (1, 2, 3):
        job = candidate(sync, "河边散步。", serial, capture=False)[0]
        supplied = window(sync, [job])
        jobs.append(job)
        manifests.append(supplied["manifest"])
        prepared = pending(sync, [job], supplied)
        done, _ = submit(
            sync, prepared, "去河边散步。", portrait="喜欢研究星空。" if serial > 1 else None
        )
        assert done["state"] == "completed"
    selected = contextual_query(sync, "研究星空", limits=dict(facts=8, episodes=0))
    assert any(item["kind"] == "portrait" for item in selected["evidence"])
    checked = []
    original = consolidation_sources.window_failures

    def observed(service, db, windows, turns, owners):
        windows = list(windows)
        checked.append({item["window_id"] for item in windows})
        return original(service, db, windows, turns, owners)

    monkeypatch.setattr(consolidation_sources, "window_failures", observed)
    sync.calls.clear()
    response = verify(sync, selected["bundle"])
    assert response.status_code == 200, response.text
    assert checked == [{manifest["window_id"] for manifest in manifests}]
    withdraw_source(sync, jobs[0])
    assert verify(sync, selected["bundle"]).status_code == 409


@pytest.mark.parametrize("phase", ["prepare", "verify"])
def test_full_fallback_keeps_selected_portrait_superseded_basis_bot_window(
    sync, monkeypatch, phase
):
    prepare(sync)
    old = candidate(sync, "河边散步。", 1, capture=False)[0]
    human = window(sync, [old])["manifest"]["members"][0]
    assistant = assistant_member(sync, human, "我们下次再一起聊聊。")
    sync.core_head["sequence"] += 1  # Delivery ACK advances the original Core authority.
    supplied = window(sync, [old], assistant=[assistant])
    done, _ = submit(sync, pending(sync, [old], supplied), "河边散步的经历。")
    prior = done["result"]["summary_record_refs"][0]
    segment_ready(sync)
    material, _ = segment_front(sync, "这次在公园散步。", "replace-with-portrait")
    prepared = segment_pending(sync, segment_seal(sync, [material])[0])
    target = prepared["segment_window"]["targets"][0]
    assert prior in target["portrait_input"]["summary_refs"]
    value = segment_output(prepared, portrait="喜欢研究星空。")
    value["results"][0]["memories"][0].update(operation="replace", prior_refs=[prior])
    committed, _ = segment_submit(sync, prepared, value)
    assert committed.status_code == 200, committed.text
    assert committed.json()["results"][0]["portrait_state"] == "committed"
    portrait = committed.json()["results"][0]["portrait_refs"][0]
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT g.state FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                (prior["record_id"],),
            ).fetchone()[0]
            == "superseded"
        )
        basis = db.execute(
            "SELECT s.summary_refs FROM records r JOIN memory_summaries s ON s.group_id=r.group_id WHERE r.id=?",
            (portrait["record_id"],),
        ).fetchone()[0]
        assert prior in json.loads(basis)
        assert supplied["manifest"]["window_id"] not in {
            manifest["window_id"]
            for manifest in consolidation_sources.stored_windows(
                db, sync.scope(), group_read=sync.scope()["audience"] == "group"
            )
        }
    selected = contextual_query(sync, "研究星空", limits=dict(facts=1, episodes=0))
    assert [item["kind"] for item in selected["evidence"]] == ["portrait"]
    history = checks(sync, selected["bundle"])[1]
    checked = []
    original = consolidation_sources.window_failures

    def observed(service, db, manifests, turns, owners):
        manifests = list(manifests)
        checked.append({manifest["window_id"] for manifest in manifests})
        return original(service, db, manifests, turns, owners)

    monkeypatch.setattr(consolidation_sources, "window_failures", observed)
    sync.calls.clear()
    proved = post(sync, phase, check_request(sync, selected["bundle"], [history], phase))
    assert proved.status_code == 200 and proved.json()["state"] == "valid", proved.text
    assert checked and all(supplied["manifest"]["window_id"] in ids for ids in checked)
    observed_turns = {
        identity
        for kind, body, _ in sync.calls
        if kind == "snapshot"
        for identity in body["turn_ids"]
    }
    assert human["turn_id"] in observed_turns
    # A real later Core turn state loses the acknowledged reply; the original
    # human source remains allowed. The selected portrait must still reject.
    turn = next(turn for turn in sync.turns if turn["turn_id"] == human["turn_id"])
    turn["reply_ids"] = []
    sync.core_head["sequence"] += 1
    invalid = post(sync, phase, check_request(sync, selected["bundle"], [history], phase))
    if phase == "prepare":
        assert invalid.status_code == 200 and invalid.json()["state"] == "invalid", invalid.text
    else:
        assert invalid.status_code == 409, invalid.text
