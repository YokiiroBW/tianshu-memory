"""Real Memory HTTP/storage with synthetic authenticated owners, never a live model."""

import copy
import json

import pytest
from test_automatic_facts import initialize
from test_candidate_extraction import PATH
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, fingerprint


def setup(h):
    initialize(h)
    h.store.migrate_memory(h.directory / "pre-memory-v3.sqlite")
    h.contracts.load_memory_v2()
    h.config["callers"]["companion"]["operations"] += [
        "context_query",
        "context_propose",
        "context_receipt",
    ]
    h.save()


def candidate(h, text, serial=1, actor=0, *, reality="real"):
    physical, admission, grant = [
        copy.deepcopy(items[actor]) for items in (h.physicals, h.admissions, h.grants)
    ]
    physical.update(
        key=dict(physical["key"], message_id=f"v2-source:{serial}"),
        physical_receipt_id=f"v2-physical:{serial}",
    )
    physical["classification"]["value"] = reality
    admission["selector"]["key"] = physical["key"]
    admission["source"]["message_key"]["message_id"] = physical["key"]["message_id"]
    admission["source"]["receipt_id"] = f"v2-admission:{serial}"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    grant.update(selector=admission["selector"], entry_id=f"v2-entry:{serial}")
    content = dict(
        message_key=dict(physical["key"], revision=physical["revision"]),
        author=physical["author"],
        sent_at=f"2026-10-11T00:00:{serial:02d}Z",
        kind=physical["kind"],
        parts=[dict(kind="text", text=text)],
        reply_refs=[],
        mentioned_accounts=[],
    )
    physical["content_digest"] = fingerprint(content)
    h.test_contents[canonical(physical["key"])] = content
    h.physicals.append(physical)
    h.admissions.append(admission)
    h.grants.append(grant)
    turn = copy.deepcopy(h.turns[actor])
    turn = json.loads(canonical(turn).replace(turn["turn_id"], f"v2-turn:{serial}"))
    turn["input_sources"] = [admission["source"]]
    event = turn["committed_event"]
    event["reality"] = reality
    event.update(event_id=f"v2-event:{serial}", sources=[admission["source"]])
    with h.store.transaction() as db:
        event["scope_version"] = h.service._scope_version(db, h.scope(actor))
    if "scope_version" in turn:
        turn["scope_version"] = event["scope_version"]
    h.turns.append(turn)
    h.core_head["sequence"] += 1
    h.platform_head["sequence"] += 1
    response = h.post("memory/turn-commits", event)
    assert response.status_code == 200, response.text
    job = response.json()["candidate_job_ref"]
    pending = h.post(PATH, dict(job_id=job, output=None, supported_extraction_schemas=[3, 4]))
    assert pending.status_code == 200, pending.text
    assert pending.json()["extraction_schema"] == 4
    h.contracts.validate("memory-extraction#process_response", pending.json())
    return job, pending.json()


def fact(
    value,
    *,
    predicate="preference.drink",
    subject=None,
    operation="assert",
    targets=(),
    cardinality="single",
    modality="asserted",
):
    return dict(
        subject=subject or dict(kind="author", id=None, type=None, mention=None),
        predicate=predicate,
        value=value,
        cardinality=cardinality,
        modality=modality,
        state="current",
        valid_from=None,
        valid_to=None,
        keywords=[value],
        operation=operation,
        target_refs=list(targets),
    )


def commit(h, job, text, *facts):
    output = dict(
        memories=[
            dict(
                conditions=[],
                negations=[],
                valid_time="当前",
                evidence=[dict(source_index=0, quote=text)],
                fact=value,
            )
            for value in facts
        ]
    )
    result = h.post(PATH, dict(job_id=job, extraction_schema=4, output=output))
    assert result.status_code == 200, result.text
    h.contracts.validate("memory-extraction#process_response", result.json())
    return result.json()


