"""Revalidate actual upstream v2 selections in the same preparation checkpoint."""

import copy
from contextlib import contextmanager
from datetime import timedelta

import pytest
from test_consolidation import assistant_member, pending, prepare, submit, window, withdraw_source
from test_context_query_selection import guard_window
from test_contextual_recall import contextual_query, experience
from test_group_context import owner
from test_memory_context import ProofIssuer
from test_memory_preparation import checks
from test_memory_raw_sources import check_request, optional_origin, original_turn, raw_check
from test_memory_v2 import candidate, post, request
from test_memory_v2_boundaries import source_calls
from test_memory_v2_domains import private_conversation
from test_segments import MATERIALS, archive, forget_original, front
from test_segments import ready as segment_ready
from test_selected_source_proofs import snapshot_selectors, summaries
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, fingerprint, parse_time, source_selector, utc
from tianshu_memory.memory_context import preparation


def inherited(h, bundle, identity="check:upstream"):
    return dict(
        check_id=identity,
        kind="context_bundle",
        request=dict(
            query=request(h, "actual upstream context")["query"],
            requested_scope=bundle["effective_scope"],
            bundle=copy.deepcopy(bundle),
        ),
        expected={},
    )


def empty_bundle(h):
    return contextual_query(h, "当前无选中材料", limits=dict(facts=0, episodes=0))["bundle"]


def before_checkpoint(monkeypatch, callback):
    """A real authority write after requirements, before the queued checkpoint."""
    original = preparation.operation
    called = []

    @contextmanager
    def boundary(service, items):
        if not called:
            called.append(True)
            callback()
        with original(service, items) as db:
            yield db

    monkeypatch.setattr(preparation, "operation", boundary)
    return called


def test_expired_upstream_bundle_is_freshly_proved_with_raw_in_one_boundary(sync, monkeypatch):
    old, _, selected, _, unrelated = summaries(sync)
    main = empty_bundle(sync)
    sync.service.clock = lambda: parse_time(old["valid_until"]) + timedelta(seconds=1)
    child = inherited(sync, old)
    origin = optional_origin(sync, child)
    origin["expires_at"] = utc(sync.service.clock() + timedelta(seconds=5))
    raw = raw_check(sync, turn=original_turn(sync, "inherited-current-input"))
    catalog = [child, raw]
    checked = guard_window(monkeypatch, unrelated["window_id"])
    original = copy.deepcopy(child["request"]["bundle"])
    sync.calls.clear()
    response = post(sync, "prepare", check_request(sync, main, catalog, "prepare"))
    assert response.status_code == 200 and response.json()["state"] == "valid", response.text
    assert response.json()["checks"] == [
        dict(check_id=item["check_id"], kind=item["kind"], status="valid") for item in catalog
    ]
    assert child["request"]["bundle"] == original
    assert parse_time(original["valid_until"]) < sync.service.clock()
    assert response.json()["valid_until"] == origin["expires_at"]
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert checked == [{selected["window_id"]}]
    assert snapshot_selectors(sync) == {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in selected["members"]
    } | {
        canonical(source_selector(source, raw["request"]["requested_scope"]))
        for source in raw["request"]["source_check"]["sources"]
    }
    strict = post(sync, "verify", check_request(sync, main, catalog, "verify"))
    assert strict.status_code == 200 and strict.json()["state"] == "valid", strict.text
    assert strict.json()["valid_until"] == origin["expires_at"]


@pytest.mark.parametrize("field", ["evidence_digest", "source_dependencies", "limits"])
@pytest.mark.parametrize("required", [False, True])
def test_forged_child_selection_has_its_own_outcome(sync, field, required):
    prepare(sync)
    experience(sync)
    child = inherited(sync, contextual_query(sync, "河边散步")["bundle"])
    main = empty_bundle(sync)
    bundle = child["request"]["bundle"]
    if field == "evidence_digest":
        bundle[field] = "0" * 64
    elif field == "source_dependencies":
        bundle[field][0]["epoch"] += 1
    else:
        bundle[field]["facts"] -= 1
    sync.calls.clear()
    body = check_request(sync, main, [child], "prepare")
    body["required_check_ids"] = [child["check_id"]] if required else []
    response = post(sync, "prepare", body)
    assert response.status_code == 200, response.text
    if required:
        assert response.json()["component"] == "required_check"
        assert response.json()["check_id"] == child["check_id"]
    else:
        assert response.json()["state"] == "valid"
        assert response.json()["checks"] == [
            dict(
                check_id=child["check_id"],
                kind="context_bundle",
                status="rejected",
                reason="dependency_changed",
            )
        ]
    assert snapshot_selectors(sync) == set()
    strict = post(sync, "verify", check_request(sync, main, [child], "verify"))
    assert strict.status_code == 409, strict.text


