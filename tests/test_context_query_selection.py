"""New context discovery stays local; only returned material obtains fresh proof."""

import copy
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from manual_memory_fixtures import reviewed
from summary_fixtures import pending, prepare, submit, window, withdraw_source
from test_contextual_recall import contextual_query, experience
from test_group_context import owner
from test_memory_v2 import candidate, post, request, setup
from test_memory_v2_boundaries import attach, source_calls, verify
from test_query_vector_boundary import indexed, outside_boundaries
from test_segments import MATERIALS, archive, forget_original, front
from test_segments import output as segment_output
from test_segments import pending as segment_pending
from test_segments import ready as segment_ready
from test_segments import seal as segment_seal
from test_segments import submit as segment_submit
from test_selected_source_proofs import snapshot_selectors, summaries
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import consolidation_sources, memory_documents
from tianshu_memory.domain import Fault, canonical, fingerprint, parse_time, source_selector
from tianshu_memory.memory_context import read_v2
from tianshu_memory.memory_reindex import materialize


def before_final(monkeypatch, callback):
    """A real write may finish after discovery and before the source checkpoint."""
    original = read_v2.operation
    seen = []

    @contextmanager
    def boundary(service, items):
        if not seen:
            seen.append(copy.deepcopy(items))
            callback(items)
        with original(service, items) as db:
            yield db

    monkeypatch.setattr(read_v2, "operation", boundary)
    return seen


def guard_window(monkeypatch, rejected):
    original = consolidation_sources.window_failures
    checked = []

    def check(service, db, manifests, turns, owners):
        manifests = list(manifests)
        identities = {item["window_id"] for item in manifests}
        checked.append(identities)
        if rejected in identities:
            raise Fault("dependency_unavailable", 503)
        return original(service, db, manifests, turns, owners)

    monkeypatch.setattr(consolidation_sources, "window_failures", check)
    return checked


def test_new_context_query_does_not_prove_an_unreturned_window_or_archive(sync, monkeypatch):
    if sync.scope()["audience"] != "group":
        pytest.skip("An unrelated archive author shares the real group")
    _, _, selected, _, unrelated = summaries(sync)
    segment_ready(sync)
    archive(sync, text="另一个人准备登山。")
    sync.passive_owner.calls.clear()
    sync.passive_owner.read_allowed = False
    checked = guard_window(monkeypatch, unrelated["window_id"])
    sync.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))
    assert result["evidence"] and all(item["kind"] == "summary" for item in result["evidence"])
    assert snapshot_selectors(sync) == {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in selected["members"]
    }
    assert checked == [{selected["window_id"]}]
    assert sync.passive_owner.calls == []
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("entry", ["factual", "known_bundle", "rerank"])
def test_full_query_paths_keep_unrelated_complete_window_proof(sync, monkeypatch, entry):
    bundle, _, _, _, unrelated = summaries(sync)
    checked = guard_window(monkeypatch, unrelated["window_id"])
    body = request(sync, "河边散步")
    if entry != "factual":
        body["retrieval_mode"] = "context"
    if entry == "known_bundle":
        body["known_bundle"] = bundle
    backend = attach(sync, "keyword", True) if entry == "rerank" else None
    response = post(sync, "query", body)
    assert response.status_code == 503 and response.json()["code"] == "dependency_unavailable"
    assert any(unrelated["window_id"] in identities for identities in checked)
    if backend is not None:
        assert all(operation == "settings" for operation, _ in backend.calls)


@pytest.mark.parametrize("change", ["append", "expire", "correct"])
def test_final_query_boundary_never_adds_or_refills_candidates(sync, monkeypatch, change):
    prepare(sync)
    experience(sync, "去河边散步。", 1, importance="low")
    experience(sync, "去河边散步并喝茶。", 2, importance="low")
    with sync.store.transaction() as db:
        expiry = parse_time(
            db.execute("SELECT MIN(expires_at) FROM memory_summaries").fetchone()[0]
        )
    sync.service.clock = lambda: expiry - timedelta(seconds=1)
    selected = []

    def change_after_discovery(items):
        selected.extend(items[0]["selected"]["group_ids"])
        assert len(selected) == 1
        with sync.store.transaction() as db:
            row = db.execute(
                "SELECT r.id,s.expires_at FROM records r JOIN memory_summaries s ON s.group_id=r.group_id WHERE r.group_id=?",
                (selected[0],),
            ).fetchone()
        if change == "append":
            experience(sync, "去河边散步，又见到新朋友。", 3)
        elif change == "expire":
            sync.service.clock = lambda: parse_time(row["expires_at"]) + timedelta(seconds=1)
        else:
            response = sync.post("memory/revise", sync.revision(row["id"], kind="correct"))
            assert response.status_code == 200, response.text
        sync.calls.clear()

    before_final(monkeypatch, change_after_discovery)
    result = contextual_query(sync, "河边散步", limits=dict(facts=1, episodes=0))
    if change == "append":
        assert {item["unit"]["semantic_group_id"] for item in result["evidence"]} == set(selected)
    else:
        assert result["evidence"] == [] and "stale_source" in result["omissions"]
    assert "budget" in result["omissions"]
    assert verify(sync, result["bundle"]).status_code == 200


