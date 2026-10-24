"""Contextual summaries through actual Memory HTTP, TLS owners and guarded storage.

Only the upstream window owner and model output are synthetic. The existing
SyncHarness (including pg_sync delegation) owns all business transactions.
"""

import copy
import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest
from test_candidate_extraction import PATH
from test_memory_v2 import candidate, post, request, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import Fault, canonical, fingerprint, parse_time


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare(h):
    setup(h)
    h.summary_replies = {}

    def check(body):
        manifest = body["manifest"]
        invalid = []
        turns = {turn["turn_id"]: turn for turn in h.turns}
        for member in manifest["members"]:
            turn = turns.get(member["turn_id"])
            valid = turn is not None and turn["scope"] == member["source_scope"]
            if member["role"] == "user":
                ref = member["reference"]
                valid = (
                    valid
                    and ref["input_revision"] == turn["input_revision"]
                    and ref["source"] in turn["input_sources"]
                )
                key = {
                    key: value
                    for key, value in ref["source"]["message_key"].items()
                    if key != "revision"
                }
                physical = next((row for row in h.physicals if row["key"] == key), None)
                valid = valid and physical is not None and physical["state"] == "active"
                content = h.test_contents.get(canonical(key))
                valid = (
                    valid
                    and content is not None
                    and text_hash(
                        "\n".join(
                            part["text"] for part in content["parts"] if part["kind"] == "text"
                        )
                    )
                    == member["content_sha256"]
                )
            else:
                reply = h.summary_replies.get(member["reference"]["reply_id"])
                valid = (
                    valid
                    and reply is not None
                    and reply["state"] == "sent"
                    and reply["reference"] == member["reference"]
                    and text_hash(reply["text"]) == member["content_sha256"]
                )
            if not valid:
                invalid.append(member["member_id"])
        return dict(
            schema_version=1,
            request_id=body["request_id"],
            request_digest=fingerprint(body),
            window_id=manifest["window_id"],
            state="invalid" if invalid else "valid",
            invalid_member_ids=invalid,
            checked_at="2026-10-24T00:00:00Z",
        )

    h.window_checker = check


def window(h, jobs, *, context=(), assistant=()):
    members, bodies, new_turns = [], [], []
    with h.store.transaction() as db:
        for purpose, ids in (("context", context), ("new", jobs)):
            for identity in ids:
                event = json.loads(
                    db.execute("SELECT event FROM jobs WHERE id=?", (identity,)).fetchone()[0]
                )
                if purpose == "new":
                    new_turns.append(
                        dict(turn_id=event["aggregate_id"], input_revision=event["input_revision"])
                    )
                for index, source in enumerate(event["sources"]):
                    key = {
                        key: value
                        for key, value in source["message_key"].items()
                        if key != "revision"
                    }
                    content = h.test_contents[canonical(key)]
                    text = "\n".join(
                        part["text"] for part in content["parts"] if part["kind"] == "text"
                    )
                    account = content["author"]
                    binding = h.service._binding(db, account)
                    member = dict(
                        member_id=f"window-member:{event['aggregate_id']}:{index}",
                        turn_id=event["aggregate_id"],
                        turn_sequence=event["turn_sequence"],
                        role="user",
                        speaker=dict(
                            kind="person", person_id=event["scope"]["person_id"], account=account
                        ),
                        source_scope=event["scope"],
                        binding_version=binding["version"],
                        purpose=purpose,
                        reference=dict(input_revision=event["input_revision"], source=source),
                        observed_at=content["sent_at"],
                        content_sha256=text_hash(text),
                        memory_dependencies=dict(
                            group_dependencies=[], episode_dependencies=[], source_dependencies=[]
                        ),
                    )
                    members.append(member)
                    bodies.append(dict(member_id=member["member_id"], text=text))
        event = json.loads(
            db.execute("SELECT event FROM jobs WHERE id=?", (jobs[0],)).fetchone()[0]
        )
    for member, text in assistant:
        members.append(copy.deepcopy(member))
        bodies.append(dict(member_id=member["member_id"], text=text))
    channel = copy.deepcopy(event["sources"][0]["message_key"]["channel"])
    manifest = dict(
        schema_version=1,
        scope=event["scope"],
        channel=channel,
        anchor=new_turns[-1],
        new_turns=new_turns,
        members=members,
        coverage=dict(complete=True, omitted_before=False, omitted_after=False),
    )
    manifest["window_id"] = fingerprint(manifest)
    return dict(manifest=manifest, members=bodies)


