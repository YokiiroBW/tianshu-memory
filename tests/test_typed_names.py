"""Typed name aliases share canonical identity without weakening source attribution."""

import json

import pytest
from test_candidate_extraction import PATH
from test_fact_provenance import forget_request, indexer, ref
from test_memory_v2 import candidate, commit, fact, post, query, request, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory import fact_names
from tianshu_memory.domain import canonical


@pytest.mark.parametrize("key", fact_names.KEYS)
def test_alias_name_and_ordinary_author_property_commit_atomically(sync, key):
    setup(sync)
    text = "我叫合成甲，我喜欢乌龙茶。"
    job, _ = candidate(sync, text)
    result = commit(sync, job, text, fact("合成甲", predicate=key), fact("乌龙茶"))
    assert result["state"] == "completed", result
    assert len(result["result"]["record_ids"]) == 2
    with sync.store.transaction() as db:
        rows = db.execute(
            "SELECT f.fact_key,f.payload,g.field_key,r.payload AS unit FROM automatic_facts f "
            "JOIN groups g ON g.id=f.group_id JOIN records r ON r.group_id=g.id"
        ).fetchall()
    assert {row["fact_key"] for row in rows} == {"identity.name", "preference.drink"}
    name = next(row for row in rows if row["fact_key"] == "identity.name")
    payload = json.loads(name["payload"])
    assert payload["key"] == payload["canonical"]["predicate"] == "identity.name"
    assert name["field_key"] == "automatic.identity.name"
    assert json.loads(name["unit"])["statement"] == "姓名：合成甲"
    _, pending = candidate(sync, "我再补充一件事。", 2)
    assert any(
        item.get("fact", {}).get("predicate") == "identity.name" for item in pending["known_facts"]
    )


@pytest.mark.parametrize("key", fact_names.KEYS)
def test_alias_update_targets_resolve_the_same_canonical_name_slot(sync, key):
    setup(sync)
    first = "我叫合成甲。"
    job, _ = candidate(sync, first)
    assert commit(sync, job, first, fact("合成甲", predicate=key))["state"] == "completed"
    second = "我现在叫合成乙。"
    job, pending = candidate(sync, second, 2)
    target = next(
        item
        for item in pending["known_facts"]
        if item.get("fact", {}).get("predicate") == "identity.name"
    )
    result = commit(
        sync,
        job,
        second,
        fact("合成乙", predicate=key, operation="replace", targets=target["target_refs"]),
    )
    assert result["state"] == "completed", result
    values = [
        item["fact"]["value"]
        for item in query(sync, "我叫什么")["evidence"]
        if item.get("fact", {}).get("predicate") == "identity.name"
    ]
    assert values == ["合成乙"]


@pytest.mark.parametrize(
    "source,quoted,reality",
    [
        ("我喜欢乌龙茶。", "我喜欢乌龙茶。", "real"),
        ("朋友说：“我叫合成甲。”我喜欢乌龙茶。", "我叫合成甲。", "real"),
        ("如果我叫合成甲，就很有趣。我喜欢乌龙茶。", "我叫合成甲", "real"),
        ("我不叫合成甲。我喜欢乌龙茶。", "我不叫合成甲。", "real"),
        ("小周叫合成甲。我喜欢乌龙茶。", "小周叫合成甲。", "real"),
        ("我叫合成甲，我喜欢乌龙茶。", "我叫合成甲", "fictional"),
    ],
)
def test_unsupported_name_rejects_batch_without_losing_ordinary_fact_on_retry(
    sync, source, quoted, reality
):
    setup(sync)
    job, _ = candidate(sync, source, reality=reality)
    memories = [
        dict(
            fact=value,
            conditions=[],
            negations=[],
            valid_time="当前",
            evidence=[dict(source_index=0, quote=quote)],
        )
        for value, quote in [
            (fact("乌龙茶"), source),
            (fact("合成甲", predicate="author.name"), quoted),
        ]
    ]
    response = sync.post(
        PATH, dict(job_id=job, extraction_schema=4, output=dict(memories=memories))
    )
    assert response.status_code == 200 and response.json()["state"] == "rejected", response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM automatic_facts").fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM write_ledger WHERE job_id=?", (job,)).fetchone() is None
    assert commit(sync, job, source, fact("乌龙茶"))["state"] == "completed"


