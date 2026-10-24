"""Typed batches consume exact targets once, using an actual source statement."""

import json

import pytest
from test_candidate_extraction import PATH
from test_fact_provenance import ref
from test_memory_v2 import candidate, commit, fact, query, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync
from test_typed_source_semantics import propose


def target_state(h, group):
    with h.store.transaction() as db:
        value = dict(
            db.execute("SELECT * FROM automatic_facts WHERE group_id=?", (group,)).fetchone()
        )
        record = dict(db.execute("SELECT * FROM records WHERE group_id=?", (group,)).fetchone())
        lineage = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM lineage WHERE group_id=? ORDER BY source_key", (group,)
            )
        ]
    return value, record, lineage


def known_email(h, *, modality="asserted"):
    setup(h)
    text = "我的邮箱没有改动。"
    job, _ = candidate(h, text)
    result = commit(h, job, text, fact("未改动", predicate="email.status", modality=modality))
    assert result["state"] == "completed"
    return result["result"]["group_ids"][0]


def assert_pending_without_receipt(h, job):
    with h.store.transaction() as db:
        assert db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()[0] == "pending"
        assert db.execute("SELECT 1 FROM write_ledger WHERE job_id=?", (job,)).fetchone() is None


@pytest.mark.parametrize(
    "first_operation,second_operation",
    [("replace", "retract"), ("confirm", "retract"), ("retract", "retract")],
)
def test_repeated_target_rejects_the_entire_batch_and_can_be_repaired(
    sync, first_operation, second_operation
):
    setup(sync)
    text = "我用棉浆纸，也做木工。"
    job, _ = candidate(sync, text)
    result = commit(
        sync,
        job,
        text,
        fact("棉浆纸", predicate="preference.paper_type"),
        fact("做木工", predicate="activity.recurring", cardinality="set", operation="add"),
    )
    paper, wood = result["result"]["group_ids"]
    before = {g: target_state(sync, g) for g in (paper, wood)}
    paper_ref, wood_ref = ref(sync, paper), ref(sync, wood)
    text = "木工先彻底停掉。画画的纸改用竹浆纸，不再用棉浆纸。"
    job, _ = candidate(sync, text, 2)
    stop = fact(
        "做木工",
        predicate="activity.recurring",
        cardinality="set",
        operation="retract",
        targets=[wood_ref],
    )
    first = fact(
        "竹浆纸" if first_operation == "replace" else "棉浆纸",
        predicate="preference.paper_type",
        operation=first_operation,
        targets=[paper_ref],
    )
    second = fact(
        "棉浆纸",
        predicate="preference.paper_type",
        operation=second_operation,
        targets=[paper_ref],
    )
    rejected = commit(sync, job, text, stop, first, second)
    assert rejected["state"] == "rejected"
    assert rejected["result"] == {"error_code": "invalid_input"}
    assert {g: target_state(sync, g) for g in (paper, wood)} == before
    assert_pending_without_receipt(sync, job)
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM automatic_facts").fetchone()[0] == 2
    # Existing proposal repair can submit a coherent replacement, retaining the
    # independent woodworking retract. The server never drops an operation.
    replacement = dict(first, value="竹浆纸", keywords=["竹浆纸"], operation="replace")
    repaired = commit(sync, job, text, stop, replacement)
    assert repaired["state"] == "completed"
    successor_wood, successor_paper = repaired["result"]["group_ids"]
    assert target_state(sync, wood)[0]["superseded_by"] == successor_wood
    assert target_state(sync, paper)[0]["superseded_by"] == successor_paper
    assert len(target_state(sync, successor_wood)[2]) == 2


def test_a_batch_cannot_invent_a_second_target_version_to_chain_its_own_writes(sync):
    group = known_email(sync)
    target = ref(sync, group)
    before = target_state(sync, group)
    text = "邮箱已改动，撤回之前那条记录。"
    job, _ = candidate(sync, text, 2)
    result = commit(
        sync,
        job,
        text,
        fact("已改动", predicate="email.status", operation="replace", targets=[target]),
        fact(
            "未改动",
            predicate="email.status",
            operation="retract",
            targets=[dict(target, record_version=target["record_version"] + 1)],
        ),
    )
    assert result["state"] == "rejected"
    assert target_state(sync, group) == before
    assert_pending_without_receipt(sync, job)