def pending(h, jobs, supplied):
    response = h.post(
        PATH,
        dict(
            job_id=jobs[0],
            job_ids=list(jobs),
            output=None,
            supported_extraction_schemas=[5],
            window=supplied,
        ),
    )
    assert response.status_code == 200, response.text
    value = response.json()
    h.contracts.validate("memory-extraction#process_response", value)
    return value


def submit(h, prepared, summary, *, portrait=None, importance="normal"):
    body = dict(
        job_id=prepared["job_ids"][0],
        job_ids=prepared["job_ids"],
        batch_ref=prepared["batch_ref"],
        extraction_schema=5,
        output=dict(summary=summary, portrait=portrait, importance=importance),
    )
    response = h.post(PATH, body)
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-extraction#process_response", response.json())
    return response.json(), body


def query(h, text, *, intent="current", kinds=("facts", "episodes")):
    payload = request(h, text)
    payload["queries"][0].update(intent=intent, kinds=list(kinds))
    response = post(h, "query", payload)
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-context-v2#query_response", response.json())
    return response.json()


def test_context_window_captures_current_job_before_summary_and_replays_exactly(sync):
    prepare(sync)
    old = candidate(sync, "昨天一起讨论了海边旅行。", 1, capture=False)[0]
    current = candidate(sync, "后来改成下个月去。", 2, capture=False)[0]
    jobs = [current]
    supplied = window(sync, jobs, context=[old])
    first = pending(sync, jobs, supplied)
    assert first["state"] == "pending" and first["portrait_input"] is None
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    done, body = submit(sync, first, "先讨论海边旅行，后来把计划改到下个月。")
    result = done["result"]
    assert result["covered_job_ids"] == jobs and result["cursor"] == dict(
        window_id=supplied["manifest"]["window_id"], job_ids=jobs
    )
    assert result["experience_state"] == "saved" and result["summary_state"] == "committed"
    assert result["portrait"] == dict(state="not_requested", record_refs=[])
    assert pending(sync, jobs, None) == done
    assert sync.post(PATH, body).json() == done
    body["output"]["summary"] += "不同文本"
    assert sync.post(PATH, body).status_code == 409
    found = query(sync, "海边旅行")
    assert any(item["kind"] == "summary" for item in found["evidence"])
    assert not any(
        item["kind"] in {"current_fact", "historical_fact"} for item in found["evidence"]
    )
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1
        assert db.execute("SELECT state FROM jobs WHERE id=?", (old,)).fetchone()[0] == "pending"
        assert db.execute("SELECT 1 FROM write_ledger WHERE job_id=?", (old,)).fetchone() is None


def test_current_job_queries_its_frozen_window_and_real_receipt(sync):
    prepare(sync)
    jobs = [candidate(sync, "一起散步聊天。", 1, capture=False)[0]]
    first = pending(sync, jobs, window(sync, jobs))
    assert pending(sync, jobs, None) == first
    done, _ = submit(sync, first, "最近一起散步。")
    assert pending(sync, jobs, None) == done


@pytest.mark.parametrize(
    "race", [False, True], ids=["same-window-recovery", "same-job-already-claimed"]
)
def test_before_claim_unknown_looks_up_unclaimed_then_resends_only_frozen_window(sync, race):
    prepare(sync)
    jobs = [candidate(sync, "一起讨论河边散步。", 1, capture=False)[0]]
    supplied = window(sync, jobs)
    frozen = canonical(supplied)
    body = dict(
        job_id=jobs[0], job_ids=jobs, output=None, supported_extraction_schemas=[5], window=supplied
    )
    sync.status["window_check"] = 503
    failed = sync.post(PATH, body)
    assert failed.status_code == 503 and failed.json()["execution_state"] == "unknown", failed.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_batches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 1
        )
    sync.calls.clear()
    lookup = pending(sync, jobs, None)
    assert lookup == dict(state="unclaimed", extraction_schema=5, job_ids=jobs)
    assert sync.calls == []  # No source/owner retry or fabricated completion on lookup.
    sync.status.pop("window_check")
    if race:
        competing = pending(sync, jobs, supplied)
    resent = pending(sync, jobs, supplied)
    assert resent["state"] == "pending" and resent["window_id"] == supplied["manifest"]["window_id"]
    if race:
        assert resent == competing
    done, _ = submit(sync, resent, "讨论过河边散步。")
    assert pending(sync, jobs, None) == done
    assert done["result"]["covered_job_ids"] == jobs
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1
    assert canonical(supplied) == frozen