def test_author_subject_does_not_make_an_ordinary_property_a_name(sync):
    setup(sync)
    text = "我喜欢乌龙茶。"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact("乌龙茶"))["state"] == "completed"
    with sync.store.transaction() as db:
        value = json.loads(db.execute("SELECT payload FROM automatic_facts").fetchone()[0])
    assert value["key"] == value["canonical"]["predicate"] == "preference.drink"


def test_entity_name_does_not_become_the_authors_canonical_name(sync):
    setup(sync)
    text = "我的猫叫团子，我叫合成甲。"
    job, _ = candidate(sync, text)
    result = commit(
        sync,
        job,
        text,
        fact(
            "团子",
            predicate="name",
            subject=dict(kind="entity", id=None, type="cat", mention="我的猫"),
        ),
        fact("合成甲", predicate="author.name"),
    )
    assert result["state"] == "completed", result
    with sync.store.transaction() as db:
        values = [
            json.loads(row[0])["canonical"]
            for row in db.execute("SELECT payload FROM automatic_facts")
        ]
    assert {(value["subject"]["kind"], value["predicate"], value["value"]) for value in values} == {
        ("entity", "name", "团子"),
        ("author", "identity.name", "合成甲"),
    }


@pytest.mark.parametrize("operation", ["confirm", "retract"])
def test_sourced_name_update_preserves_canonical_render_and_inherited_forget(sync, operation):
    setup(sync)
    first = "我叫合成甲。"
    job, _ = candidate(sync, first)
    assert commit(sync, job, first, fact("合成甲", predicate="author.name"))["state"] == "completed"
    episode = next(item for item in query(sync, "合成甲")["evidence"] if item["kind"] == "episode")
    text = "我还是叫合成甲。" if operation == "confirm" else "我以前叫合成甲。"
    job, pending = candidate(sync, text, 2)
    target = next(
        item
        for item in pending["known_facts"]
        if item.get("fact", {}).get("predicate") == "identity.name"
    )
    result = commit(
        sync,
        job,
        text,
        fact("合成甲", predicate="name", operation=operation, targets=target["target_refs"]),
    )
    assert result["state"] == "completed", result
    history = request(sync, "合成甲")
    history["queries"][0]["intent"] = "history"
    response = post(sync, "query", history)
    assert response.status_code == 200, response.text
    assert any(
        item.get("fact", {}).get("predicate") == "identity.name"
        for item in response.json()["evidence"]
    )
    forgotten = post(sync, "forget", forget_request(sync, episode, 3))
    assert forgotten.status_code == 200 and forgotten.json()["state"] == "tombstoned", (
        forgotten.text
    )
    response = post(sync, "query", history)
    assert response.status_code == 200, response.text
    assert not any(
        item.get("fact", {}).get("predicate") == "identity.name"
        for item in response.json()["evidence"]
    )