@pytest.mark.parametrize(
    "operation,before_mode,after_mode",
    [
        ("replace", "asserted", "uncertain"),
        ("replace", "observed", "uncertain"),
        ("replace", "asserted", "asserted"),
        ("replace", "observed", "observed"),
        ("retract", "asserted", "asserted"),
        ("retract", "observed", "observed"),
        ("confirm", "asserted", "asserted"),
    ],
)
def test_a_question_cannot_consume_a_known_target_even_when_model_labels_it_known(
    sync, operation, before_mode, after_mode
):
    group = known_email(sync, modality=before_mode)
    before = target_state(sync, group)
    text = "我的邮箱是不是也改了？"
    job, _ = candidate(sync, text, 2)
    value = "不确定是否已改动" if operation == "replace" else "未改动"
    result = commit(
        sync,
        job,
        text,
        fact(
            value,
            predicate="email.status",
            modality=after_mode,
            operation=operation,
            targets=[ref(sync, group)],
        ),
    )
    assert result["state"] == "rejected"
    assert target_state(sync, group) == before
    assert_pending_without_receipt(sync, job)
    assert any(
        e.get("fact", {}).get("value") == "未改动" for e in query(sync, "我的邮箱")["evidence"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "我不知道邮箱是否改动了。",
        "旧说法是不是不可靠，能撤回吗？",
        "如果旧说法不可靠，就撤回之前那条。",
        "‘撤回之前那条邮箱记录’。",
        "他说：‘撤回之前那条邮箱记录’。",
        "我没有撤回之前那条邮箱记录。",
        "我打算撤回之前那条邮箱记录。",
        "旧说法并非不可靠。",
        "I don't know the current email state.",
        "我的邮箱是不是也改了？今天我已经买了牛奶。",
        "我的邮箱是不是也改了，晚饭现在改为待定。",
        "我不知道邮箱是否改了，晚饭现在改为待定。",
        "我不知道邮箱是否变了，晚饭那条旧说法不可靠。",
    ],
)
def test_source_uncertainty_quote_or_condition_cannot_be_laundered_as_a_retraction(sync, text):
    group = known_email(sync)
    before = target_state(sync, group)
    job, _ = candidate(sync, text, 2)
    result = commit(
        sync,
        job,
        text,
        fact("未改动", predicate="email.status", operation="retract", targets=[ref(sync, group)]),
    )
    assert result["state"] == "rejected"
    assert target_state(sync, group) == before


@pytest.mark.parametrize("operation", ["retract", "confirm", "replace"])
@pytest.mark.parametrize(
    "text,quote",
    [
        ("我的邮箱是不是也改了？今天我已经买了牛奶。", "今天我已经买了牛奶。"),
        ("我不知道邮箱是否改了，晚饭现在改为待定。", "晚饭现在改为待定。"),
        ("我不知道邮箱是否变了，晚饭那条旧说法不可靠。", "晚饭那条旧说法不可靠。"),
    ],
)
def test_a_precise_unrelated_quote_cannot_control_the_known_target(sync, operation, text, quote):
    group = known_email(sync)
    before = target_state(sync, group)
    job, _ = candidate(sync, text, 2)
    result = propose(
        sync,
        job,
        text,
        fact("未改动", predicate="email.status", operation=operation, targets=[ref(sync, group)]),
        quote=quote,
    )
    assert result["state"] == "rejected"
    assert target_state(sync, group) == before
    assert_pending_without_receipt(sync, job)


def test_a_known_plan_also_requires_a_declaration_before_its_target_is_consumed(sync):
    setup(sync)
    text = "我计划周六去河岸。"
    job, _ = candidate(sync, text)
    original = commit(
        sync, job, text, fact("去河岸", predicate="event.destination", modality="planned")
    )["result"]["group_ids"][0]
    before = target_state(sync, original)
    text = "周六是不是改去山谷了？"
    job, _ = candidate(sync, text, 2)
    replacement = fact(
        "去山谷",
        predicate="event.destination",
        modality="planned",
        operation="replace",
        targets=[ref(sync, original)],
    )
    assert commit(sync, job, text, replacement)["state"] == "rejected"
    assert target_state(sync, original) == before
    assert_pending_without_receipt(sync, job)
    text = "周六的计划现在改为去山谷。"
    job, _ = candidate(sync, text, 3)
    accepted = commit(sync, job, text, replacement)
    assert accepted["state"] == "completed"
    assert target_state(sync, original)[0]["superseded_by"] == accepted["result"]["group_ids"][0]


def test_a_declaration_can_replace_a_known_rule_with_a_conditional_rule(sync):
    setup(sync)
    text = "会议安排为直接召开。"
    job, _ = candidate(sync, text)
    original = commit(sync, job, text, fact("直接召开", predicate="meeting.rule"))["result"][
        "group_ids"
    ][0]
    text = "会议现在改为若下雨才开。"
    job, _ = candidate(sync, text, 2)
    accepted = commit(
        sync,
        job,
        text,
        fact(
            "若下雨才开",
            predicate="meeting.rule",
            modality="conditional",
            operation="replace",
            targets=[ref(sync, original)],
        ),
    )
    assert accepted["state"] == "completed"
    assert target_state(sync, original)[0]["superseded_by"] == accepted["result"]["group_ids"][0]