def test_bounded_vector_context_query_proves_only_the_selected_episode(sync, monkeypatch):
    backend = indexed(sync)
    outside_boundaries(sync, monkeypatch, backend)
    originals = copy.deepcopy(backend.document_batches)
    sync.calls.clear()
    result = contextual_query(sync, "睡前选什么", limits=dict(facts=0, episodes=1))
    assert len(result["evidence"]) == 1 and result["evidence"][0]["kind"] == "episode"
    assert {fingerprint(json.loads(item)) for item in snapshot_selectors(sync)} == {
        item["source_key"] for item in result["bundle"]["source_dependencies"]
    }
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert backend.query_calls == 1 and backend.inputs[0][0] == "睡前选什么"
    assert backend.document_batches == originals and backend.reranks == []
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("empty", [False, True])
def test_empty_or_zero_budget_context_query_still_has_a_fresh_viewer_boundary(sync, empty):
    prepare(sync)
    if not empty:
        experience(sync)
    sync.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=0))
    assert result["evidence"] == [] and result["bundle"]["source_dependencies"] == []
    assert snapshot_selectors(sync) == set()
    assert source_calls(sync) == ["snapshot", "current", "head"]
    assert result["query_results"][0]["status"] == "incomplete"
    assert "no_match" not in result["omissions"]
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize(
    "code,status", [("forbidden", 403), ("scope_changed", 409), ("dependency_unavailable", 503)]
)
def test_query_does_not_convert_authority_errors_to_an_empty_success(
    sync, monkeypatch, code, status
):
    prepare(sync)
    experience(sync)

    def reject(items):
        assert items[0]["selected"]["group_ids"]
        raise Fault(code, status)

    before_final(monkeypatch, reject)
    body = dict(request(sync, "河边散步"), retrieval_mode="context")
    response = post(sync, "query", body)
    assert response.status_code == status and response.json()["code"] == code


def test_new_query_proves_a_foreign_context_leaf_and_persists_its_negative(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two actual authors in the same selected window")
    prepare(sync)
    other = owner(sync)
    context = candidate(sync, "约在河边见。", 1, other, capture=False)[0]
    current = candidate(sync, "九点去河边散步。", 2, capture=False)[0]
    supplied = window(sync, [current], context=[context])
    submit(sync, pending(sync, [current], supplied), "本人约九点去河边散步。")
    sync.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))
    assert result["evidence"] and all(
        item["subject_person_id"] == sync.scope()["person_id"] for item in result["evidence"]
    )
    assert snapshot_selectors(sync) == {
        canonical(source_selector(member["reference"]["source"], member["source_scope"]))
        for member in supplied["manifest"]["members"]
    }
    withdraw_source(sync, context)
    fresh = contextual_query(sync, "河边散步", limits=dict(facts=8, episodes=0))
    assert fresh["evidence"] == [] and "stale_source" in fresh["omissions"]
    foreign = next(
        member
        for member in supplied["manifest"]["members"]
        if member["source_scope"] == sync.scope(other)
    )
    key = sync.service.source_identity(foreign["reference"]["source"], foreign["source_scope"])
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0] == "withdrawn"
        )


def test_query_episode_keeps_the_real_core_observation_alias_and_forget_closure(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("An actual observation/Core physical mapping")
    segment_ready(sync)
    material, original = archive(sync, text="一起去河边散步。")
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "一起去河边散步。", "query-alias", actor, accept=False)
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
    sync.calls.clear()
    sync.passive_owner.calls.clear()
    result = contextual_query(sync, "河边散步", limits=dict(facts=0, episodes=8))
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["subject_person_id"] == material["person_id"]
    assert snapshot_selectors(sync) == {
        canonical(source_selector(event["sources"][0], event["scope"]))
    }
    assert len(sync.passive_owner.calls) == 2
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])
    forget_original(sync, dict(material, source_key=core_key), actor)
    assert contextual_query(sync, "河边散步")["evidence"] == []
    with sync.store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT source_key FROM suppression")} >= {
            core_key,
            material["source_key"],
        }