@pytest.mark.parametrize(
    "broken",
    [
        "body_hash",
        "body_order",
        "missing_new",
        "new_revision",
        "wrong_owner",
        "extra_job",
        "assistant_without_inputs",
    ],
)
def test_window_does_not_claim_uncovered_or_unbound_jobs(sync, broken):
    prepare(sync)
    old = candidate(sync, "这是解释上下文。", 1, capture=False)[0]
    jobs = [candidate(sync, "这是需要完整保存的新消息。", 2, capture=False)[0]]
    supplied = window(sync, jobs, context=[old])
    if broken == "body_hash":
        supplied["members"][0]["text"] += "伪造"
    elif broken == "body_order":
        supplied["members"].reverse()
    elif broken == "missing_new":
        supplied["members"].pop()
        supplied["manifest"]["members"].pop()
    elif broken == "new_revision":
        supplied["manifest"]["new_turns"][0]["input_revision"] += 1
    elif broken == "wrong_owner":
        supplied["manifest"]["members"][0]["source_scope"]["actor_id"] = "actor:b"
    elif broken == "extra_job":
        jobs.append(old)
    else:
        member, text = assistant_member(sync, supplied["manifest"]["members"][0])
        member["turn_id"] = "absent-turn"
        supplied["manifest"]["members"].append(member)
        supplied["members"].append(dict(member_id=member["member_id"], text=text))
    manifest = supplied["manifest"]
    manifest["window_id"] = fingerprint(
        {key: value for key, value in manifest.items() if key != "window_id"}
    )
    response = sync.post(
        PATH,
        dict(
            job_id=jobs[0],
            job_ids=jobs,
            output=None,
            supported_extraction_schemas=[5],
            window=supplied,
        ),
    )
    assert response.status_code in {400, 403, 409}, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_batches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0


def assistant_member(h, human, text="好的，我们之后继续聊旅行。", *, memory_dependencies=None):
    turn = next(turn for turn in h.turns if turn["turn_id"] == human["turn_id"])
    reply_id = "summary-reply:" + human["turn_id"]
    reference = dict(
        reply_id=reply_id,
        segment_sequence=1,
        result_version=1,
        receipt_digest=fingerprint(
            dict(reply_id=reply_id, state="sent", channel_message_ids=["sent:" + reply_id])
        ),
    )
    h.summary_replies[reply_id] = dict(state="sent", reference=reference, text=text)
    turn["reply_ids"] = [*turn["reply_ids"], reply_id]
    member = dict(
        human,
        member_id="assistant:" + human["member_id"],
        role="assistant",
        speaker=dict(kind="actor", actor_id=human["source_scope"]["actor_id"]),
        purpose="context",
        reference=reference,
        content_sha256=text_hash(text),
    )
    if memory_dependencies is not None:
        member["memory_dependencies"] = memory_dependencies
    return member, text


def test_human_and_bot_context_remain_different_authors_and_dependencies(sync):
    prepare(sync)
    old = candidate(sync, "上次讨论过海边旅行。", 1, capture=False)[0]
    old_window = window(sync, [old])
    assistant = assistant_member(sync, old_window["manifest"]["members"][0])
    new = candidate(sync, "改成下个月再去。", 2, capture=False)[0]
    supplied = window(sync, [new], context=[old], assistant=[assistant])
    prepared = pending(sync, [new], supplied)
    done, _ = submit(sync, prepared, "对话先讨论旅行，BOT回应后，用户把计划改到下个月。")
    group = done["result"]["group_ids"][0]
    with sync.store.transaction() as db:
        saved = json.loads(
            db.execute(
                "SELECT manifest FROM memory_summaries WHERE group_id=?", (group,)
            ).fetchone()[0]
        )
        assert {member["role"] for member in saved["members"]} == {"user", "assistant"}
        assert (
            db.execute(
                "SELECT COUNT(*) FROM memory_summary_sources WHERE group_id=?", (group,)
            ).fetchone()[0]
            == 2
        )
    # Routine reading batches captured turn facts; no window RPC per old summary.
    sync.calls.clear()
    result = query(sync, "海边旅行")
    assert any(item["kind"] == "summary" for item in result["evidence"])
    assert not any(kind == "window_check" for kind, _, _ in sync.calls)
    selected_turns = {
        identity
        for kind, body, _ in sync.calls
        if kind == "snapshot"
        for identity in body["turn_ids"]
    }
    assert {member["turn_id"] for member in saved["members"]} <= selected_turns
    # A changed fact at the same owner sequence is equivocation, not a valid
    # lifecycle event. Preserve the authority rejection before exercising a
    # real versioned source withdrawal below.
    turn = next(turn for turn in sync.turns if turn["turn_id"] == assistant[0]["turn_id"])
    replies = turn["reply_ids"]
    turn["reply_ids"] = []
    rejected = post(sync, "query", request(sync, "海边旅行"))
    assert rejected.status_code == 503 and rejected.json()["code"] == "dependency_unavailable"
    turn["reply_ids"] = replies
    withdraw_source(sync, old)
    result = query(sync, "海边旅行")
    assert all(
        item.get("unit", {}).get("semantic_group_id") != group for item in result["evidence"]
    )