@pytest.mark.parametrize("when", ["before_planning", "after_planning"])
@pytest.mark.parametrize("required", [False, True])
def test_child_public_forget_before_checkpoint_never_invalidates_an_independent_main(
    sync, monkeypatch, when, required
):
    prepare(sync)
    saved = experience(sync)
    child = inherited(
        sync, contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=1))["bundle"]
    )
    assert child["request"]["bundle"]["episode_dependencies"]
    main = empty_bundle(sync)

    def forget():
        result = sync.post("memory/revise", sync.revision(saved["result"]["record_ids"][0]))
        assert result.status_code == 200, result.text
        assert result.json()["authoritative_state"] == "tombstoned"

    if when == "before_planning":
        forget()
    else:
        before_checkpoint(monkeypatch, forget)
    body = check_request(sync, main, [child], "prepare")
    body["required_check_ids"] = [child["check_id"]] if required else []
    result = post(sync, "prepare", body)
    assert result.status_code == 200, result.text
    if required:
        assert result.json()["component"] == "required_check"
        assert result.json()["check_id"] == child["check_id"]
    else:
        assert result.json()["state"] == "valid"
        assert result.json()["bundle"]["bundle_id"] == main["bundle_id"]
        assert result.json()["checks"][0]["reason"] == "dependency_changed"
    strict = post(sync, "verify", check_request(sync, main, [child], "verify"))
    assert strict.status_code == 409, strict.text


def test_shared_primary_and_child_root_cannot_be_dropped_as_optional(sync, monkeypatch):
    prepare(sync)
    saved = experience(sync)
    main = contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=1))["bundle"]
    child = inherited(sync, main)

    def forget():
        result = sync.post("memory/revise", sync.revision(saved["result"]["record_ids"][0]))
        assert result.status_code == 200, result.text

    before_checkpoint(monkeypatch, forget)
    result = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "invalid"
    assert result.json()["component"] == "current_selection"