@pytest.mark.parametrize("limit,change", [(1, False), (2, False), (2, True)])
def test_query_keeps_whole_groups_and_binds_every_record_version(sync, monkeypatch, limit, change):
    setup(sync)
    job, _ = candidate(sync, "河边散步时上午喝咖啡，晚上不喝咖啡。")
    event = sync.fixture_events[job]
    draft = copy.deepcopy(sync.examples["memory/candidate-commit"]["drafts"][0])
    draft["scope"] = sync.scope()
    unit = dict(draft["units"][0], sources=event["sources"])
    draft["units"] = [dict(unit, statement=text) for text in ("上午喝咖啡。", "晚上不喝咖啡。")]
    committed = reviewed(sync, draft)
    assert len(committed["record_ids"]) == 2
    # The reviewed legacy writer indexes before its lineage is complete. Use
    # the existing verified rebuild to attach the actual source's timestamp.
    assert materialize(sync.service, event["scope"], event["sources"]) == len(event["sources"])
    with sync.store.transaction() as db:
        document = db.execute(
            "SELECT observed_at FROM memory_documents WHERE group_id=?",
            (committed["group_ids"][0],),
        ).fetchone()
        assert document["observed_at"] is not None

    def update_after_discovery(items):
        if not change:
            return
        assert items[0]["selected"]["group_ids"] == committed["group_ids"]
        # The complete group changes without changing its indexed text/hash.
        # Fresh local eligibility still accepts this row, so source-only or
        # first-record comparisons cannot catch this concurrent version change.
        with sync.store.transaction() as db:
            identity = committed["record_ids"][1]
            row = db.execute("SELECT payload FROM records WHERE id=?", (identity,)).fetchone()
            payload = json.loads(row[0])
            payload["record_version"] += 1
            db.execute(
                "UPDATE records SET version=version+1,payload=? WHERE id=?",
                (canonical(payload), identity),
            )

    before_final(monkeypatch, update_after_discovery)
    sync.calls.clear()
    result = contextual_query(sync, "咖啡", limits=dict(facts=limit, episodes=0))
    if limit == 1 or change:
        assert result["evidence"] == []
        assert ("stale_source" if change else "budget") in result["omissions"]
    else:
        assert {item["unit"]["record_id"] for item in result["evidence"]} == set(
            committed["record_ids"]
        )
        assert set(result["bundle"]["group_dependencies"][0]["group"]["record_ids"]) == set(
            committed["record_ids"]
        )
    assert verify(sync, result["bundle"]).status_code == 200


def summary_pair(h, *, serial="summary-pair", portrait=None, actor=0, scenario="pottery"):
    """Two separate, sourced statements from one real owner/window."""
    texts = [
        "松弥原本确定参加周六下午两点的陶艺坊活动。",
        "松弥后来因临时需要照看家里人而退出这次陶艺坊活动，并说明原定时间不用为她更改。",
    ]
    if scenario == "shifts":
        texts = [
            "From 2026-11-02 to 2026-11-08 they are on evening shift and asked not to be scheduled "
            "for daytime appointments during that period; shifts after that are not yet assigned.",
            "They changed 2026-11-06 to a day shift; all other days in the Nov 2–8 period keep "
            "the original evening-shift arrangement.",
        ]
    accepted = [front(h, text, f"{serial}-{index}", actor) for index, text in enumerate(texts)]
    materials = [item[0] for item in accepted]
    prepared = segment_pending(h, segment_seal(h, materials, f"segment:{serial}")[0])
    value = segment_output(prepared, portrait=portrait)
    base = value["results"][0]["memories"][0]
    value["results"][0]["memories"] = [
        dict(
            base,
            text=text,
            kind="agreement" if index == 0 else "experience",
            state="confirmed" if index == 0 else "cancelled",
            source_member_ids=[materials[index]["material_ref"]],
            retention="long" if index == 0 else "short",
        )
        for index, text in enumerate(texts)
    ]
    done, _ = segment_submit(h, prepared, value)
    assert done.status_code == 200, done.text
    refs = done.json()["results"][0]["memory_refs"]
    with h.store.transaction() as db:
        groups = [
            db.execute("SELECT group_id FROM records WHERE id=?", (ref["record_id"],)).fetchone()[0]
            for ref in refs
        ]
    return dict(
        texts=texts,
        materials=materials,
        events=[item[1] for item in accepted],
        prepared=prepared,
        refs=refs,
        groups=groups,
    )


