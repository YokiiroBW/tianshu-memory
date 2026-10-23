"""Inherited canonical values retain their sources through forgetting and rebuilds."""

import json

import pytest
from test_candidate_extraction import PATH
from test_memory_context import ProofIssuer
from test_memory_v2 import SyntheticVectors, candidate, commit, fact, post, query, request, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical
from tianshu_memory.memory_reindex import MemoryReindexer
from tianshu_memory.retrieval import Retrieval

VALUE = "青瓷四十二"
PREDICATE = "preference.marker"


def ref(sync, group):
    with sync.store.transaction() as db:
        record = db.execute("SELECT id,version FROM records WHERE group_id=?", (group,)).fetchone()
    return dict(group_id=group, record_id=record["id"], record_version=record["version"])


def write(sync, text, serial, *facts):
    job, _ = candidate(sync, text, serial)
    result = commit(sync, job, text, *facts)
    assert result["state"] == "completed", result
    return result["result"]["group_ids"]


def original(sync):
    text = f"我给工具包设的识别标记是{VALUE}。"
    group = write(sync, text, 1, fact(VALUE, predicate=PREDICATE))[0]
    episode = next(
        item for item in query(sync, "我的识别标记")["evidence"] if item["kind"] == "episode"
    )
    source_key = sync.service.source_identity(sync.admissions[-1]["source"], sync.scope())
    return group, episode, source_key


def forget_request(sync, episode, serial):
    write(sync, "请忘记最初那条来源。", serial)
    request = dict(
        command=sync.command(),
        scope=sync.scope(),
        batch_ref=None,
        item_id=f"forget:{serial}",
        kind="forget_episode",
        target=dict(episode_ref=episode["episode_id"], episode_version=episode["episode_version"]),
        evidence_refs=[sync.admissions[-1]["source"]],
        proof_ref="test:proof",
    )
    proofs = ProofIssuer()
    sync.service.memory_context.proofs = proofs
    proofs.issue(request, "revision", [sync.account], [sync.scope()])
    return request


def indexer(sync):
    backend = SyntheticVectors()
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    return MemoryReindexer(sync.service), backend


def test_target_closure_forget_preserves_receipt_and_independent_fact(sync):
    setup(sync)
    first, episode, source_key = original(sync)
    confirmation = "刚才那项记录仍然有效。"
    assert VALUE not in confirmation
    second = write(
        sync,
        confirmation,
        2,
        fact(VALUE, predicate=PREDICATE, operation="confirm", targets=[ref(sync, first)]),
    )[0]
    retraction = "撤回刚才那项记录。另外我喜欢柠檬茶。"
    assert VALUE not in retraction
    third, independent = write(
        sync,
        retraction,
        3,
        fact(VALUE, predicate=PREDICATE, operation="retract", targets=[ref(sync, second)]),
        fact("柠檬茶"),
    )
    with sync.store.transaction() as db:
        closure = {
            row["source_key"]
            for row in db.execute("SELECT source_key FROM lineage WHERE group_id=?", (third,))
        }
        assert len(closure) == 3 and source_key in closure
        assert (
            db.execute(
                "SELECT quote FROM automatic_evidence WHERE group_id=? AND source_key=?",
                (third, source_key),
            ).fetchone()[0]
            == f"我给工具包设的识别标记是{VALUE}。"
        )
    late_text = "请撤回先前那一项。"
    late_job, _ = candidate(sync, late_text, 4)
    late = fact(VALUE, predicate=PREDICATE, operation="retract", targets=[ref(sync, third)])
    forget = forget_request(sync, episode, 5)
    maintenance, _ = indexer(sync)
    assert maintenance.index_step(batch_size=32)["indexed"] > 0
    response = post(sync, "forget", forget)
    assert response.status_code == 200 and response.json()["state"] == "tombstoned", response.text
    assert response.json()["record_ids"] == response.json()["group_ids"] == []
    assert post(sync, "forget", forget).json() == response.json()
    recovered = sync.post(
        "memory/context/receipt",
        dict(
            query=request(sync, "回执")["query"],
            scope=sync.scope(),
            operation_id=forget["command"]["idempotency_key"],
        ),
    )
    assert recovered.status_code == 200 and recovered.json()["receipt"] == response.json()
    assert commit(sync, late_job, late_text, late)["state"] == "invalidated"
    maintenance.step(restart=True)
    maintenance.index_step(batch_size=32)
    history = request(sync, "我的识别标记与偏好")
    history["queries"][0]["intent"] = "history"
    selected = post(sync, "query", history)
    assert selected.status_code == 200, selected.text
    assert VALUE not in canonical(selected.json()["evidence"])
    assert any(
        item.get("fact", {}).get("value") == "柠檬茶"
        for item in query(sync, "我喜欢什么茶")["evidence"]
    )
    with sync.store.transaction() as db:
        assert all(VALUE not in row[0] for row in db.execute("SELECT text FROM memory_documents"))
        for group in (first, second, third):
            assert (
                db.execute("SELECT state FROM groups WHERE id=?", (group,)).fetchone()[0]
                == "invalidated"
            )
            assert (
                db.execute("SELECT 1 FROM memory_documents WHERE group_id=?", (group,)).fetchone()
                is None
            )
            assert (
                db.execute(
                    "SELECT 1 FROM memory_vectors WHERE document_id=?", ("fact:" + group,)
                ).fetchone()
                is None
            )
            assert (
                db.execute("SELECT state FROM records WHERE group_id=?", (group,)).fetchone()[0]
                == "tombstoned"
            )
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (independent,)).fetchone()[0]
            == "active"
        )
        assert db.execute(
            "SELECT 1 FROM history WHERE record_id=?", (ref_from_group(db, third),)
        ).fetchone()