def request(h, text, *, actor=0):
    return dict(
        schema_version=2,
        query={k: h.command(actor)[k] for k in ("schema_version", "request_id", "origin")},
        requested_scope=h.scope(actor),
        queries=[
            dict(
                query_id="q1",
                text=text,
                kinds=["facts", "episodes"],
                intent="current",
                time_filter=None,
            )
        ],
        selection=["identity", "evidence"],
        include_associated=False,
        budget=dict(tokens=65536, bytes=65536),
        limits=dict(facts=8, episodes=8),
        known_bundle=None,
    )


def post(h, operation, payload):
    return h.client.post(
        "/internal/v2/memory/context/" + operation,
        json=payload,
        headers={"Authorization": "Bearer test-only-companion-secret"},
    )


def query(h, text):
    result = post(h, "query", request(h, text))
    assert result.status_code == 200, result.text
    return result.json()


def test_canonical_and_episode_read_and_one_barrier_verify(sync):
    setup(sync)
    text = "我喜欢乌龙茶。"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact("乌龙茶"))["state"] == "completed"
    result = query(sync, "我喜欢什么茶")
    assert any(item.get("fact", {}).get("value") == "乌龙茶" for item in result["evidence"])
    assert any(item["kind"] == "episode" and item["text"] == text for item in result["evidence"])
    sync.calls.clear()
    verified = post(
        sync,
        "verify",
        dict(
            schema_version=2,
            query=request(sync, "茶")["query"],
            requested_scope=sync.scope(),
            bundle=result["bundle"],
            checks=[],
        ),
    )
    assert verified.status_code == 200, verified.text
    assert [kind for kind, _, _ in sync.calls if kind != "origin"] == [
        "snapshot",
        "current",
        "head",
    ]


def test_episode_only_withdrawal_invalidates_bundle_and_removes_all_projections(sync):
    setup(sync)
    text = "明天可能去松林，随身带雨伞。" * 30
    job, _ = candidate(sync, text)
    assert commit(sync, job, text)["state"] == "completed"
    result = query(sync, "我明天去松林带什么")
    assert any(item["kind"] == "episode" for item in result["evidence"])
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    sync.grants[-1]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    verified = post(
        sync,
        "verify",
        dict(
            schema_version=2,
            query=request(sync, "松林")["query"],
            requested_scope=sync.scope(),
            bundle=result["bundle"],
            checks=[],
        ),
    )
    assert verified.status_code == 409, verified.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM memory_documents").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_episode_index").fetchone()[0] == 0


def test_set_add_and_explicit_replace_keep_distinct_values(sync):
    setup(sync)
    first = "我喜欢游泳。"
    job, _ = candidate(sync, first)
    assert (
        commit(
            sync, job, first, fact("游泳", predicate="hobby", cardinality="set", operation="add")
        )["state"]
        == "completed"
    )
    second = "我也喜欢骑行。"
    job, pending = candidate(sync, second, 2)
    assert pending["known_facts"][0]["fact"]["value"] == "游泳"
    assert (
        commit(
            sync, job, second, fact("骑行", predicate="hobby", cardinality="set", operation="add")
        )["state"]
        == "completed"
    )
    third = "游泳改成登山，骑行继续。"
    job, pending = candidate(sync, third, 3)
    targets = next(
        item["target_refs"] for item in pending["known_facts"] if item["fact"]["value"] == "游泳"
    )
    assert (
        commit(
            sync,
            job,
            third,
            fact(
                "登山", predicate="hobby", cardinality="set", operation="replace", targets=targets
            ),
        )["state"]
        == "completed"
    )
    with sync.store.transaction() as db:
        current = [
            json.loads(row[0])["value"]
            for row in db.execute("SELECT payload FROM automatic_facts WHERE superseded_by IS NULL")
        ]
    assert set(current) == {"骑行", "登山"}