def ranked_prefix(monkeypatch, identities, *, only_prefix=False):
    """Controlled order: the live run did not export its original pre-fit rank."""
    original = read_v2.rank_plans

    def rank(service, prepared, backend_failed):
        original(service, prepared, backend_failed)
        for plan in prepared:
            by_id = {document["id"]: document for document in plan["documents"]}
            first = [by_id[identity] for identity in identities if identity in by_id]
            plan["ranked"] = first + (
                []
                if only_prefix
                else [item for item in plan["ranked"] if item["id"] not in identities]
            )

    monkeypatch.setattr(read_v2, "rank_plans", rank)


@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize("scenario", ["pottery", "shifts"])
def test_current_summary_window_fits_all_companions_and_proves_every_group(
    sync, monkeypatch, limit, scenario
):
    segment_ready(sync)
    saved = summary_pair(sync, scenario=scenario)
    # The changed statement need not survive an individual-document top-k.
    ranked_prefix(monkeypatch, ["fact:" + saved["groups"][0]], only_prefix=True)
    selected = []
    before_final(monkeypatch, lambda items: selected.extend(items[0]["selected"]["group_ids"]))
    sync.calls.clear()
    result = contextual_query(
        sync,
        "这次究竟谁能参加，各自几点？",
        limits=dict(facts=limit, episodes=0),
        budget=dict(bytes=8192, tokens=8192),
    )
    if limit == 1:
        assert result["evidence"] == [] and selected == []
        assert "budget" in result["omissions"]
    else:
        assert [item["unit"]["statement"] for item in result["evidence"]] == saved["texts"]
        assert set(selected) == set(saved["groups"])
        assert {
            dependency["group"]["semantic_group_id"]
            for dependency in result["bundle"]["group_dependencies"]
        } == set(saved["groups"])
        assert snapshot_selectors(sync) == {
            canonical(source_selector(source, event["scope"]))
            for event in saved["events"]
            for source in event["sources"]
        }
        assert result["budget_used"]["bytes"] <= 8192
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("change", ["version", "expire"])
def test_current_summary_window_never_splits_after_discovery(sync, monkeypatch, change):
    segment_ready(sync)
    saved = summary_pair(sync)
    ranked_prefix(monkeypatch, ["fact:" + saved["groups"][0]], only_prefix=True)

    def mutate(items):
        assert set(items[0]["selected"]["group_ids"]) == set(saved["groups"])
        with sync.store.transaction() as db:
            if change == "version":
                identity = saved["refs"][1]["record_id"]
                row = db.execute("SELECT payload FROM records WHERE id=?", (identity,)).fetchone()
                payload = json.loads(row[0])
                payload["record_version"] += 1
                db.execute(
                    "UPDATE records SET version=version+1,payload=? WHERE id=?",
                    (canonical(payload), identity),
                )
            else:
                expiry = db.execute(
                    "SELECT expires_at FROM memory_summaries WHERE group_id=?",
                    (saved["groups"][1],),
                ).fetchone()[0]
                sync.service.clock = lambda: parse_time(expiry) + timedelta(seconds=1)

    before_final(monkeypatch, mutate)
    result = contextual_query(sync, "陶艺坊", limits=dict(facts=8, episodes=0))
    assert result["evidence"] == [] and "stale_source" in result["omissions"]
    assert result["bundle"]["group_dependencies"] == []
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("mode,intent", [("factual", "current"), ("context", "history")])
def test_summary_window_does_not_change_factual_or_history_fitting(sync, monkeypatch, mode, intent):
    segment_ready(sync)
    saved = summary_pair(sync)
    ranked_prefix(monkeypatch, ["fact:" + saved["groups"][0]], only_prefix=True)
    result = contextual_query(
        sync, "陶艺坊", mode=mode, intent=intent, limits=dict(facts=1, episodes=0)
    )
    assert [item["unit"]["statement"] for item in result["evidence"]] == saved["texts"][:1]
    assert verify(sync, result["bundle"]).status_code == 200