def ref_from_group(db, group):
    return db.execute("SELECT id FROM records WHERE group_id=?", (group,)).fetchone()[0]


@pytest.mark.parametrize("operation", ["assert", "replace"])
def test_independent_value_does_not_inherit_replaced_or_confirmed_source(sync, operation):
    setup(sync)
    first, episode, source_key = original(sync)
    confirmed = write(
        sync,
        "确认之前那项记录。",
        2,
        fact(VALUE, predicate=PREDICATE, operation="confirm", targets=[ref(sync, first)]),
    )[0]
    value = VALUE if operation == "assert" else "琥珀七号"
    own = write(
        sync,
        f"我现在明确把工具包标记设为{value}。",
        3,
        fact(
            value,
            predicate=PREDICATE,
            operation=operation,
            targets=[] if operation == "assert" else [ref(sync, confirmed)],
        ),
    )[0]
    assert own not in {first, confirmed}
    with sync.store.transaction() as db:
        dependencies = [
            row[0] for row in db.execute("SELECT source_key FROM lineage WHERE group_id=?", (own,))
        ]
        assert len(dependencies) == 1 and source_key not in dependencies
    if operation == "assert":
        for admission, grant in zip(sync.admissions, sync.grants, strict=True):
            if sync.service.source_identity(admission["source"], sync.scope()) == source_key:
                grant["state"] = "denied"
        sync.platform_head["sequence"] += 1
    else:
        forget = forget_request(sync, episode, 4)
        response = post(sync, "forget", forget)
        assert response.status_code == 200 and response.json()["state"] == "tombstoned", (
            response.text
        )
        assert response.json()["record_ids"] == response.json()["group_ids"] == []
    selected = query(sync, value)
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (confirmed,)).fetchone()[0]
            == "invalidated"
        )
        assert db.execute("SELECT state FROM groups WHERE id=?", (own,)).fetchone()[0] == "active"
    assert any(item.get("fact", {}).get("value") == value for item in selected["evidence"])


def test_confirmation_depends_on_exact_target_not_an_unrelated_equal_value(sync):
    setup(sync)
    first, episode, source_key = original(sync)
    other = write(
        sync, f"再独立记录一次，我的标记是{VALUE}。", 2, fact(VALUE, predicate=PREDICATE)
    )[0]
    other_source = sync.service.source_identity(sync.admissions[-1]["source"], sync.scope())
    confirmed = write(
        sync,
        "确认指定的最初那项记录。",
        3,
        fact(VALUE, predicate=PREDICATE, operation="confirm", targets=[ref(sync, first)]),
    )[0]
    assert confirmed not in {first, other}
    with sync.store.transaction() as db:
        dependencies = {
            row[0]
            for row in db.execute("SELECT source_key FROM lineage WHERE group_id=?", (confirmed,))
        }
        assert source_key in dependencies and other_source not in dependencies
    response = post(sync, "forget", forget_request(sync, episode, 4))
    assert response.status_code == 200 and response.json()["state"] == "tombstoned", response.text
    assert response.json()["record_ids"] == response.json()["group_ids"] == []
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT state FROM groups WHERE id=?", (confirmed,)).fetchone()[0]
            == "invalidated"
        )
        assert db.execute("SELECT state FROM groups WHERE id=?", (other,)).fetchone()[0] == "active"
    assert any(
        item.get("fact", {}).get("value") == VALUE for item in query(sync, VALUE)["evidence"]
    )


def test_late_vector_write_cannot_restore_inherited_value(sync):
    setup(sync)
    first, episode, _ = original(sync)
    derived = write(
        sync,
        "请撤回刚才那项记录。",
        2,
        fact(VALUE, predicate=PREDICATE, operation="retract", targets=[ref(sync, first)]),
    )[0]
    forget = forget_request(sync, episode, 3)
    maintenance, backend = indexer(sync)

    def withdraw():
        response = post(sync, "forget", forget)
        assert response.status_code == 200 and response.json()["state"] == "tombstoned"

    backend.on_embed = withdraw
    maintenance.index_step(batch_size=32)
    with sync.store.transaction() as db:
        for group in (first, derived):
            assert (
                db.execute(
                    "SELECT 1 FROM memory_vectors WHERE document_id=?", ("fact:" + group,)
                ).fetchone()
                is None
            )
            assert (
                db.execute("SELECT 1 FROM memory_documents WHERE group_id=?", (group,)).fetchone()
                is None
            )


def test_derived_update_requires_exact_value_and_confirmation_qualifiers(sync):
    setup(sync)
    first, _, _ = original(sync)
    job, _ = candidate(sync, "确认之前那项记录。", 2)
    wrong = commit(
        sync,
        job,
        "确认之前那项记录。",
        fact("不是原来的值", predicate=PREDICATE, operation="retract", targets=[ref(sync, first)]),
    )
    assert wrong["state"] == "rejected"
    changed = dict(
        memories=[
            dict(
                conditions=["仅限雨天"],
                negations=[],
                valid_time="当前",
                evidence=[dict(source_index=0, quote="确认之前那项记录。")],
                fact=fact(
                    VALUE, predicate=PREDICATE, operation="confirm", targets=[ref(sync, first)]
                ),
            )
        ]
    )
    response = sync.post(PATH, dict(job_id=job, extraction_schema=4, output=changed))
    assert response.status_code == 200 and response.json()["state"] == "rejected"
    with sync.store.transaction() as db:
        row = db.execute(
            "SELECT payload FROM automatic_facts WHERE group_id=?", (first,)
        ).fetchone()
        assert json.loads(row[0])["update"]["operation"] == "assert"