def test_control_only_name_chain_retains_assertion_proof_and_source_forgetting(sync):
    setup(sync)
    job, _ = candidate(sync, "我叫合成甲。")
    original = commit(sync, job, "我叫合成甲。", fact("合成甲", predicate="identity.name"))
    original_group = original["result"]["group_ids"][0]
    episode = next(item for item in query(sync, "合成甲")["evidence"] if item["kind"] == "episode")
    text = "确认先前那项记录。"
    job, pending = candidate(sync, text, 2)
    target = next(
        item
        for item in pending["known_facts"]
        if item.get("fact", {}).get("predicate") == "identity.name"
    )
    result = commit(
        sync,
        job,
        text,
        fact("合成甲", predicate="author.name", operation="confirm", targets=target["target_refs"]),
    )
    assert result["state"] == "completed", result
    confirmed = result["result"]["group_ids"][0]
    assert any(
        item.get("fact", {}).get("value") == "合成甲"
        for item in query(sync, "我叫什么")["evidence"]
    )
    text = "撤回刚才确认的那项记录。"
    job, _ = candidate(sync, text, 3)
    result = commit(
        sync,
        job,
        text,
        fact(
            "合成甲", predicate="person.name", operation="retract", targets=[ref(sync, confirmed)]
        ),
    )
    assert result["state"] == "completed", result
    retracted = result["result"]["group_ids"][0]
    with sync.store.transaction() as db:
        rows = {
            row["group_id"]: json.loads(row["payload"])
            for row in db.execute("SELECT group_id,payload FROM automatic_facts")
        }
        assert "name_assertion_refs" not in rows[original_group]
        assert rows[confirmed]["name_assertion_refs"] == rows[retracted]["name_assertion_refs"]
        assert len(rows[confirmed]["name_assertion_refs"]) == 1
        assert (
            db.execute("SELECT count(*) FROM lineage WHERE group_id=?", (retracted,)).fetchone()[0]
            == 3
        )
    maintenance, _ = indexer(sync)
    assert maintenance.index_step(batch_size=32)["indexed"] > 0
    late_text = "确认原来的记录。"
    late_job, _ = candidate(sync, late_text, 4)
    late_fact = fact(
        "合成甲", predicate="name", operation="retract", targets=[ref(sync, retracted)]
    )
    forgotten = post(sync, "forget", forget_request(sync, episode, 5))
    assert forgotten.status_code == 200 and forgotten.json()["state"] == "tombstoned", (
        forgotten.text
    )
    assert commit(sync, late_job, late_text, late_fact)["state"] == "invalidated"
    maintenance.step(restart=True)
    maintenance.index_step(batch_size=32)
    history = request(sync, "合成甲")
    history["queries"][0]["intent"] = "history"
    selected = post(sync, "query", history)
    assert selected.status_code == 200, selected.text
    assert "合成甲" not in canonical(selected.json()["evidence"])
    with sync.store.transaction() as db:
        for group in (original_group, confirmed, retracted):
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


@pytest.mark.parametrize("operation", ["assert", "replace"])
def test_independent_name_cannot_borrow_a_known_name_proof(sync, operation):
    setup(sync)
    job, _ = candidate(sync, "我叫合成甲。")
    result = commit(sync, job, "我叫合成甲。", fact("合成甲", predicate="identity.name"))
    text = "我喜欢乌龙茶。"
    job, _ = candidate(sync, text, 2)
    targets = [ref(sync, result["result"]["group_ids"][0])] if operation == "replace" else []
    result = commit(
        sync,
        job,
        text,
        fact("合成甲", predicate="author.name", operation=operation, targets=targets),
    )
    assert result["state"] == "rejected"


@pytest.mark.parametrize("damage", ["missing", "control", "source_text"])
def test_retained_name_proof_refs_fail_closed_when_the_bound_proof_changes(sync, damage):
    setup(sync)
    job, _ = candidate(sync, "我叫合成甲。")
    first = commit(sync, job, "我叫合成甲。", fact("合成甲", predicate="identity.name"))["result"][
        "group_ids"
    ][0]
    text = "确认先前那项记录。"
    job, _ = candidate(sync, text, 2)
    second = commit(
        sync,
        job,
        text,
        fact("合成甲", predicate="name", operation="confirm", targets=[ref(sync, first)]),
    )["result"]["group_ids"][0]
    with sync.store.transaction() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM automatic_facts WHERE group_id=?", (second,)
            ).fetchone()[0]
        )
        if damage == "source_text":
            db.execute(
                "UPDATE automatic_evidence SET source_text='朋友说：我叫合成甲。' WHERE group_id=? AND quote='我叫合成甲。'",
                (second,),
            )
        else:
            control = dict(
                db.execute(
                    "SELECT * FROM automatic_evidence WHERE group_id=? AND quote=?", (second, text)
                ).fetchone()
            )
            payload["name_assertion_refs"] = [
                "0" * 64 if damage == "missing" else fact_names.proof_ref(control)
            ]
            db.execute(
                "UPDATE automatic_facts SET payload=? WHERE group_id=?",
                (canonical(payload), second),
            )
        names = fact_names.family(sync.service, db, sync.scope())
        assert second not in names.entries