def test_summary_packet_omission_does_not_turn_an_old_raw_episode_into_a_complete_answer(
    sync, monkeypatch
):
    segment_ready(sync)
    saved = summary_pair(sync)
    with sync.store.transaction() as db:
        episode = next(
            item
            for item in memory_documents.episode_metadata(sync.service, db, [sync.scope()])
            if item["source_key"] == saved["materials"][0]["source_key"]
        )
    ranked_prefix(monkeypatch, ["fact:" + saved["groups"][0], episode["id"]], only_prefix=True)
    result = contextual_query(sync, "陶艺坊", limits=dict(facts=1, episodes=1))
    assert len(result["evidence"]) == 1 and result["evidence"][0]["kind"] == "episode"
    assert saved["texts"][0] in result["evidence"][0]["text"]
    assert {"budget", "context_only"} <= set(result["omissions"])
    assert result["query_results"][0]["status"] == "incomplete"
    assert result["bundle"]["group_dependencies"] == []
    assert verify(sync, result["bundle"]).status_code == 200


@pytest.mark.parametrize("entry", ["summary", "portrait"])
def test_summary_packet_does_not_bind_a_portrait_or_a_different_window(sync, monkeypatch, entry):
    segment_ready(sync)
    first = summary_pair(sync, portrait="会及时说明安排变化。")
    second = summary_pair(sync, serial="another-activity")
    with sync.store.transaction() as db:
        portrait = db.execute(
            "SELECT group_id FROM memory_summaries WHERE kind='portrait' AND window_id=?",
            (first["prepared"]["segment_window"]["window_id"],),
        ).fetchone()[0]
    first_ids = [portrait] if entry == "portrait" else first["groups"][:1]
    ranked_prefix(
        monkeypatch,
        ["fact:" + identity for identity in [*first_ids, second["groups"][0]]],
        only_prefix=True,
    )
    limit = 1 if entry == "portrait" else 2
    result = contextual_query(sync, "陶艺坊", limits=dict(facts=limit, episodes=0))
    expected = {portrait} if entry == "portrait" else set(first["groups"])
    assert {item["unit"]["semantic_group_id"] for item in result["evidence"]} == expected
    assert "budget" in result["omissions"]
    assert verify(sync, result["bundle"]).status_code == 200


# Actual h03 human texts and committed statements. Authority IDs/times are new
# fixture values, and the returned prefix is not an exported pre-fit ranking.
H03_SELECTION_REPLAY = {
    "sources": [
        (1, "我是松弥。周六去陶艺坊，我确定参加，先约两点。"),
        (2, "我是未央，我要等周五才知道能不能休息。"),
        (0, "我是安朔，我能来，不过我自己三点才能到。"),
        (2, "我休假批了，可以参加，还是跟最初说的时间走。"),
        (1, "我临时要照看家里人，这次退出。你们原定时间不用为我改。"),
    ],
    "results": [
        {
            "person": 1,
            "memories": [
                {
                    "importance": "normal",
                    "kind": "agreement",
                    "operation": "append",
                    "prior_refs": [],
                    "retention": "short",
                    "state": "confirmed",
                    "text": "松弥原本确定参加周六下午两点的陶艺坊活动。",
                    "valid_until": None,
                    "source_positions": [0],
                },
                {
                    "importance": "high",
                    "kind": "experience",
                    "operation": "append",
                    "prior_refs": [],
                    "retention": "short",
                    "state": "cancelled",
                    "text": "松弥后来因临时需要照看家里人而退出这次陶艺坊活动，并说明原定时间不用为她更改。",
                    "valid_until": None,
                    "source_positions": [4],
                },
            ],
            "portrait": "松弥说话直接明确：先确认参加周六下午两点的陶艺坊，后因临时需要照看家人而退出，并主动说明不必为她改动原定时间。",
        },
        {
            "person": 2,
            "memories": [
                {
                    "importance": "normal",
                    "kind": "thought",
                    "operation": "append",
                    "prior_refs": [],
                    "retention": "short",
                    "state": "described",
                    "text": "未央起初表示要等到周五才知道能不能休息，因此陶艺坊是否参加暂未确定。",
                    "valid_until": None,
                    "source_positions": [1],
                },
                {
                    "importance": "high",
                    "kind": "agreement",
                    "operation": "append",
                    "prior_refs": [],
                    "retention": "short",
                    "state": "confirmed",
                    "text": "未央的休假获批，她确定参加陶艺坊，并仍按最初说的时间（下午两点）前往。",
                    "valid_until": None,
                    "source_positions": [3],
                },
            ],
            "portrait": "未央会先说明自己的不确定状态，等休假有结果后再明确答复；休假获批后确定按最初约定的下午两点参加陶艺坊。",
        },
        {
            "person": 0,
            "memories": [
                {
                    "importance": "normal",
                    "kind": "agreement",
                    "operation": "append",
                    "prior_refs": [],
                    "retention": "short",
                    "state": "confirmed",
                    "text": "安朔表示能参加陶艺坊活动，但由于自己的安排要到下午三点才能到场，晚于两点开始的时间。",
                    "valid_until": None,
                    "source_positions": [2],
                }
            ],
            "portrait": "安朔参加陶艺坊活动但说明自己三点才能到，直接交代了到场时间与活动开始时间的差异。",
        },
    ],
    "priority": [
        (
            "summary",
            "安朔表示能参加陶艺坊活动，但由于自己的安排要到下午三点才能到场，晚于两点开始的时间。",
        ),
        (
            "portrait",
            "安朔参加陶艺坊活动但说明自己三点才能到，直接交代了到场时间与活动开始时间的差异。",
        ),
        ("summary", "松弥原本确定参加周六下午两点的陶艺坊活动。"),
        ("summary", "未央的休假获批，她确定参加陶艺坊，并仍按最初说的时间（下午两点）前往。"),
        ("summary", "未央起初表示要等到周五才知道能不能休息，因此陶艺坊是否参加暂未确定。"),
        ("episode", 0),
        ("episode", 3),
    ],
    "budget": {"tokens": 8192, "bytes": 8192},
    "limits": {"facts": 8, "episodes": 8},
    "query": "这次究竟谁能参加，各自几点？",
}