@pytest.mark.parametrize("operation", ["retract", "replace"])
@pytest.mark.parametrize(
    "text,predicate,original_value",
    [
        ("周六不去了。", "event.attendance", "周六郊游"),
        ("那次活动我不再参加。", "event.attendance", "参加活动"),
        ("周六的票退了。", "ticket.booking", "周六的票"),
    ],
)
def test_existing_source_cancellation_can_retire_a_known_plan(
    sync, operation, text, predicate, original_value
):
    setup(sync)
    original_text = f"我的计划是{original_value}。"
    job, _ = candidate(sync, original_text)
    first = commit(
        sync, job, original_text, fact(original_value, predicate=predicate, modality="planned")
    )["result"]["group_ids"][0]
    job, _ = candidate(sync, text, 2)
    accepted = commit(
        sync,
        job,
        text,
        fact(
            original_value if operation == "retract" else "已取消",
            predicate=predicate,
            modality="planned" if operation == "retract" else "asserted",
            operation=operation,
            targets=[ref(sync, first)],
        ),
    )
    assert accepted["state"] == "completed", accepted
    successor = accepted["result"]["group_ids"][0]
    assert target_state(sync, first)[0]["superseded_by"] == successor
    if operation == "retract":
        assert len(target_state(sync, successor)[2]) == 2


@pytest.mark.parametrize("operation", ["retract", "replace"])
@pytest.mark.parametrize(
    "text,predicate,original_value",
    [
        ("我没有取消周六的计划。", "event.attendance", "周六郊游"),
        ("我不取消周六的计划。", "event.attendance", "周六郊游"),
        ("我打算取消周六的计划。", "event.attendance", "周六郊游"),
        ("我还没有退款。", "ticket.booking", "周六的票"),
    ],
)
def test_negated_or_unperformed_cancellation_does_not_retire_a_known_plan(
    sync, operation, text, predicate, original_value
):
    setup(sync)
    original_text = f"我的计划是{original_value}。"
    job, _ = candidate(sync, original_text)
    first = commit(
        sync, job, original_text, fact(original_value, predicate=predicate, modality="planned")
    )["result"]["group_ids"][0]
    before = target_state(sync, first)
    job, _ = candidate(sync, text, 2)
    rejected = commit(
        sync,
        job,
        text,
        fact(
            original_value if operation == "retract" else "已取消",
            predicate=predicate,
            modality="planned" if operation == "retract" else "asserted",
            operation=operation,
            targets=[ref(sync, first)],
        ),
    )
    assert rejected["state"] == "rejected"
    assert target_state(sync, first) == before
    assert_pending_without_receipt(sync, job)


def test_independent_uncertain_material_does_not_retire_the_known_fact(sync):
    group = known_email(sync)
    before = target_state(sync, group)
    text = "我的邮箱是不是改了？"
    job, _ = candidate(sync, text, 2)
    result = commit(
        sync, job, text, fact("不确定是否改动", predicate="email.status", modality="uncertain")
    )
    assert result["state"] == "completed"
    assert target_state(sync, group) == before
    current = result["result"]["group_ids"][0]
    assert (
        json.loads(target_state(sync, current)[0]["payload"])["canonical"]["modality"]
        == "uncertain"
    )


@pytest.mark.parametrize("operation", ["retract", "replace"])
def test_explicit_reliability_withdrawal_remains_valid_when_current_value_is_unknown(
    sync, operation
):
    group = known_email(sync)
    text = "我现在不能确定邮箱是否变了，所以撤回之前那条没改过的说法。"
    job, _ = candidate(sync, text, 2)
    value = fact(
        "未改动" if operation == "retract" else "现在不确定",
        predicate="email.status",
        modality="asserted" if operation == "retract" else "uncertain",
        operation=operation,
        targets=[ref(sync, group)],
    )
    result = commit(sync, job, text, value)
    assert result["state"] == "completed", result
    successor = result["result"]["group_ids"][0]
    assert target_state(sync, group)[0]["superseded_by"] == successor
    if operation == "retract":
        assert len(target_state(sync, successor)[2]) == 2


def test_a_declarative_correction_uses_its_own_quote_beside_an_unrelated_question(sync):
    group = known_email(sync)
    text = "牛奶是不是没了？我的邮箱现在确实改了。"
    job, _ = candidate(sync, text, 2)
    result = propose(
        sync,
        job,
        text,
        fact("已改动", predicate="email.status", operation="replace", targets=[ref(sync, group)]),
        quote="我的邮箱现在确实改了。",
    )
    assert result["state"] == "completed"
    assert target_state(sync, group)[0]["superseded_by"] == result["result"]["group_ids"][0]


