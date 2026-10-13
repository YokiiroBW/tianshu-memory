"""Real Memory candidate HTTP and guarded store; synthetic HTTPS owners and extractor output."""

import copy
import json
import shutil

import pytest
from test_candidate_extraction import PATH
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync
from test_user_actions import EXPIRES, execute, profile_action, setup_user

from tianshu_memory.domain import Fault, canonical, fingerprint
from tianshu_memory.store import Store


def initialize(h):
    h.store.migrate_context(h.directory / "pre-context.sqlite")
    h.store.migrate_facts(h.directory / "pre-facts.sqlite")
    h.test_contents = {}

    def include(kind, body, response):
        if body.get("include_content"):
            for physical in response["physicals"]:
                physical["content"] = copy.deepcopy(h.test_contents[canonical(physical["key"])])

    h.mutate = include


def add(
    h,
    text,
    key,
    value=None,
    *,
    second=1,
    state="current",
    valid_from=None,
    valid_to=None,
    conditions=(),
    negations=(),
    explicit=False,
):
    physical, admission, grant = [
        copy.deepcopy(items[0]) for items in (h.physicals, h.admissions, h.grants)
    ]
    message_id = f"quality-source:{second}"
    physical.update(
        key=dict(physical["key"], message_id=message_id),
        physical_receipt_id=f"quality-physical:{second}",
    )
    admission["selector"]["key"] = physical["key"]
    admission["source"]["message_key"]["message_id"] = message_id
    admission["source"]["receipt_id"] = f"quality-admission:{second}"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    grant.update(selector=admission["selector"], entry_id=f"quality-entry:{second}")
    data = dict(
        message_key=dict(physical["key"], revision=physical["revision"]),
        author=physical["author"],
        sent_at=f"2026-10-11T00:00:{second:02d}Z",
        kind=physical["kind"],
        parts=[dict(kind="text", text=text)],
        reply_refs=[],
        mentioned_accounts=[],
    )
    physical["content_digest"] = fingerprint(data)
    h.test_contents[canonical(physical["key"])] = data
    h.physicals.append(physical)
    h.admissions.append(admission)
    h.grants.append(grant)
    turn = copy.deepcopy(h.turns[0])
    old_id = turn["turn_id"]
    new_id = f"quality-turn:{second}"
    turn = json.loads(canonical(turn).replace(old_id, new_id))
    turn["input_sources"] = [admission["source"]]
    event = turn["committed_event"]
    event.update(event_id=f"quality-event:{second}", sources=[admission["source"]])
    with h.store.transaction() as db:
        event["scope_version"] = h.service._scope_version(db, h.scope())
    # Turn and committed event assert the same owner state.
    if "scope_version" in turn:
        turn["scope_version"] = event["scope_version"]
    h.turns.append(turn)
    h.core_head["sequence"] += 1
    h.platform_head["sequence"] += 1
    if explicit:
        from tianshu_memory.memory_context import MemoryContext

        h.service.contracts.load_context()
        proposal = dict(
            command=h.command(),
            scope=h.scope(),
            batch_ref=None,
            item_id="explicit:1",
            kind="upsert",
            target=dict(
                category="evidence",
                field_key="test.storage",
                item_key=None,
                record_id=None,
                expected_version=None,
            ),
            units=[
                dict(
                    statement=text,
                    conditions=[],
                    negations=[],
                    valid_time="当前",
                    uncertainty="confirmed",
                    reality="real",
                    sources=[admission["source"]],
                )
            ],
            evidence_refs=[admission["source"]],
            proof_ref=None,
        )
        h.service.contracts.validate("memory-context#proposal_request", proposal)
        receipt = MemoryContext(h.service).propose(proposal, h.contexts["synthetic-viewer:0"])
        assert receipt["state"] == "committed"
        # In a real turn the tool commits before the delivered-turn event is published.
        with h.store.transaction() as db:
            event["scope_version"] = h.service._scope_version(db, h.scope())
        if "scope_version" in turn:
            turn["scope_version"] = event["scope_version"]
        h.core_head["sequence"] += 1
        response = h.post("memory/turn-commits", event)
        assert response.status_code == 200, response.text
        job = response.json()["candidate_job_ref"]
        pending = h.post(PATH, dict(job_id=job, output=None))
        assert pending.json()["content"][0]["text"] == ""
        completed = h.post(PATH, dict(job_id=job, output={"memories": []}))
        assert completed.status_code == 200, completed.text
        assert completed.json()["state"] == "completed", completed.text
        return receipt, job
    response = h.post("memory/turn-commits", event)
    assert response.status_code == 200, response.text
    job = response.json()["candidate_job_ref"]
    pending = h.post(PATH, dict(job_id=job, output=None))
    assert pending.status_code == 200, pending.text
    assert pending.json()["extraction_schema"] == 2
    proposed = dict(
        memories=[
            dict(
                statement=text,
                conditions=list(conditions),
                negations=list(negations),
                valid_time="当前" if state == "current" else "过去",
                evidence=[dict(source_index=0, quote=text)],
                fact=dict(
                    key=key,
                    value=value or text,
                    state=state,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    keywords=[key, text[:20]],
                ),
            )
        ]
    )
    committed = h.post(PATH, dict(job_id=job, output=proposed))
    assert committed.status_code == 200, committed.text
    assert committed.json()["state"] == "completed", committed.text
    return committed.json()["result"], job