def test_portrait_needs_multiple_experiences_and_retains_prior_leaf_sources(sync):
    prepare(sync)
    first = candidate(sync, "这周喜欢在河边散步。", 1, capture=False)[0]
    prepared = pending(sync, [first], window(sync, [first]))
    assert prepared["portrait_input"] is None
    one, _ = submit(sync, prepared, "最近会去河边散步。")
    second = candidate(sync, "周末也想去公园走走。", 2, capture=False)[0]
    prepared = pending(sync, [second], window(sync, [second]))
    assert prepared["portrait_input"]["summary_refs"] == one["result"]["summary_record_refs"]
    two, _ = submit(sync, prepared, "周末计划去公园散步。", portrait="最近倾向在户外散步放松。")
    assert two["result"]["portrait"]["state"] == "committed"
    third = candidate(sync, "今天继续去河边走走。", 3, capture=False)[0]
    prepared = pending(sync, [third], window(sync, [third]))
    assert prepared["portrait_input"]["current_text"] == "最近倾向在户外散步放松。"
    three, _ = submit(
        sync, prepared, "今天又去河边散步。", portrait="多次经历显示最近常通过散步放松。"
    )
    assert three["result"]["portrait"]["state"] == "committed"
    groups = [*two["result"]["group_ids"], *three["result"]["group_ids"]]
    # Forgetting the first summary's own source closes all later derived uses,
    # including the old portrait text that was supplied to the third batch.
    revision = sync.revision(one["result"]["record_ids"][0])
    response = sync.post("memory/revise", revision)
    assert response.status_code == 200, response.text
    with sync.store.transaction() as db:
        assert all(
            db.execute("SELECT state FROM groups WHERE id=?", (group,)).fetchone()[0] != "active"
            for group in groups
        )
    assert not any(
        item["kind"] in {"summary", "portrait"}
        for item in query(sync, "户外散步", intent="mixed")["evidence"]
    )


def test_portrait_cas_pending_does_not_roll_back_saved_summary(sync):
    prepare(sync)
    first = candidate(sync, "周末一起散步。", 1, capture=False)[0]
    submit(sync, pending(sync, [first], window(sync, [first])), "周末散步。")
    jobs = [candidate(sync, "最近继续散步聊天。", index, capture=False)[0] for index in (2, 3)]
    prepared = [pending(sync, [job], window(sync, [job])) for job in jobs]
    submit(sync, prepared[1], "第二段散步经历。", portrait="喜欢边散步边聊天。")
    done, _ = submit(
        sync, prepared[0], "第一段散步经历仍真实保存。", portrait="另一个过时画像提议。"
    )
    assert done["result"]["summary_state"] == "committed"
    assert done["result"]["portrait"] == dict(state="pending", record_refs=[])
    assert pending(sync, [jobs[0]], None) == done


@pytest.mark.parametrize("importance,days", [("low", 7), ("normal", 30), ("high", 180)])
def test_natural_expiry_is_routine_only_not_suppression_or_original_deletion(
    sync, importance, days
):
    prepare(sync)
    job = candidate(sync, "那天在河边散步聊天。", 1, capture=False)[0]
    done, _ = submit(
        sync,
        pending(sync, [job], window(sync, [job])),
        "一次河边散步聊天的经历。",
        importance=importance,
    )
    with sync.store.transaction() as db:
        stored = db.execute(
            "SELECT * FROM memory_summaries WHERE group_id=?", (done["result"]["group_ids"][0],)
        ).fetchone()
        assert parse_time(stored["expires_at"]) - parse_time(stored["recorded_at"]) == timedelta(
            days=days
        )
        before = db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0]
    sync.service.clock = lambda: parse_time(stored["expires_at"]) + timedelta(seconds=1)
    assert query(sync, "河边散步")["evidence"] == []
    historical = query(sync, "河边散步", intent="history")
    assert {item["kind"] for item in historical["evidence"]} >= {"summary", "episode"}
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == before
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0