def test_plan_does_not_retire_reported_execution_and_retract_is_historical(sync):
    setup(sync)
    text = "这次出游计划14:10出发。"
    subject = dict(kind="event", id=None, type="outing", mention="这次出游")
    job, _ = candidate(sync, text)
    assert (
        commit(
            sync,
            job,
            text,
            fact("14:10", predicate="event.departure", subject=subject, modality="planned"),
        )["state"]
        == "completed"
    )
    actual = "这次出游实际14:25出发。"
    job, pending = candidate(sync, actual, 2)
    prior = pending["known_facts"][0]
    subject = prior["fact"]["subject"]
    assert (
        commit(
            sync,
            job,
            actual,
            fact("14:25", predicate="event.departure", subject=subject, modality="observed"),
        )["state"]
        == "completed"
    )
    wrong = "这次出游我说错了。"
    job, pending = candidate(sync, wrong, 3)
    cross = commit(
        sync,
        job,
        wrong,
        fact(
            "14:30",
            predicate="event.departure",
            subject=subject,
            modality="observed",
            operation="replace",
            targets=prior["target_refs"],
        ),
    )
    assert cross["state"] == "rejected"
    retract = commit(
        sync,
        job,
        wrong,
        fact(
            "14:10",
            predicate="event.departure",
            subject=subject,
            modality="planned",
            operation="retract",
            targets=prior["target_refs"],
        ),
    )
    assert retract["state"] == "completed"
    result = query(sync, "这次出游几点出发")
    facts = [item["fact"] for item in result["evidence"] if "fact" in item]
    assert any(value["value"] == "14:25" and value["modality"] == "observed" for value in facts)
    assert not any(value["value"] == "14:10" for value in facts)


def test_explicit_episode_forget_reuses_receipt_and_cannot_reindex(sync):
    from test_memory_context import ProofIssuer

    from tianshu_memory.memory_reindex import MemoryReindexer

    setup(sync)
    text = "明天去松林随身带雨伞。" * 30
    job, _ = candidate(sync, text)
    assert commit(sync, job, text)["state"] == "completed"
    result = query(sync, "我去松林带什么")
    target = next(item for item in result["evidence"] if item["kind"] == "episode")
    command = dict(
        command=sync.command(),
        scope=sync.scope(),
        batch_ref=None,
        item_id="forget:1",
        kind="forget_episode",
        target=dict(episode_ref=target["episode_id"], episode_version=target["episode_version"]),
        evidence_refs=[sync.admissions[-1]["source"]],
        proof_ref="test:proof",
    )
    proofs = ProofIssuer()
    sync.service.memory_context.proofs = proofs
    proofs.issue(command, "revision", [sync.account], [sync.scope()])
    response = post(sync, "forget", command)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "tombstoned"
    assert response.json()["record_ids"] == response.json()["group_ids"] == []
    assert post(sync, "forget", command).json() == response.json()
    receipt = sync.post(
        "memory/context/receipt",
        dict(
            query=request(sync, "松林")["query"],
            scope=sync.scope(),
            operation_id=command["command"]["idempotency_key"],
        ),
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["receipt"] == response.json()
    MemoryReindexer(sync.service).step(restart=True)
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM memory_documents").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_episode_index").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM suppression").fetchone()[0] == 1


def test_old_migration_backup_and_explicit_source_reindex(sync):
    import shutil

    from test_automatic_facts import add

    from tianshu_memory.memory_reindex import MemoryReindexer
    from tianshu_memory.store import Store

    initialize(sync)
    text = "很久以前我把备用钥匙放在蓝色花盆下面。"
    add(sync, text, "storage.key", memories=[])
    backup = sync.directory / "q20-backup.sqlite"
    sync.store.migrate_memory(backup)
    restored_path = sync.directory / "restored-q20.sqlite"
    shutil.copyfile(backup, restored_path)
    shutil.copyfile(str(backup) + ".source-guard.json", str(restored_path) + ".source-guard.json")
    with Store(restored_path).transaction() as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='automatic_fact_schema'").fetchone()[0]
            == "2"
        )
    sync.contracts.load_memory_v2()
    sync.config["callers"]["companion"]["operations"].append("context_query")
    sync.save()
    assert query(sync, "我的备用钥匙在哪里")["evidence"] == []
    progress = MemoryReindexer(sync.service).step()
    assert progress["completed"] and not progress["unavailable"]
    assert any(item.get("text") == text for item in query(sync, "我的备用钥匙在哪里")["evidence"])
    sync.grants[-1]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    assert query(sync, "我的备用钥匙在哪里")["evidence"] == []


