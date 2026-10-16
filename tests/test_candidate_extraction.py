"""Real Memory and TLS source owners with explicitly synthetic source/model content."""

import copy
from contextlib import contextmanager

import pytest
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.workflow import TrustedWorkflow

TEXT = "晚上不喝咖啡，白天偶尔可以"
PATH = "memory/candidates/process"


def prepare(h):
    contents = {}
    for physical in h.physicals:
        data = dict(
            message_key=dict(physical["key"], revision=physical["revision"]),
            author=physical["author"],
            sent_at="2026-10-11T00:00:00Z",
            kind=physical["kind"],
            parts=[dict(kind="text", text=TEXT)],
            reply_refs=[],
            mentioned_accounts=[],
        )
        physical["content_digest"] = fingerprint(data)
        contents[canonical(physical["key"])] = data

    def include(kind, body, response):
        if body.get("include_content"):
            for physical in response["physicals"]:
                physical["content"] = copy.deepcopy(contents[canonical(physical["key"])])

    h.mutate = include
    response = h.post("memory/turn-commits", h.event())
    assert response.status_code == 200, response.text
    assert response.json()["confirmed_memory_written"] is False
    return response.json()["candidate_job_ref"]


def output():
    return {
        "memories": [
            dict(
                statement=TEXT,
                conditions=["白天偶尔可以"],
                negations=["晚上不喝咖啡"],
                valid_time="current",
                evidence=[dict(source_index=0, quote=TEXT)],
            )
        ]
    }


def recall(h, actor=0):
    request = h.selection(actor=actor, budget=8192)
    request.update(selection=["evidence"], query_text="咖啡喝什么")
    response = h.post("memory/select", request)
    assert response.status_code == 200, response.text
    return response.json()["selected_units"]


def test_read_commit_recall_replay_and_role_isolation(sync):
    job = prepare(sync)
    pending = sync.post(PATH, dict(job_id=job, output=None))
    assert pending.status_code == 200, pending.text
    assert pending.json()["state"] == "pending"
    assert pending.json()["content"][0]["text"] == TEXT
    assert recall(sync) == []
    committed = sync.post(PATH, dict(job_id=job, output=output()))
    assert committed.status_code == 200, committed.text
    assert committed.json()["result"]["state"] == "committed", committed.text
    units = recall(sync)
    assert len(units) == 1
    assert units[0]["statement"] == TEXT
    assert units[0]["uncertainty"] == "inferred"
    assert units[0]["negations"] == ["晚上不喝咖啡"]
    assert recall(sync, actor=1) == []
    assert sync.post(PATH, dict(job_id=job, output=None)).json() == committed.json()
    assert sync.post(PATH, dict(job_id=job, output=output())).json() == committed.json()
    assert recall(sync)[0]["record_id"] == units[0]["record_id"]


@pytest.mark.parametrize("empty", [False, True])
def test_source_revocation_between_prepare_and_commit_blocks_memory(sync, empty):
    job = prepare(sync)
    assert sync.post(PATH, dict(job_id=job, output=None)).status_code == 200
    sync.grants[0]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    response = sync.post(PATH, dict(job_id=job, output={"memories": []} if empty else output()))
    assert response.status_code != 200, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert sync.post(PATH, dict(job_id=job, output=None)).json()["state"] == "invalidated"


def test_queued_extraction_survives_additions_but_reviewed_snapshot_stays_strict(sync):
    job = prepare(sync)
    assert sync.post(PATH, dict(job_id=job, output=None)).status_code == 200
    with sync.store.transaction() as db:
        sync.service._bump(db, sync.scope(0), history_changed=False)
    with pytest.raises(Fault) as error:
        TrustedWorkflow(sync.service).commit_candidate(dict(job_id=job, drafts=[]))
    assert error.value.code == "scope_changed"
    response = sync.post(PATH, dict(job_id=job, output=output()))
    assert response.status_code == 200, response.text
    assert response.json()["result"]["state"] == "committed"
    assert len(recall(sync)) == 1
    assert sync.post(PATH, dict(job_id=job, output=output())).json() == response.json()