def recall(h, text):
    query = h.selection(budget=100000)
    query.update(query_text=text, selection=["evidence"])
    response = h.post("memory/select", query)
    assert response.status_code == 200, response.text
    return response.json()["selected_units"]


def test_correction_recall_retains_auditable_past_without_old_current(sync):
    initialize(sync)
    first, _ = add(sync, "我住在杭州西湖区", "residence")
    newer, _ = add(sync, "现在搬到杭州滨江区了", "residence", second=2)
    units = recall(sync, "我目前的住所在哪")
    assert {u["record_id"] for u in units} == set(newer["record_ids"])
    history = recall(sync, "我以前的住址")
    assert set(first["record_ids"]) <= {u["record_id"] for u in history}
    assert any("非当前" in u["valid_time"] or "不能作为当前" in u["valid_time"] for u in history)
    with sync.store.transaction() as db:
        assert (
            db.execute(
                "SELECT superseded_by FROM automatic_facts WHERE group_id=?",
                (first["group_ids"][0],),
            ).fetchone()[0]
            == newer["group_ids"][0]
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM history WHERE record_id=?", (first["record_ids"][0],)
            ).fetchone()[0]
            == 2
        )


def test_paraphrase_deduplicates_and_preserves_both_sources_and_quotes(sync):
    initialize(sync)
    first, job = add(sync, "我家猫叫糖球", "pet.cat.name", "糖球")
    repeated, _ = add(sync, "养的猫咪名字是糖球", "pet.cat.name", "糖球", second=2)
    assert repeated["record_ids"] == first["record_ids"]
    assert sync.post(PATH, dict(job_id=job, output=None)).json()["result"] == first
    units = recall(sync, "我家喵叫什么")
    assert len(units) == 1 and "糖球" in units[0]["statement"]
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM lineage WHERE group_id=?", (first["group_ids"][0],)
            ).fetchone()[0]
            == 2
        )
        assert db.execute("SELECT COUNT(*) FROM automatic_evidence").fetchone()[0] == 2
    assert recall(sync, "我的银行卡号码是什么") == []


def test_past_and_expired_reports_do_not_replace_current_state(sync):
    initialize(sync)
    current, _ = add(sync, "我现在住苏州", "residence")
    add(sync, "以前住南京", "residence", second=2, state="historical")
    assert {u["record_id"] for u in recall(sync, "我现在住哪")} == set(current["record_ids"])
    add(
        sync,
        "过去出差去合肥，于2026年9月30日已经结束",
        "event.business_trip",
        second=3,
        state="historical",
        valid_to="2026-09-30T00:00:00Z",
    )
    assert recall(sync, "我目前正在出差吗") == []
    assert any("已经结束" in u["statement"] for u in recall(sync, "我过去的出差"))