def test_context_repetition_does_not_renew_raw_episode_lifetime(sync):
    prepare(sync)
    old = candidate(sync, "第一次去河边散步。", 1, capture=False)[0]
    first, _ = submit(
        sync, pending(sync, [old], window(sync, [old])), "第一次河边散步。", importance="low"
    )
    with sync.store.transaction() as db:
        expiry = db.execute(
            "SELECT expires_at FROM memory_summaries WHERE group_id=?",
            (first["result"]["group_ids"][0],),
        ).fetchone()[0]
        old_source = db.execute(
            "SELECT source_key FROM memory_summary_sources WHERE group_id=? AND purpose='new'",
            (first["result"]["group_ids"][0],),
        ).fetchone()[0]
    sync.service.clock = lambda: parse_time(expiry) + timedelta(seconds=1)
    new = candidate(sync, "最近在公园走走。", 2, capture=False)[0]
    submit(
        sync,
        pending(sync, [new], window(sync, [new], context=[old])),
        "最近散步，想起第一次去河边。",
        importance="high",
    )
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT expires_at FROM memory_source_lifecycle WHERE source_key=?", (old_source,)
            ).fetchone()[0]
            == expiry
        )
    assert any(
        item["kind"] == "episode" and "第一次" in item["text"]
        for item in query(sync, "河边散步", intent="history")["evidence"]
    )
    assert not any(
        item["kind"] == "episode" and "第一次" in item["text"]
        for item in query(sync, "河边散步")["evidence"]
    )


def test_skip_still_saves_original_and_applies_its_lifecycle(sync):
    prepare(sync)
    job = candidate(sync, "今天在河边散步。", 1, capture=False)[0]
    done, _ = submit(sync, pending(sync, [job], window(sync, [job])), "", importance="low")
    assert done["result"]["state"] == "skipped" and done["result"]["experience_state"] == "saved"
    assert done["result"]["summary_record_refs"] == []
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM memory_source_lifecycle").fetchone()[0] == 1


def test_context_forget_before_commit_rejects_the_late_derived_write(sync):
    prepare(sync)
    old = candidate(sync, "上次去河边散步。", 1, capture=False)[0]
    first, _ = submit(sync, pending(sync, [old], window(sync, [old])), "以前去过河边散步。")
    new = candidate(sync, "继续讨论散步。", 2, capture=False)[0]
    prepared = pending(sync, [new], window(sync, [new], context=[old]))
    revision = sync.revision(first["result"]["record_ids"][0])
    assert sync.post("memory/revise", revision).status_code == 200
    body = dict(
        job_id=new,
        job_ids=[new],
        batch_ref=prepared["batch_ref"],
        extraction_schema=5,
        output=dict(summary="保留被忘记的河边经历", portrait=None),
    )
    response = sync.post(PATH, body)
    assert response.status_code == 200 and response.json()["state"] == "invalidated", response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT 1 FROM write_ledger WHERE job_id=?", (new,)).fetchone() is None


def test_owner_failures_do_not_return_saved_or_advance_the_cursor(sync):
    prepare(sync)
    job = candidate(sync, "一起散步。", 1, capture=False)[0]
    prepared = pending(sync, [job], window(sync, [job]))
    sync.status["window_check"] = 503
    response = sync.post(
        PATH,
        dict(
            job_id=job,
            job_ids=[job],
            batch_ref=prepared["batch_ref"],
            extraction_schema=5,
            output=dict(summary="散步经历。", portrait=None),
        ),
    )
    assert response.status_code == 503, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_named_summary_context_keeps_actual_owner_without_faking_name_resolution(sync):
    prepare(sync)
    job = candidate(sync, "我叫林叶，周末喜欢去河边散步。", 1, capture=False)[0]
    submit(sync, pending(sync, [job], window(sync, [job])), "林叶说周末喜欢在河边散步。")
    result = query(sync, "林叶家之前提过什么河边散步经历？", kinds=["facts"])
    summaries = [item for item in result["evidence"] if item["kind"] == "summary"]
    assert summaries and all(
        item["owner_scope"] == sync.scope() and item["subject_person_id"] == sync.person
        for item in summaries
    )
    constraint = result["bundle"]["selection_constraints"][0]
    assert "unique_owner" not in constraint["proof_kinds"]
    assert not any(
        target["kind"] == "named"
        and (target["status"] == "resolved" or target["witness_group_ids"])
        for target in constraint["owner_resolution"]["targets"]
    )
    # The original factual query's grammar may classify this as self/room;
    # contextual always-incomplete semantics belongs to the separate read mode.