@pytest.mark.parametrize("history_changed", [False, True])
def test_delayed_turn_registration_only_survives_additive_version_changes(
    sync, monkeypatch, history_changed
):
    original = sync.post
    delayed = False

    def after_other_commit(path, body):
        nonlocal delayed
        if path == "memory/turn-commits" and not delayed:
            delayed = True
            with sync.store.transaction() as db:
                sync.service._bump(db, sync.scope(), history_changed=history_changed)
        return original(path, body)

    monkeypatch.setattr(sync, "post", after_other_commit)
    job = prepare(sync)
    if history_changed:
        assert job is None
        assert sync.post("memory/turn-commits", sync.event()).json()["state"] == "scope_changed"
        return
    assert job is not None
    pending = sync.post(PATH, dict(job_id=job, output=None))
    assert pending.status_code == 200 and pending.json()["content"][0]["text"] == TEXT
    completed = sync.post(PATH, dict(job_id=job, output=output()))
    assert completed.status_code == 200, completed.text
    assert completed.json()["result"]["state"] == "committed"
    assert sync.post("memory/turn-commits", sync.event()).json()["candidate_job_ref"] == job
    assert sync.post(PATH, dict(job_id=job, output=output())).json() == completed.json()
    assert len(recall(sync)) == 1


@pytest.mark.parametrize("empty", [False, True])
def test_semantic_version_change_rejects_facts_but_empty_current_source_can_skip(sync, empty):
    job = prepare(sync)
    assert sync.post(PATH, dict(job_id=job, output=None)).status_code == 200
    with sync.store.transaction() as db:
        sync.service._bump(db, sync.scope(0))
    response = sync.post(PATH, dict(job_id=job, output={"memories": []} if empty else output()))
    if empty:
        assert response.status_code == 200, response.text
        assert response.json()["result"]["state"] == "skipped"
    else:
        assert response.status_code == 409
        assert response.json()["code"] == "scope_changed"
    assert recall(sync) == []


def test_candidate_invalidated_during_barrier_cannot_commit_even_empty(sync, monkeypatch):
    job = prepare(sync)
    original = sync.service.operation

    @contextmanager
    def invalidated(**kwargs):
        with original(**kwargs) as db:
            db.execute("UPDATE jobs SET state='scope_changed' WHERE id=?", (job,))
            yield db

    monkeypatch.setattr(sync.service, "operation", invalidated)
    with pytest.raises(Fault) as error:
        TrustedWorkflow(sync.service).commit_extracted(dict(job_id=job, drafts=[]))
    assert error.value.code == "scope_changed"
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"scope": {}},
        {"statement": ""},
        {"valid_time": None},
        {"valid_time": 0},
        {"evidence": [{"source_index": 999, "quote": TEXT}]},
        {"evidence": [{"source_index": True, "quote": TEXT}]},
        {"evidence": [{"source_index": 0, "quote": "不存在的原文"}]},
    ],
)
def test_invalid_model_proposal_is_retryable_without_writing(sync, change):
    job = prepare(sync)
    proposed = output()
    proposed["memories"][0].update(change)
    response = sync.post(PATH, dict(job_id=job, output=proposed))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "rejected"
    assert recall(sync) == []
    assert (
        sync.post(PATH, dict(job_id=job, output=output())).json()["result"]["state"] == "committed"
    )


def test_empty_extraction_records_skip_and_candidate_capacity_is_released(sync):
    job = prepare(sync)
    response = sync.post(PATH, dict(job_id=job, output={"memories": []}))
    assert response.status_code == 200, response.text
    assert response.json()["result"]["state"] == "skipped"
    assert recall(sync) == []
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0] == 0


def test_existing_consume_grant_does_not_authorize_another_actor(sync):
    job = prepare(sync)
    sync.config["callers"]["companion"]["event_scopes"] = [sync.scope(1)]
    sync.save()
    assert sync.post(PATH, dict(job_id=job, output=None)).status_code == 403
    assert sync.post(PATH, dict(job_id=job, output=output())).status_code == 403