def test_h03_captured_text_reconstruction_keeps_withdrawal_within_original_budget(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("The captured story has three real authors in one group")
    segment_ready(sync)
    actors = [0, owner(sync), owner(sync)]
    replay = H03_SELECTION_REPLAY
    materials = [
        front(sync, text, f"h03-replay-{index}", actors[person])[0]
        for index, (person, text) in enumerate(replay["sources"])
    ]
    prepared = segment_pending(sync, segment_seal(sync, materials, "segment:h03-replay")[0])
    output = segment_output(prepared)
    actor_index = {sync.scope(actor)["person_id"]: index for index, actor in enumerate(actors)}
    captured = {result["person"]: result for result in replay["results"]}
    for result in output["results"]:
        original = captured[actor_index[result["subject_person_id"]]]
        result["memories"] = []
        for memory in original["memories"]:
            memory = dict(memory)
            positions = memory.pop("source_positions")
            memory["source_member_ids"] = [materials[index]["material_ref"] for index in positions]
            result["memories"].append(memory)
        result["portrait"] = original["portrait"]
    done, _ = segment_submit(sync, prepared, output)
    assert done.status_code == 200, done.text
    assert sum(len(result["memory_refs"]) for result in done.json()["results"]) == 5
    with sync.store.transaction() as db:
        group_by_text = {
            json.loads(row["payload"])["statement"]: row["group_id"]
            for row in db.execute(
                "SELECT r.group_id,r.payload FROM records r JOIN groups g ON g.id=r.group_id "
                "WHERE g.state='active'"
            )
        }
        episodes = {
            row["source_key"]: row["id"]
            for row in memory_documents.episode_metadata(
                sync.service, db, [sync.scope(actor) for actor in actors]
            )
        }
    order = [
        episodes[materials[value]["source_key"]]
        if kind == "episode"
        else "fact:" + group_by_text[value]
        for kind, value in replay["priority"]
    ]
    # This uses the observed returned prefix as a controlled stress order.
    # It does not claim to recover the missing original hybrid rank/scores.
    ranked_prefix(monkeypatch, order, only_prefix=True)
    selected = []
    before_final(monkeypatch, lambda items: selected.extend(items[0]["selected"]["group_ids"]))
    sync.calls.clear()
    result = contextual_query(
        sync, replay["query"], budget=replay["budget"], limits=replay["limits"]
    )
    withdrawal = next(
        memory["text"]
        for target in replay["results"]
        for memory in target["memories"]
        if memory["state"] == "cancelled"
    )
    assert withdrawal in [
        item["unit"]["statement"] for item in result["evidence"] if "unit" in item
    ]
    assert group_by_text[withdrawal] in selected
    assert group_by_text[withdrawal] in {
        dependency["group"]["semantic_group_id"]
        for dependency in result["bundle"]["group_dependencies"]
    }
    assert result["budget_used"]["bytes"] <= replay["budget"]["bytes"]
    assert "context_only" in result["omissions"]
    assert verify(sync, result["bundle"]).status_code == 200