@pytest.mark.parametrize("owner_budget", [2, 17])
def test_summary_leaf_owners_are_authorized_and_budgeted_before_selection(
    sync, monkeypatch, owner_budget
):
    from test_group_context import owner
    from test_memory_v2_boundaries import verify

    from tianshu_memory.memory_context import read_v2

    if sync.scope()["audience"] != "group":
        pytest.skip("This window uses different authors in the same authorized group")
    prepare(sync)
    others = [owner(sync), owner(sync)]
    old = [
        candidate(sync, "一起聊过河边散步。", index, actor, capture=False)[0]
        for index, actor in enumerate(others, 1)
    ]
    new = candidate(sync, "我也聊过河边散步。", 3, capture=False)[0]
    done, _ = submit(
        sync,
        pending(sync, [new], window(sync, [new], context=old)),
        "大家一起聊过河边散步的经历。",
    )
    group = done["result"]["group_ids"][0]
    monkeypatch.setattr(read_v2, "MAX_BUNDLE_OWNERS", owner_budget)
    result = query(sync, "我之前说过什么河边散步经历？")
    selected = any(
        item.get("unit", {}).get("semantic_group_id") == group for item in result["evidence"]
    )
    assert selected == (owner_budget == 17)
    assert any(item["kind"] == "episode" for item in result["evidence"])
    bundle = result["bundle"]
    owners = {canonical(entry["scope"]) for entry in bundle["scope_checks"]}
    leaf_owners = {canonical(entry["owner_scope"]) for entry in bundle["source_dependencies"]}
    assert leaf_owners <= owners and len(owners) <= owner_budget
    if selected:
        assert owners == {canonical(sync.scope(actor)) for actor in [0, *others]}
    else:
        assert "budget" in result["query_results"][0]["omissions"]
        assert owners == {canonical(sync.scope())}
    assert verify(sync, bundle).status_code == 200


def test_source_window_validation_uses_batched_turns_not_one_rpc_per_window(sync):
    prepare(sync)
    for index in range(1, 4):
        job = candidate(sync, "河边散步经历。", index, capture=False)[0]
        submit(sync, pending(sync, [job], window(sync, [job])), f"第{index}次河边散步。")
    sync.calls.clear()
    result = query(sync, "河边散步", kinds=["facts"])
    assert result["evidence"]
    assert not any(kind == "window_check" for kind, _, _ in sync.calls)
    assert len([kind for kind, _, _ in sync.calls if kind == "snapshot"]) == 1
    assert len(next(body["turn_ids"] for kind, body, _ in sync.calls if kind == "snapshot")) == 3
    limited = request(sync, "河边散步")
    limited["queries"][0]["kinds"] = ["facts"]
    limited["limits"]["facts"] = 1
    response = post(sync, "query", limited)
    assert response.status_code == 200, response.text
    assert len(response.json()["evidence"]) == 1
    assert "budget" in response.json()["query_results"][0]["omissions"]


def test_late_store_failure_rolls_back_all_summary_and_job_receipts(sync, monkeypatch):
    prepare(sync)
    jobs = [candidate(sync, "一起讨论散步。", 1, capture=False)[0]]
    prepared = pending(sync, jobs, window(sync, jobs))
    emit = sync.service._emit

    def failed(db, kind, payload):
        if kind == "memory.candidate_completed":
            raise sqlite3.OperationalError("synthetic transaction failure after first receipt")
        return emit(db, kind, payload)

    monkeypatch.setattr(sync.service, "_emit", failed)
    body = dict(
        job_id=jobs[0],
        job_ids=jobs,
        batch_ref=prepared["batch_ref"],
        extraction_schema=5,
        output=dict(summary="讨论过散步。", portrait=None),
    )
    response = sync.post(PATH, body)
    assert response.status_code == 503, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 1
        )
        assert {row[0] for row in db.execute("SELECT state FROM jobs")} == {"pending"}
    monkeypatch.setattr(sync.service, "_emit", emit)
    recovered = sync.post(PATH, body)
    assert recovered.status_code == 200 and recovered.json()["state"] == "completed", recovered.text