def test_multiple_owners_and_unknown_does_not_expand_room(sync):
    from test_group_context import owner

    if sync.scope()["audience"] != "group":
        pytest.skip("Group multi-owner domain only")
    setup(sync)
    other = owner(sync)
    for actor, serial, name, pet in [(0, 1, "阿林", "团子"), (other, 2, "小明", "豆包")]:
        text = f"我叫{name}，我的猫叫{pet}。"
        job, _ = candidate(sync, text, serial, actor)
        assert (
            commit(
                sync,
                job,
                text,
                fact(name, predicate="identity.name"),
                fact(
                    pet,
                    predicate="pet.name",
                    subject=dict(kind="entity", id=None, type="cat", mention="我的猫"),
                ),
            )["state"]
            == "completed"
        )
    result = query(sync, "我的猫和小明的猫分别叫什么")
    resolution = result["bundle"]["selection_constraints"][0]["owner_resolution"]
    assert resolution["mode"] == "multiple"
    assert {target["person_id"] for target in resolution["targets"]} == {
        sync.scope(0)["person_id"],
        sync.scope(other)["person_id"],
    }, resolution
    assert {
        item["fact"]["value"]
        for item in result["evidence"]
        if item.get("fact", {}).get("predicate") == "pet.name"
    } == {"团子", "豆包"}
    partial = query(sync, "我的猫和不存在的猫分别叫什么")
    assert partial["query_results"][0]["status"] == "incomplete"
    assert {item["subject_person_id"] for item in partial["evidence"]} == {
        sync.scope()["person_id"]
    }


class SyntheticVectors:
    def __init__(self):
        self.document_batches = []
        self.query_calls = 0
        self.fail_query = False
        self.on_embed = lambda: None

    def call(self, operation, payload):
        if operation == "settings":
            return dict(
                mode="hybrid",
                rerank=False,
                candidates=32,
                revision="synthetic:1",
                embedding_revision="synthetic:1",
            )
        self.document_batches.append(payload["texts"])
        self.on_embed()
        return {"vectors": [[1, 0] for _ in payload["texts"]]}

    def embed_query(self, text, revision):
        self.query_calls += 1
        if self.fail_query:
            raise TimeoutError()
        return {"vectors": [[1, 0]]}


def test_persistent_incremental_vectors_restart_and_lexical_failure_fallback(sync):
    from tianshu_memory.memory_reindex import MemoryReindexer
    from tianshu_memory.retrieval import Retrieval

    setup(sync)
    job, _ = candidate(sync, "我喜欢乌龙茶。")
    commit(sync, job, "我喜欢乌龙茶。", fact("乌龙茶"))
    backend = SyntheticVectors()
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    indexer = MemoryReindexer(sync.service)
    assert indexer.index_step()["indexed"] == 2
    assert indexer.index_step()["indexed"] == 0
    documents = sum(map(len, backend.document_batches))
    result = query(sync, "我喜欢什么茶")
    assert result["query_results"][0]["coverage"]["index_complete"]
    assert sum(map(len, backend.document_batches)) == documents
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    assert query(sync, "我喜欢什么茶")["evidence"]
    assert sum(map(len, backend.document_batches)) == documents
    job, _ = candidate(sync, "我喜欢骑行。", 2)
    commit(
        sync,
        job,
        "我喜欢骑行。",
        fact("骑行", predicate="hobby", cardinality="set", operation="add"),
    )
    assert indexer.index_step()["indexed"] == 2
    assert sum(map(len, backend.document_batches)) == documents + 2
    backend.fail_query = True
    fallback = query(sync, "我喜欢什么茶")
    assert fallback["evidence"] and fallback["query_results"][0]["status"] == "incomplete"
    backend.fail_query = False
    with sync.store.transaction() as db:
        db.execute("UPDATE memory_vectors SET vector='broken-vector'")
    corrupted = query(sync, "我喜欢什么茶")
    assert corrupted["evidence"] and corrupted["query_results"][0]["status"] == "incomplete"


