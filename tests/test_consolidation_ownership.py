"""Summary ownership is bound to the job and portrait input uses owner originals."""

import copy
import json

import pytest
from test_candidate_extraction import PATH
from test_consolidation import pending, prepare, query, submit, window
from test_memory_v2 import candidate
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, fingerprint


def submission(prepared, *, subject=None):
    return dict(
        job_id=prepared["job_ids"][0],
        job_ids=prepared["job_ids"],
        batch_ref=prepared["batch_ref"],
        extraction_schema=5,
        output=dict(
            summary="一次讨论经历。",
            portrait=None,
            subject_person_id=subject or prepared["subject_person_id"],
        ),
    )


@pytest.mark.parametrize("wrong", ["other-person", "missing-subject"])
def test_new_batch_cannot_write_output_for_another_or_undeclared_subject(sync, wrong):
    prepare(sync)
    job = candidate(sync, "今天晚上读了一本小说。", 1, capture=False)[0]
    prepared = pending(sync, [job], window(sync, [job]))
    assert prepared["subject_person_id"] == sync.person
    body = submission(prepared, subject="person:someone-else")
    if wrong == "missing-subject":
        body["output"].pop("subject_person_id")
    response = sync.post(PATH, body)
    assert response.status_code == 400 and response.json()["code"] == "invalid_input", response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 1
        )
        envelope = json.loads(
            db.execute(
                "SELECT portrait_input FROM memory_batches WHERE job_id=?", (job,)
            ).fetchone()[0]
        )
        assert envelope == dict(subject_person_id=sync.person, input=None)
    done, _ = submit(sync, prepared, "晚上读过一本小说。")
    assert done["result"]["subject_person_id"] == sync.person


def test_portrait_history_contains_target_raw_speech_instead_of_mixed_summary_prose(sync):
    from test_group_context import owner

    if sync.scope()["audience"] != "group":
        pytest.skip("A mixed conversation needs two actual people in one authorized room")
    prepare(sync)
    other = owner(sync)
    foreign = candidate(sync, "我每天凌晨跑步。", 1, other, capture=False)[0]
    first = candidate(sync, "我晚上读小说。", 2, capture=False)[0]
    mixed = window(sync, [first], context=[foreign])
    one, _ = submit(sync, pending(sync, [first], mixed), "同事热爱凌晨跑步，用户晚上读小说。")
    second = candidate(sync, "今天晚上又读了一章。", 3, capture=False)[0]
    prepared = pending(sync, [second], window(sync, [second], context=[foreign, first]))
    portrait = prepared["portrait_input"]
    assert portrait["subject_person_id"] == sync.person
    assert portrait["summary_refs"] == one["result"]["summary_record_refs"]
    assert all(
        "text" not in item and item["subject_person_id"] == sync.person
        for item in portrait["summaries"]
    )
    material = [part for item in portrait["summaries"] for part in item["subject_material"]]
    assert [part["text"] for part in material] == ["我晚上读小说。"]
    assert all(part["speaker"]["person_id"] == sync.person for part in material)
    assert all(part["speaker"]["person_id"] != sync.scope(other)["person_id"] for part in material)
    # The whole conversation summary remains an ordinary sourced memory. Its
    # complete foreign leaf is retained even though it is not portrait prose.
    with sync.store.transaction() as db:
        group = one["result"]["group_ids"][0]
        owners = {
            row[0]
            for row in db.execute(
                "SELECT source_scope FROM memory_summary_sources WHERE group_id=?", (group,)
            )
        }
        assert owners == {canonical(sync.scope()), canonical(sync.scope(other))}
    assert any(
        item["kind"] == "summary" and "同事热爱凌晨跑步" in item["unit"]["statement"]
        for item in query(sync, "凌晨跑步", kinds=["facts"])["evidence"]
    )


@pytest.mark.parametrize("damage", ["none", "missing-span", "changed-body", "oversized"])
def test_portrait_basis_rebuilds_complete_utf8_spans_or_omits_the_whole_summary(sync, damage):
    prepare(sync)
    text = "上个月读了一段很喜欢的小说，接着和朋友讨论了其中的人物。" * (
        260 if damage == "oversized" else 24
    )
    first = candidate(sync, text, 1, capture=False)[0]
    submit(sync, pending(sync, [first], window(sync, [first])), "讨论过小说。")
    with sync.store.transaction() as db:
        parts = db.execute(
            "SELECT * FROM memory_documents WHERE kind='episode' ORDER BY span_start"
        ).fetchall()
        assert len(parts) > 1
        if damage == "missing-span":
            db.execute("DELETE FROM memory_documents WHERE id=?", (parts[0]["id"],))
        elif damage == "changed-body":
            from tianshu_memory.memory_documents import text_hash

            changed = "错" + parts[0]["text"][1:]
            db.execute(
                "UPDATE memory_documents SET text=?,content_hash=? WHERE id=?",
                (changed, text_hash(changed), parts[0]["id"]),
            )
    second = candidate(sync, "今天继续读小说。", 2, capture=False)[0]
    prepared = pending(sync, [second], window(sync, [second]))
    if damage == "none":
        material = prepared["portrait_input"]["summaries"][0]["subject_material"]
        assert len(material) == 1 and material[0]["text"] == text
    else:
        assert prepared["portrait_input"] is None