def test_bot_context_cannot_launder_a_forgotten_memory_dependency(sync):
    prepare(sync)
    first = candidate(sync, "上次去过海边。", 1, capture=False)[0]
    done, _ = submit(sync, pending(sync, [first], window(sync, [first])), "去海边的经历。")
    recalled = query(sync, "海边", kinds=["facts"])["bundle"]
    context_job = candidate(sync, "谢谢提醒。", 2, capture=False)[0]
    context_window = window(sync, [context_job])
    assistant = assistant_member(
        sync,
        context_window["manifest"]["members"][0],
        "你上次去过海边。",
        memory_dependencies={
            key: recalled[key]
            for key in ("group_dependencies", "episode_dependencies", "source_dependencies")
        },
    )
    revision = sync.revision(done["result"]["record_ids"][0])
    assert sync.post("memory/revise", revision).status_code == 200
    new = candidate(sync, "我们继续聊天。", 3, capture=False)[0]
    supplied = window(sync, [new], context=[context_job], assistant=[assistant])
    response = sync.post(
        PATH,
        dict(
            job_id=new,
            job_ids=[new],
            output=None,
            supported_extraction_schemas=[5],
            window=supplied,
        ),
    )
    assert response.status_code == 200 and response.json()["state"] == "context_changed", (
        response.text
    )
    sync.contracts.validate("memory-extraction#process_response", response.json())
    assert response.json()["window_id"] == supplied["manifest"]["window_id"]
    assert response.json()["invalid_context_member_ids"] == [assistant[0]["member_id"]]
    with sync.store.transaction() as db:
        assert db.execute("SELECT 1 FROM memory_batches WHERE job_id=?", (new,)).fetchone() is None
        assert db.execute("SELECT state FROM jobs WHERE id=?", (new,)).fetchone()[0] == "pending"
    clean = pending(sync, [new], window(sync, [new]))
    done, _ = submit(sync, clean, "之后继续聊天。")
    assert done["result"]["experience_state"] == "saved"


def test_summary_and_portrait_vectors_use_the_existing_index_worker(sync):
    from test_query_vector_boundary import BoundedVectors

    from tianshu_memory.memory_reindex import MemoryReindexer
    from tianshu_memory.retrieval import Retrieval

    prepare(sync)
    first = candidate(sync, "周末一起散步。", 1, capture=False)[0]
    submit(sync, pending(sync, [first], window(sync, [first])), "周末散步。")
    second = candidate(sync, "今天也去散步。", 2, capture=False)[0]
    done, _ = submit(
        sync,
        pending(sync, [second], window(sync, [second])),
        "今天继续散步。",
        portrait="最近会通过散步放松。",
    )
    backend = BoundedVectors()
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    indexer = MemoryReindexer(sync.service)
    for _ in range(8):
        if indexer.index_step(batch_size=4)["state"] == "complete":
            break
    else:
        pytest.fail("the existing index worker did not finish this bounded corpus")
    with sync.store.transaction() as db:
        for group in done["result"]["group_ids"]:
            document = db.execute(
                "SELECT * FROM memory_documents WHERE group_id=?", (group,)
            ).fetchone()
            assert document["kind"] in {"summary", "portrait"}
            assert db.execute(
                "SELECT 1 FROM memory_vectors WHERE document_id=? AND content_hash=?",
                (document["id"], document["content_hash"]),
            ).fetchone()
    assert backend.document_batches


def test_existing_memory_migration_installs_guarded_summary_tables_once(sync):
    from tianshu_memory.consolidation_migration import TABLES

    prepare(sync)
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT value FROM metadata WHERE key='memory_consolidation_schema'"
            ).fetchone()[0]
            == "1"
        )
        for table in TABLES:
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
    backup = sync.directory / "unused-already-consolidated.sqlite"
    result = sync.store.migrate_memory(backup)
    assert result["already_installed"] and not backup.exists()


@pytest.mark.parametrize("budget", [1, 2])
def test_consolidation_omits_whole_prior_inputs_and_can_start_a_new_bounded_basis(
    sync, monkeypatch, budget
):
    from tianshu_memory import consolidation

    # A small budget exercises the same complete-leaf rule with real sources,
    # without manufacturing hundreds of jobs or changing production's 256 limit.
    monkeypatch.setattr(consolidation, "SOURCE_BUDGET", budget)
    prepare(sync)
    prepared_values = []
    for index in (1, 2, 3):
        job = candidate(sync, f"第{index}次散步聊天。", index, capture=False)[0]
        prepared = pending(sync, [job], window(sync, [job]))
        prepared_values.append(prepared)
        done, _ = submit(
            sync,
            prepared,
            f"第{index}次散步经历。",
            portrait="最近喜欢散步。" if prepared["portrait_input"] else None,
        )
        with sync.store.transaction() as db:
            for group in done["result"]["group_ids"]:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM memory_summary_sources WHERE group_id=?", (group,)
                    ).fetchone()[0]
                    <= budget
                )
    if budget == 1:
        assert all(prepared["portrait_input"] is None for prepared in prepared_values)
    else:
        assert prepared_values[1]["portrait_input"] is not None
        # Current portrait has two complete source leaves; adding the third
        # source would exceed budget, so rebuild without its text or partial basis.
        assert prepared_values[2]["portrait_input"]["current_text"] == ""
        assert len(prepared_values[2]["portrait_input"]["summary_refs"]) == 1