def test_late_vector_completion_cannot_revive_withdrawn_source(sync):
    from tianshu_memory.memory_reindex import MemoryReindexer
    from tianshu_memory.retrieval import Retrieval

    setup(sync)
    job, _ = candidate(sync, "明天带雨伞。")
    commit(sync, job, "明天带雨伞。")
    backend = SyntheticVectors()

    def revoke():
        sync.grants[-1]["state"] = "denied"
        sync.platform_head["sequence"] += 1

    backend.on_embed = revoke
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    assert MemoryReindexer(sync.service).index_step()["indexed"] == 0
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_documents").fetchone()[0] == 0


def test_old_source_remains_retrievable_after_more_than_4096_newer_index_rows(sync):
    """Two actual source proofs, many valid exact spans: exercise index scale without 4k network writes."""
    from tianshu_memory.domain import terms
    from tianshu_memory.memory_documents import discard, text_hash

    setup(sync)
    old = "我的赤鹫盒钥匙藏在书柜顶层。"
    job, _ = candidate(sync, old)
    commit(sync, job, old)
    lines = [f"noise-{index:04d}\n" for index in range(4100)]
    newer = "".join(lines)
    job, _ = candidate(sync, newer, 2)
    commit(sync, job, newer)
    key = sync.service.source_identity(sync.admissions[-1]["source"], sync.scope())
    with sync.store.transaction() as db:
        source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
        for row in db.execute(
            "SELECT id FROM memory_documents WHERE source_key=?", (key,)
        ).fetchall():
            discard(db, row[0])
        # A persisted index may contain many small spans (including after a
        # rebuild policy changes). Every fixture span is exact within this
        # authenticated source; only the derived segmentation is synthesized.
        offset = 0
        for index, line in enumerate(lines):
            identity = f"episode:synthetic-newer:{index:04d}"
            end = offset + len(line.encode())
            db.execute(
                "INSERT INTO memory_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    "episode",
                    source["scope"],
                    None,
                    key,
                    source["revision"],
                    source["epoch"],
                    text_hash(line),
                    line,
                    "2026-10-11T00:00:02Z",
                    offset,
                    end,
                ),
            )
            db.execute(
                "INSERT INTO memory_episode_index VALUES (?,?,?)",
                (identity, source["scope"], " ".join(terms(line))),
            )
            offset = end
    result = query(sync, "我的赤鹫盒钥匙藏哪里")
    assert any(item.get("text") == old for item in result["evidence"])
    constraint = result["bundle"]["selection_constraints"][0]
    assert constraint["candidate_count"] == 4101 and constraint["candidate_domain_complete"]


def test_fictional_and_real_properties_are_separate_and_episodes_keep_reality(sync):
    setup(sync)
    job, _ = candidate(sync, "我喜欢乌龙茶。")
    assert commit(sync, job, "我喜欢乌龙茶。", fact("乌龙茶"))["state"] == "completed"
    story = "在故事里我喜欢月光茶。"
    job, pending = candidate(sync, story, 2, reality="fictional")
    assert pending["known_facts"] == []
    assert commit(sync, job, story, fact("月光茶"))["state"] == "completed"
    result = query(sync, "我喜欢什么茶")
    values = {
        item["fact"]["value"]: item["unit"]["reality"]
        for item in result["evidence"]
        if "fact" in item
    }
    assert values == {"乌龙茶": "real", "月光茶": "fictional"}
    assert {
        item["text"]: item["reality"] for item in result["evidence"] if item["kind"] == "episode"
    } == {"我喜欢乌龙茶。": "real", story: "fictional"}