@pytest.mark.parametrize("send_output", [False, True])
def test_unbound_pending_old_batch_retires_without_replacing_its_frozen_input(sync, send_output):
    prepare(sync)
    job = candidate(sync, "昨天读了一本小说。", 1, capture=False)[0]
    prepared = pending(sync, [job], window(sync, [job]))
    with sync.store.transaction() as db:
        # Explicit fixture of the old persisted schema-5 format, not a source
        # or authorization bypass. The old batch stays byte-for-byte retained.
        db.execute("UPDATE memory_batches SET portrait_input='null' WHERE job_id=?", (job,))
        before = dict(db.execute("SELECT * FROM memory_batches WHERE job_id=?", (job,)).fetchone())
        originals = [
            tuple(row) for row in db.execute("SELECT * FROM memory_documents WHERE kind='episode'")
        ]
    if send_output:
        body = submission(prepared)
        body["output"].pop("subject_person_id")
    else:
        body = dict(
            job_id=job, job_ids=[job], output=None, supported_extraction_schemas=[5], window=None
        )
    response = sync.post(PATH, body)
    assert response.status_code == 200 and response.json()["state"] == "invalidated", response.text
    assert sync.post(PATH, body).json() == response.json()
    with sync.store.transaction() as db:
        assert (
            dict(db.execute("SELECT * FROM memory_batches WHERE job_id=?", (job,)).fetchone())
            == before
        )
        assert [
            tuple(row) for row in db.execute("SELECT * FROM memory_documents WHERE kind='episode'")
        ] == originals
        assert (
            db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0] == "scope_changed"
        )
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0


def test_completed_old_output_uses_exact_ledger_digest_before_subject_projection(sync):
    prepare(sync)
    job = candidate(sync, "昨天读了一本小说。", 1, capture=False)[0]
    prepared = pending(sync, [job], window(sync, [job]))
    done, body = submit(sync, prepared, "昨天读过小说。")
    legacy = copy.deepcopy(body)
    legacy["output"].pop("subject_person_id")
    original_result = dict(done["result"])
    original_result.pop("subject_person_id")
    with sync.store.transaction() as db:
        db.execute("UPDATE memory_batches SET portrait_input='null' WHERE job_id=?", (job,))
        db.execute(
            "UPDATE write_ledger SET digest=?,result=? WHERE job_id=?",
            (fingerprint(legacy["output"]), canonical(original_result), job),
        )
        before = tuple(db.execute("SELECT * FROM write_ledger WHERE job_id=?", (job,)).fetchone())
    response = sync.post(PATH, legacy)
    assert response.status_code == 200 and response.json()["state"] == "completed", response.text
    assert response.json()["result"] == done["result"]
    assert pending(sync, [job], None) == response.json()
    changed = copy.deepcopy(legacy)
    changed["output"]["summary"] += "另一份结果"
    assert sync.post(PATH, changed).status_code == 409
    with sync.store.transaction() as db:
        assert (
            tuple(db.execute("SELECT * FROM write_ledger WHERE job_id=?", (job,)).fetchone())
            == before
        )
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


def test_unbound_old_portrait_is_retained_but_rebuilt_from_owner_originals(sync):
    prepare(sync)
    first = candidate(sync, "昨天读了一本小说。", 1, capture=False)[0]
    submit(sync, pending(sync, [first], window(sync, [first])), "读过小说。")
    second = candidate(sync, "今天又读了一章。", 2, capture=False)[0]
    prepared = pending(sync, [second], window(sync, [second]))
    done, _ = submit(sync, prepared, "今天继续读小说。", portrait="旧画像混入了别人的跑步爱好。")
    with sync.store.transaction() as db:
        db.execute(
            "UPDATE memory_batches SET portrait_input=? WHERE job_id=?",
            (canonical(prepared["portrait_input"]), second),
        )
        old_group = done["result"]["portrait"]["record_refs"][0]["record_id"]
        old_record = tuple(db.execute("SELECT * FROM records WHERE id=?", (old_group,)).fetchone())
    third = candidate(sync, "今晚打算继续读小说。", 3, capture=False)[0]
    fresh = pending(sync, [third], window(sync, [third]))
    assert fresh["portrait_input"]["current_text"] == ""
    material = [
        part["text"]
        for item in fresh["portrait_input"]["summaries"]
        for part in item["subject_material"]
    ]
    assert set(material) == {"昨天读了一本小说。", "今天又读了一章。"}
    with sync.store.transaction() as db:
        assert (
            tuple(db.execute("SELECT * FROM records WHERE id=?", (old_group,)).fetchone())
            == old_record
        )