def withdraw_source(h, job):
    with h.store.transaction() as db:
        event = json.loads(db.execute("SELECT event FROM jobs WHERE id=?", (job,)).fetchone()[0])
    key = {
        key: value for key, value in event["sources"][0]["message_key"].items() if key != "revision"
    }
    physical = next(item for item in h.physicals if item["key"] == key)
    physical.update(
        state="withdrawn",
        kind="retract",
        revision=physical["revision"] + 1,
        physical_receipt_id="withdrawn:" + job,
        content_digest="b" * 64,
    )
    h.core_head["sequence"] += 1


def test_unclaimed_schema5_job_survives_unrelated_context_withdrawal(sync):
    prepare(sync)
    old = candidate(sync, "曾经讨论海边旅行。", 1, capture=False)[0]
    submit(sync, pending(sync, [old], window(sync, [old])), "讨论过海边旅行。")
    new = candidate(sync, "今天在公园散步。", 2, capture=False)[0]
    supplied = window(sync, [new], context=[old])
    withdraw_source(sync, old)
    response = sync.post(
        PATH,
        dict(
            job_id=new,
            job_ids=[new],
            output=None,
            supported_extraction_schemas=[5],
            window=supplied,
        ),
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    sync.contracts.validate("memory-extraction#process_response", changed)
    assert (
        changed["state"] == "context_changed"
        and changed["window_id"] == supplied["manifest"]["window_id"]
    )
    assert changed["invalid_context_member_ids"] == [
        supplied["manifest"]["members"][0]["member_id"]
    ]
    with sync.store.transaction() as db:
        assert tuple(
            db.execute("SELECT state,extraction_schema FROM jobs WHERE id=?", (new,)).fetchone()
        ) == ("pending", 5)
        assert db.execute("SELECT 1 FROM memory_batches WHERE job_id=?", (new,)).fetchone() is None
    final, _ = submit(sync, pending(sync, [new], window(sync, [new])), "今天去公园散步。")
    assert final["result"]["covered_job_ids"] == [new]


def test_actual_new_input_withdrawal_is_not_optional_context_recovery(sync):
    prepare(sync)
    new = candidate(sync, "需要撤回的新输入。", 1, capture=False)[0]
    supplied = window(sync, [new])
    withdraw_source(sync, new)
    response = sync.post(
        PATH,
        dict(
            job_id=new,
            job_ids=[new],
            output=None,
            supported_extraction_schemas=[5],
            window=supplied,
        ),
    )
    assert response.status_code in {409, 503}, response.text
    assert response.json().get("state") != "context_changed"
    with sync.store.transaction() as db:
        assert db.execute("SELECT state FROM jobs WHERE id=?", (new,)).fetchone()[0] != "pending"
        assert db.execute("SELECT 1 FROM memory_batches WHERE job_id=?", (new,)).fetchone() is None
        assert db.execute("SELECT 1 FROM write_ledger WHERE job_id=?", (new,)).fetchone() is None


def test_context_summary_cannot_be_republished_as_a_legacy_single_author_profile(sync):
    from test_user_actions import EXPIRES, execute, setup_user

    if sync.scope()["audience"] != "self_private":
        pytest.skip("The legacy exact-evidence profile draft accepts private records")
    prepare(sync)
    job = candidate(sync, "一起讨论过户外旅行。", 1, capture=False)[0]
    done, _ = submit(sync, pending(sync, [job], window(sync, [job])), "讨论户外旅行的经历。")
    action = dict(
        operation="prepare_evidence_profile",
        origin={"assertion_ref": "synthetic-viewer:0"},
        records=done["result"]["summary_record_refs"],
        field_key="interest.travel",
        sharing="public_preference",
        conversation_id=None,
        expires_at=EXPIRES,
    )
    with pytest.raises(Fault) as rejected:
        execute(setup_user(sync), action)
    assert rejected.value.code == "invalid_input"