@pytest.mark.parametrize("new_value", ["乌龙茶", "红茶"])
def test_legacy_current_can_be_asserted_as_typed_without_promoting_old_statement(sync, new_value):
    from test_automatic_facts import add

    from tianshu_memory.memory_reindex import MemoryReindexer

    initialize(sync)
    old_text = "我喜欢乌龙茶，茶杯放书架。"
    receipt, _ = add(sync, old_text, "preference.drink", "乌龙茶")
    old_id = receipt["group_ids"][0]
    with sync.store.transaction() as db:
        old_record = db.execute(
            "SELECT payload FROM records WHERE group_id=?", (old_id,)
        ).fetchone()[0]
        old_fact = db.execute(
            "SELECT payload FROM automatic_facts WHERE group_id=?", (old_id,)
        ).fetchone()[0]
    sync.store.migrate_memory(sync.directory / "legacy-upgrade.sqlite")
    sync.contracts.load_memory_v2()
    sync.config["callers"]["companion"]["operations"].append("context_query")
    sync.save()
    text = f"我现在喜欢{new_value}。"
    job, pending = candidate(sync, text, 2)
    assert pending["known_facts"][0]["representation"] == "legacy_unstructured"
    written = commit(sync, job, text, fact(new_value))
    assert written["state"] == "completed"
    assert written["result"]["group_ids"] != [old_id]
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT payload FROM records WHERE group_id=?", (old_id,)).fetchone()[0]
            == old_record
        )
        assert (
            db.execute(
                "SELECT payload FROM automatic_facts WHERE group_id=?", (old_id,)
            ).fetchone()[0]
            == old_fact
        )
    result = query(sync, "我现在喜欢什么茶")
    assert [item["fact"]["value"] for item in result["evidence"] if "fact" in item] == [new_value]
    assert not any(item["kind"] == "legacy_fact" for item in result["evidence"])
    MemoryReindexer(sync.service).step()
    history = request(sync, "我以前喜欢什么茶，茶杯放哪里")
    history["queries"][0]["intent"] = "history"
    result = post(sync, "query", history)
    assert result.status_code == 200, result.text
    assert any(item.get("text") == old_text for item in result.json()["evidence"])


def test_historical_assert_does_not_conflict_with_or_retire_current_value(sync):
    setup(sync)
    job, _ = candidate(sync, "我现在住北京。")
    assert (
        commit(sync, job, "我现在住北京。", fact("北京", predicate="residence"))["state"]
        == "completed"
    )
    job, _ = candidate(sync, "我以前住杭州。", 2)
    past = dict(fact("杭州", predicate="residence"), state="historical")
    assert commit(sync, job, "我以前住杭州。", past)["state"] == "completed"
    current = query(sync, "我现在住哪里")
    assert [item["fact"]["value"] for item in current["evidence"] if "fact" in item] == ["北京"]
    history = request(sync, "我以前住哪里")
    history["queries"][0]["intent"] = "history"
    response = post(sync, "query", history)
    assert response.status_code == 200, response.text
    assert any(
        item.get("fact", {}).get("value") == "杭州" and item["kind"] == "historical_fact"
        for item in response.json()["evidence"]
    )


def test_negative_selection_cannot_be_forged_and_new_source_invalidates_real_negative(sync):
    setup(sync)
    job, _ = candidate(sync, "我喜欢乌龙茶。")
    commit(sync, job, "我喜欢乌龙茶。", fact("乌龙茶"))
    selected = query(sync, "我喜欢什么茶")
    forged = copy.deepcopy(selected["bundle"])
    for constraint in forged["selection_constraints"]:
        constraint["result_status"] = "no_match"
        constraint["proof_kinds"].append("no_match")
    for name in (
        "group_dependencies",
        "episode_dependencies",
        "source_dependencies",
        "evidence_ids",
    ):
        forged[name] = []
    forged["evidence_digest"] = fingerprint([])
    response = post(
        sync,
        "verify",
        dict(
            schema_version=2,
            query=request(sync, "茶")["query"],
            requested_scope=sync.scope(),
            bundle=forged,
            checks=[],
        ),
    )
    assert response.status_code == 409, response.text
    genuine = query(sync, "unseen_material_xyz")
    assert genuine["query_results"][0]["status"] == "no_match"
    verify = dict(
        schema_version=2,
        query=request(sync, "材料")["query"],
        requested_scope=sync.scope(),
        bundle=genuine["bundle"],
        checks=[],
    )
    assert post(sync, "verify", verify).status_code == 200
    job, _ = candidate(sync, "unseen_material_xyz 已经出现。", 2)
    commit(sync, job, "unseen_material_xyz 已经出现。")
    assert post(sync, "verify", verify).status_code == 409