@pytest.mark.parametrize("text", ["我的邮箱状态现在明确改为待定。", "之前邮箱没改那条说法不可靠。"])
def test_declaring_a_pending_value_or_unreliable_old_claim_is_not_just_unknown_material(sync, text):
    group = known_email(sync)
    job, _ = candidate(sync, text, 2)
    result = commit(
        sync,
        job,
        text,
        fact(
            "待定",
            predicate="email.status",
            modality="uncertain",
            operation="replace",
            targets=[ref(sync, group)],
        ),
    )
    assert result["state"] == "completed", result
    assert target_state(sync, group)[0]["superseded_by"] == result["result"]["group_ids"][0]


def test_a_literal_author_name_is_not_mistaken_for_uncertain_control_language(sync):
    setup(sync)
    text = "我叫合成旧名。"
    job, _ = candidate(sync, text)
    first = commit(sync, job, text, fact("合成旧名", predicate="name"))["result"]["group_ids"][0]
    text = "我现在改叫‘不确定’。"
    job, _ = candidate(sync, text, 2)
    result = commit(
        sync,
        job,
        text,
        fact("不确定", predicate="name", operation="replace", targets=[ref(sync, first)]),
    )
    assert result["state"] == "completed"
    assert target_state(sync, first)[0]["superseded_by"] == result["result"]["group_ids"][0]


def test_an_owned_object_can_be_literally_renamed_to_an_uncertainty_word(sync):
    setup(sync)
    text = "第一支钢笔叫晨光。"
    subject = dict(kind="entity", id=None, type="pen", mention="第一支钢笔")
    job, _ = candidate(sync, text)
    first = commit(sync, job, text, fact("晨光", predicate="object.name", subject=subject))[
        "result"
    ]["group_ids"][0]
    stored = json.loads(target_state(sync, first)[0]["payload"])
    subject = stored["canonical"]["subject"]
    text = "第一支钢笔现在改名叫不确定。"
    job, _ = candidate(sync, text, 2)
    accepted = commit(
        sync,
        job,
        text,
        fact(
            "不确定",
            predicate="object.name",
            subject=subject,
            operation="replace",
            targets=[ref(sync, first)],
        ),
    )
    assert accepted["state"] == "completed"
    successor = accepted["result"]["group_ids"][0]
    new_fact = json.loads(target_state(sync, successor)[0]["payload"])
    assert new_fact["canonical"]["subject"]["id"] == subject["id"]
    assert new_fact["value"] == "不确定"
    assert target_state(sync, first)[0]["superseded_by"] == successor


@pytest.mark.parametrize("object_name", [False, True])
def test_a_current_literal_name_cannot_be_used_as_a_withdrawal(sync, object_name):
    setup(sync)
    text = "第一支钢笔叫晨光。" if object_name else "我叫晨光。"
    value = fact("晨光", predicate="name")
    if object_name:
        value = fact(
            "晨光",
            predicate="object.name",
            subject=dict(kind="entity", id=None, type="pen", mention="第一支钢笔"),
        )
    job, _ = candidate(sync, text)
    first = commit(sync, job, text, value)["result"]["group_ids"][0]
    stored = json.loads(target_state(sync, first)[0]["payload"])
    value = dict(
        value,
        subject=stored["canonical"]["subject"],
        operation="retract",
        target_refs=[ref(sync, first)],
    )
    before = target_state(sync, first)
    job, _ = candidate(sync, text, 2)
    assert commit(sync, job, text, value)["state"] == "rejected"
    assert target_state(sync, first) == before
    assert_pending_without_receipt(sync, job)
    if object_name:
        text = "第一支钢笔以前叫晨光。"
        job, _ = candidate(sync, text, 3)
        accepted = commit(sync, job, text, value)
        assert accepted["state"] == "completed", accepted
        assert target_state(sync, first)[0]["superseded_by"] == accepted["result"]["group_ids"][0]


def test_an_independently_stale_target_still_has_the_existing_unknown_409_semantics(sync):
    group = known_email(sync)
    stale = ref(sync, group)
    text = "我的邮箱现在已经改动。"
    job, _ = candidate(sync, text, 2)
    result = commit(
        sync,
        job,
        text,
        fact("已改动", predicate="email.status", operation="replace", targets=[stale]),
    )
    assert result["state"] == "completed"
    before = target_state(sync, group)
    text = "我改回原来的邮箱了。"
    job, _ = candidate(sync, text, 3)
    response = sync.post(
        PATH,
        dict(
            job_id=job,
            extraction_schema=4,
            output=dict(
                memories=[
                    dict(
                        fact=fact(
                            "未改动", predicate="email.status", operation="replace", targets=[stale]
                        ),
                        conditions=[],
                        negations=[],
                        valid_time="当前",
                        evidence=[dict(source_index=0, quote=text)],
                    )
                ]
            ),
        ),
    )
    assert response.status_code == 409
    assert response.json() == {"code": "version_conflict", "execution_state": "unknown"}
    assert target_state(sync, group) == before
    assert_pending_without_receipt(sync, job)