@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_children_in_one_scope_keep_their_independent_complete_selections(
    sync, monkeypatch, reverse
):
    prepare(sync)
    saved = experience(sync, "在河边散步。", 1)
    first = inherited(
        sync,
        contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=1))["bundle"],
        "check:first",
    )
    experience(sync, "在图书馆阅读。", 2)
    second = inherited(
        sync,
        contextual_query(sync, "图书馆阅读", limits=dict(facts=0, episodes=1))["bundle"],
        "check:second",
    )
    main = empty_bundle(sync)

    def forget():
        result = sync.post("memory/revise", sync.revision(saved["result"]["record_ids"][0]))
        assert result.status_code == 200, result.text

    before_checkpoint(monkeypatch, forget)
    catalog = [second, first] if reverse else [first, second]
    response = post(sync, "prepare", check_request(sync, main, catalog, "prepare"))
    assert response.status_code == 200 and response.json()["state"] == "valid", response.text
    assert {item["check_id"]: item["status"] for item in response.json()["checks"]} == {
        "check:first": "rejected",
        "check:second": "valid",
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_a_true_legacy_full_component_keeps_full_coverage_with_upstream_context(
    sync, monkeypatch, reverse
):
    old, _, _, _, unrelated = summaries(sync)
    main = empty_bundle(sync)
    child = inherited(sync, old)
    history = checks(sync, main)[1]
    catalog = [history, child] if reverse else [child, history]
    observed = guard_window(monkeypatch, unrelated["window_id"])
    result = post(sync, "prepare", check_request(sync, main, catalog, "prepare"))
    assert result.status_code == 503 and result.json()["code"] == "dependency_unavailable"
    assert any(unrelated["window_id"] in identities for identities in observed)


@pytest.mark.parametrize("required", [False, True])
def test_child_origin_is_reauthorized_without_borrowing_the_primary_lease(sync, required):
    prepare(sync)
    experience(sync)
    child = inherited(sync, contextual_query(sync, "河边散步")["bundle"])
    main = empty_bundle(sync)
    origin = optional_origin(sync, child)
    origin["expires_at"] = "2020-01-01T00:00:00Z"
    body = check_request(sync, main, [child], "prepare")
    body["required_check_ids"] = [child["check_id"]] if required else []
    result = post(sync, "prepare", body)
    assert result.status_code == 200, result.text
    if required:
        assert result.json()["component"] == "required_check"
    else:
        assert result.json()["state"] == "valid"
        assert result.json()["checks"][0]["reason"] == "origin_denied"
    strict = post(sync, "verify", check_request(sync, main, [child], "verify"))
    assert strict.status_code == 403, strict.text


@pytest.mark.parametrize("remote", ["snapshot", "current"])
def test_child_remote_failure_cannot_degrade_to_an_optional_success(sync, remote):
    prepare(sync)
    experience(sync)
    child = inherited(sync, contextual_query(sync, "河边散步")["bundle"])
    main = empty_bundle(sync)
    sync.status[remote] = 503
    result = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert result.status_code == 503 and result.json()["code"] == "dependency_unavailable"
    assert "checks" not in result.json()


def test_upstream_bot_inherits_the_real_foreign_owner_window_and_next_withdrawal(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("A real foreign speaker in the same physical conversation")
    prepare(sync)
    other = owner(sync)
    prior = candidate(sync, "约好九点去河边。", 1, other, capture=False)[0]
    prior_window = window(sync, [prior])
    submit(sync, pending(sync, [prior], prior_window), "约好九点去河边。")
    other_body = dict(request(sync, "九点河边", actor=other), retrieval_mode="context")
    old = post(sync, "query", other_body)
    assert old.status_code == 200 and old.json()["evidence"], old.text
    dependencies = {
        key: old.json()["bundle"][key]
        for key in ("group_dependencies", "episode_dependencies", "source_dependencies")
    }
    current = candidate(sync, "我也会到场。", 2, capture=False)[0]
    human = window(sync, [current])["manifest"]["members"][0]
    bot = assistant_member(sync, human, "记得九点在河边见。", memory_dependencies=dependencies)
    sync.core_head["sequence"] += 1
    supplied = window(sync, [current], assistant=[bot])
    done, _ = submit(sync, pending(sync, [current], supplied), "本人准备在河边签到。")
    child = inherited(
        sync, contextual_query(sync, "签到", limits=dict(facts=1, episodes=0))["bundle"]
    )
    assert (
        child["request"]["bundle"]["group_dependencies"][0]["group"]["record_ids"]
        == done["result"]["record_ids"]
    )
    main = empty_bundle(sync)
    sync.calls.clear()
    result = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert result.status_code == 200 and result.json()["checks"][0]["status"] == "valid"
    assert {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in prior_window["manifest"]["members"]
    } <= snapshot_selectors(sync)
    withdraw_source(sync, prior)
    body = check_request(sync, main, [child], "prepare")
    body["required_check_ids"] = [child["check_id"]]
    rejected = post(sync, "prepare", body)
    assert rejected.status_code == 200 and rejected.json()["component"] == "required_check"


def test_changed_child_graph_is_rebuilt_at_the_real_commit_cas(sync):
    child_bundle, _, _, _, unrelated = summaries(sync)
    child = inherited(sync, child_bundle)
    main = empty_bundle(sync)
    identity = child_bundle["group_dependencies"][0]["group"]["semantic_group_id"]
    member = unrelated["members"][0]
    key = sync.service.source_identity(member["reference"]["source"], member["source_scope"])
    moved = []

    def mutate(kind, body, response):
        if kind == "head" and not moved:
            moved.append(True)
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

    sync.mutate = mutate
    sync.calls.clear()
    result = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert moved and result.status_code == 200 and result.json()["state"] == "valid", result.text
    assert result.json()["checks"][0]["status"] == "rejected"
    snapshots = [body for kind, body, _ in sync.calls if kind == "snapshot"]
    assert len(snapshots) >= 2
    selector = canonical(source_selector(member["reference"]["source"], member["source_scope"]))
    assert selector not in {canonical(item) for item in snapshots[0]["selectors"]}
    assert selector in {canonical(item) for item in snapshots[-1]["selectors"]}


@pytest.mark.parametrize("primary,required", [(False, False), (False, True), (True, False)])
def test_real_association_revoke_after_planning_preserves_exact_root_ownership(
    sync, monkeypatch, primary, required
):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Explicit private association controls a selected foreign owner")
    prepare(sync)
    linked = private_conversation(sync, "private:upstream-owner", same_person=False)
    job = candidate(sync, "去年去过海边旅行。", 1, linked, capture=False)[0]
    submit(sync, pending(sync, [job], window(sync, [job])), "去年去过海边旅行。")
    sync.config["callers"]["companion"]["operations"].append("context_association")
    sync.save()
    proofs = ProofIssuer()
    sync.service.memory_context.proofs = proofs
    link = dict(
        command=sync.command(),
        action="link",
        source_account=sync.account,
        target_account=sync.physicals[linked]["author"],
        association_id=None,
        expected_version=0,
        scopes=[sync.scope(), sync.scope(linked)],
        proof_ref="context:upstream-association",
    )
    proofs.issue(
        link, "association", [link["source_account"], link["target_account"]], link["scopes"]
    )
    response = sync.post("memory/context/association", link)
    assert response.status_code == 200, response.text
    upstream = contextual_query(
        sync, "海边旅行", include_associated=True, limits=dict(facts=1, episodes=0)
    )["bundle"]
    assert upstream["source_dependencies"][0]["owner_scope"] == sync.scope(linked)
    child = inherited(sync, upstream)
    main = (
        upstream
        if primary
        else contextual_query(
            sync, "当前无选中材料", include_associated=True, limits=dict(facts=0, episodes=0)
        )["bundle"]
    )
    baseline = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert baseline.status_code == 200 and baseline.json()["state"] == "valid", baseline.text
    assert baseline.json()["checks"][0]["status"] == "valid"

    def revoke():
        result = sync.post(
            "memory/context/association",
            dict(
                command=sync.command(),
                action="revoke",
                source_account=sync.account,
                target_account=None,
                association_id=response.json()["association_id"],
                expected_version=1,
                scopes=[],
                proof_ref=None,
            ),
        )
        assert result.status_code == 200, result.text

    before_checkpoint(monkeypatch, revoke)
    body = check_request(sync, main, [child], "prepare")
    body["required_check_ids"] = [child["check_id"]] if required else []
    result = post(sync, "prepare", body)
    assert result.status_code == 200, result.text
    if primary:
        assert result.json()["component"] == "current_selection"
    elif required:
        assert result.json()["component"] == "required_check"
        assert result.json()["check_id"] == child["check_id"]
    else:
        assert result.json()["state"] == "valid"
        assert result.json()["checks"][0]["reason"] == "dependency_changed"
    strict = post(sync, "verify", check_request(sync, main, [child], "verify"))
    assert strict.status_code == 409, strict.text


@pytest.mark.parametrize("direction", ["core", "observation"])
def test_child_episode_proves_both_real_aliases_and_public_forget_cannot_be_optional_revival(
    sync, monkeypatch, direction
):
    if sync.scope()["audience"] != "group":
        pytest.skip("An actual observation/Core physical mapping")
    segment_ready(sync)
    material, original = archive(sync, text="一起去河边散步。")
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "一起去河边散步。", "upstream-alias", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = {
        key: original[key] for key in ("source_ref", "source_digest", "scope_version")
    } | dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        archive_epoch=1,
    )
    accepted = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200 and accepted.json()["materials"] == [material]
    child = inherited(
        sync, contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=8))["bundle"]
    )
    assert child["request"]["bundle"]["episode_dependencies"]
    main = empty_bundle(sync)
    sync.calls.clear()
    sync.passive_owner.calls.clear()
    valid = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert valid.status_code == 200 and valid.json()["checks"][0]["status"] == "valid", valid.text
    assert snapshot_selectors(sync) == {
        canonical(source_selector(event["sources"][0], event["scope"]))
    }
    assert len(sync.passive_owner.calls) == 2
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])

    def forget():
        target = dict(material, source_key=core_key) if direction == "core" else material
        forget_original(sync, target, actor)

    before_checkpoint(monkeypatch, forget)
    result = post(sync, "prepare", check_request(sync, main, [child], "prepare"))
    assert result.status_code == 200 and result.json()["state"] == "valid", result.text
    assert result.json()["checks"][0]["reason"] == "dependency_changed"
    with sync.store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT source_key FROM suppression")} >= {
            core_key,
            material["source_key"],
        }
    strict = post(sync, "verify", check_request(sync, main, [child], "verify"))
    assert strict.status_code == 409, strict.text