def test_time_constraint_precedes_semantic_top32(sync):
    from tianshu_memory.domain import terms
    from tianshu_memory.memory_documents import discard, text_hash
    from tianshu_memory.memory_reindex import MemoryReindexer
    from tianshu_memory.retrieval import Retrieval

    setup(sync)
    old = "我的计划在旧纸条上。"
    job, _ = candidate(sync, old)
    commit(sync, job, old)
    with sync.store.transaction() as db:
        identity = db.execute("SELECT id FROM memory_documents WHERE kind='episode'").fetchone()[0]
        db.execute("UPDATE memory_documents SET id='episode:zz-old-target' WHERE id=?", (identity,))
        db.execute(
            "UPDATE memory_episode_index SET document_id='episode:zz-old-target' WHERE document_id=?",
            (identity,),
        )
    lines = [f"我的计划新记录{index:02d}。\n" for index in range(35)]
    newer = "".join(lines)
    job, _ = candidate(sync, newer, 2)
    commit(sync, job, newer)
    key = sync.service.source_identity(sync.admissions[-1]["source"], sync.scope())
    with sync.store.transaction() as db:
        source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
        for row in db.execute(
            "SELECT id FROM memory_documents WHERE source_key=?", (key,)
        ).fetchall():
            discard(db, row[0])
        offset = 0
        for index, line in enumerate(lines):
            identity = f"episode:aa-newer:{index:02d}"
            end = offset + len(line.encode())
            db.execute(
                "INSERT INTO memory_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    "episode",
                    source["scope"],
                    None,
                    key,
                    source["revision"],
                    source["epoch"],
                    text_hash(line),
                    line,
                    "2026-10-11T00:00:02Z",
                    offset,
                    end,
                ),
            )
            db.execute(
                "INSERT INTO memory_episode_index VALUES (?,?,?)",
                (identity, source["scope"], " ".join(terms(line))),
            )
            offset = end
    sync.service.retrieval = Retrieval(SyntheticVectors())
    sync.service.retrieval.store = sync.store
    indexer = MemoryReindexer(sync.service)
    assert indexer.index_step(batch_size=32)["indexed"] == 32
    assert indexer.index_step(batch_size=32)["indexed"] == 4
    filtered = request(sync, "我的计划")
    filtered["queries"][0]["time_filter"] = dict(
        basis="observed_at", **{"from": "2026-10-11T00:00:01Z", "to": "2026-10-11T00:00:02Z"}
    )
    result = post(sync, "query", filtered)
    assert result.status_code == 200, result.text
    assert [item["text"] for item in result.json()["evidence"]] == [old]


def test_selection_proof_key_survives_post_upgrade_backup_and_reopen(sync):
    import shutil

    from tianshu_memory.store import Store

    setup(sync)
    with sync.store.readonly() as db:
        key = db.execute(
            "SELECT value FROM metadata WHERE key='memory_selection_proof_key'"
        ).fetchone()[0]
        assert len(key) == 64
    backup = sync.directory / "post-upgrade.sqlite"
    sync.store.backup(backup)
    shutil.copyfile(sync.store.recovery_path, str(backup) + ".source-guard.json")
    restored = Store(backup)
    try:
        with restored.readonly() as db:
            assert (
                db.execute(
                    "SELECT value FROM metadata WHERE key='memory_selection_proof_key'"
                ).fetchone()[0]
                == key
            )
    finally:
        restored.close()
