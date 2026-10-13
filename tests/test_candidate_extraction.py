"""Real Memory and TLS source owners with explicitly synthetic source/model content."""

import copy

import pytest
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, fingerprint

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


def test_source_revocation_between_prepare_and_commit_blocks_memory(sync):
    job = prepare(sync)
    assert sync.post(PATH, dict(job_id=job, output=None)).status_code == 200
    sync.grants[0]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    response = sync.post(PATH, dict(job_id=job, output=output()))
    assert response.status_code != 200, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert sync.post(PATH, dict(job_id=job, output=None)).json()["state"] == "invalidated"


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