def test_source_revocation_invalidates_consolidated_fact_and_hints(sync):
    initialize(sync)
    add(sync, "我喜欢乌龙茶", "beverage.tea", "乌龙茶")
    add(sync, "喝茶更偏爱乌龙", "beverage.tea", "乌龙茶", second=2)
    sync.grants[-1]["state"] = "denied"
    sync.platform_head["sequence"] += 1
    assert recall(sync, "泡什么茶") == []


def test_migration_backup_preserves_authority_and_rejects_stale_restore(sync):
    initialize(sync)
    backup = sync.directory / "pre-facts.sqlite"
    copy = sync.directory / "restored.sqlite"
    shutil.copyfile(backup, copy)
    shutil.copyfile(
        backup.with_name(backup.name + ".source-guard.json"), str(copy) + ".source-guard.json"
    )
    restored = Store(copy)
    with restored.transaction() as db:
        assert (
            db.execute("SELECT 1 FROM metadata WHERE key='automatic_fact_schema'").fetchone()
            is None
        )
    # Pairing the old DB with the current independent guard is deliberately rejected.
    shutil.copyfile(sync.store.recovery_path, str(copy) + ".source-guard.json")
    with pytest.raises(Fault):
        Store(copy)


def test_profile_preparation_preserves_qualifiers_and_requires_existing_grant(sync):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Private evidence is the authorized profile-source path under test")
    initialize(sync)
    written, _ = add(
        sync,
        "我晚上不喝红茶，白天可以",
        "beverage.tea",
        conditions=["白天可以"],
        negations=["晚上不喝红茶"],
    )
    app = setup_user(sync)
    action = dict(
        operation="prepare_evidence_profile",
        origin={"assertion_ref": "synthetic-viewer:0"},
        records=[dict(record_id=written["record_ids"][0], record_version=1)],
        field_key="interest.tea",
        sharing="public_preference",
        conversation_id=None,
        expires_at=EXPIRES,
    )
    prepared = execute(app, action)
    assert prepared["approval_required"] and not prepared["published"]
    draft = prepared["draft"]
    assert draft["units"][0]["negations"] == ["晚上不喝红茶"]

    def read_profile():
        request = sync.profile_request(sync.contexts["synthetic-viewer:0"], target=draft["subject"])
        request.update(query_text="泡茶有什么讲究", selection=["interest"])
        response = sync.post("memory/profiles/select", request)
        assert response.status_code == 200, response.text
        return response.json()["selected_units"]

    assert not read_profile()
    forged = copy.deepcopy(action)
    forged["records"][0]["record_id"] = "nonexistent:private-record"
    with pytest.raises(Fault) as unauthorized:
        app.execute(forged, principal="unregistered-user", credential="x" * 40)
    assert unauthorized.value.status == 401
    approval = execute(app, profile_action(draft))
    execute(app, profile_action(draft, operation="publish_profile", ref=approval))
    assert read_profile()[0]["negations"] == ["晚上不喝红茶"]
    sync.config["local_users"]["owner"]["profile_permissions"] = []
    sync.save()
    with pytest.raises(Fault):
        execute(app, action)


def test_explicit_source_is_not_written_again_by_automatic_learning(sync):
    initialize(sync)
    receipt, _ = add(sync, "我的测试物品在书桌抽屉", "test.storage", explicit=True)
    assert len(receipt["record_ids"]) == 1
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE state='active'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM automatic_facts").fetchone()[0] == 0


def test_pet_name_question_does_not_inject_the_persons_name(sync):
    initialize(sync)
    add(sync, "我的名字叫小安", "identity.name", "小安")
    pet, _ = add(sync, "我养的猫叫饼干", "pet.cat.name", "饼干", second=2)
    units = recall(sync, "我的宠物名字和种类是什么")
    assert {u["record_id"] for u in units} == set(pet["record_ids"])
    assert any("小安" in u["statement"] for u in recall(sync, "我的名字是什么"))


def test_malformed_fact_state_is_rejected_as_input():
    from tianshu_memory.automatic_facts import validate

    with pytest.raises(Fault) as rejected:
        validate(
            dict(
                key="residence", value="测试", state=[], valid_from=None, valid_to=None, keywords=[]
            )
        )
    assert rejected.value.status == 400
