"""Source wording survives typed preparation, target updates and persistence."""

import json

import pytest
from test_candidate_extraction import PATH
from test_fact_provenance import forget_request, ref
from test_group_context import owner
from test_memory_v2 import candidate, commit, fact, post, query, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.recall_language import author_name_query, query_aliases, self_reference


def propose(h, job, text, *values, time="当前", negations=(), quote=None):
    response = h.post(
        PATH,
        dict(
            job_id=job,
            extraction_schema=4,
            output=dict(
                memories=[
                    dict(
                        fact=value,
                        conditions=[],
                        negations=list(negations),
                        valid_time=time,
                        evidence=[dict(source_index=0, quote=quote or text)],
                    )
                    for value in values
                ]
            ),
        ),
    )
    assert response.status_code == 200, response.text
    return response.json()


def persisted(h, group_id):
    with h.store.transaction() as db:
        row = db.execute(
            "SELECT f.payload AS fact,r.payload AS unit FROM automatic_facts f "
            "JOIN records r ON r.group_id=f.group_id WHERE f.group_id=?",
            (group_id,),
        ).fetchone()
    return json.loads(row["fact"]), json.loads(row["unit"])


@pytest.mark.parametrize("intro", ["我是", "我就是", "我也叫"])
def test_natural_author_introduction_commits_the_whole_mixed_proposal(sync, intro):
    setup(sync)
    text = f"{intro}合成岚，我喜欢大麦茶。"
    job, _ = candidate(sync, text)
    result = commit(sync, job, text, fact("合成岚", predicate="name"), fact("大麦茶"))
    assert result["state"] == "completed", result
    assert len(result["result"]["group_ids"]) == 2
    for question in ("我叫什么", "我的名字是什么", "你还记得我的姓名吗"):
        assert any(
            item.get("fact", {}).get("value") == "合成岚"
            for item in query(sync, question)["evidence"]
        ), question


@pytest.mark.parametrize("question", ["他叫什么", "我家猫叫什么", "‘我叫什么’", "他说：‘我叫什么’"])
def test_someone_elses_or_quoted_name_question_does_not_become_the_authors_name(question):
    assert not author_name_query(question)
    assert not set(query_aliases(question)) & {"姓名", "name"}
    if "猫" not in question:
        assert not self_reference(question)


def test_requesters_name_question_cannot_select_another_owner_or_a_pet(sync):
    setup(sync)
    other = owner(sync, audience=sync.scope()["audience"])
    text = "我是合成松。"
    job, _ = candidate(sync, text, 1, actor=other)
    assert commit(sync, job, text, fact("合成松", predicate="name"))["state"] == "completed"
    text = "我是合成岚，我的猫叫云豆。"
    job, _ = candidate(sync, text, 2)
    pet = dict(kind="entity", id=None, type="cat", mention="我的猫")
    assert (
        commit(
            sync,
            job,
            text,
            fact("合成岚", predicate="name"),
            fact("云豆", predicate="pet.name", subject=pet),
        )["state"]
        == "completed"
    )
    selected = query(sync, "我叫什么")
    facts = [item["fact"] for item in selected["evidence"] if item.get("fact")]
    assert [value["value"] for value in facts] == ["合成岚"]
    resolution = selected["bundle"]["selection_constraints"][0]["owner_resolution"]
    assert resolution["mode"] == "self"
    assert resolution["targets"][0]["person_id"] == sync.scope()["person_id"]


@pytest.mark.parametrize("name", ["安生", "陈生", "金生"])
def test_natural_personal_name_is_not_a_role_just_because_of_its_last_character(sync, name):
    setup(sync)
    text = f"我是{name}。"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact(name, predicate="name"))["state"] == "completed"


@pytest.mark.parametrize(
    "text,value",
    [
        ("我是医生。", "医生"),
        ("我就是管理员。", "管理员"),
        ("我是护士。", "护士"),
        ("我是志愿者。", "志愿者"),
        ("如果下雨，我就是合成岚。", "合成岚"),
        ("朋友说，我是合成岚。", "合成岚"),
        ("他说：‘我是合成岚’。", "合成岚"),
        ("我是合成岚吗？", "合成岚"),
        ("我也叫你拿水。", "你拿水"),
    ],
)
def test_natural_introduction_keeps_role_attribution_condition_and_question_boundaries(
    sync, text, value
):
    setup(sync)
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact(value, predicate="name"))["state"] == "rejected"


@pytest.mark.parametrize("text", ["我叫护士。", "我的昵称是志愿者。"])
def test_an_explicit_literal_name_is_not_rejected_for_also_being_a_role(sync, text):
    setup(sync)
    value = "护士" if "护士" in text else "志愿者"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact(value, predicate="name"))["state"] == "completed"


def test_two_known_pens_accept_sourced_classifier_variant_but_reject_wrong_id(sync):
    setup(sync)
    text = "两支钢笔，有金夹的那支叫松涛，没夹的那支叫岚影；松涛放工作包，岚影在抽屉。"
    job, _ = candidate(sync, text)
    first = dict(kind="entity", id=None, type="pen", mention="有金夹的那支")
    second = dict(kind="entity", id=None, type="pen", mention="没夹的那支")
    result = commit(
        sync,
        job,
        text,
        fact("松涛", predicate="object.name", subject=first),
        fact("岚影", predicate="object.name", subject=second),
        fact("工作包", predicate="object.location", subject=dict(first, mention="松涛")),
        fact("抽屉", predicate="object.location", subject=dict(second, mention="岚影")),
    )
    assert result["state"] == "completed", result
    text = "有金夹那支还来了，我放到了床头盒里。"
    job, pending = candidate(sync, text, 2)
    locations = {
        item["fact"]["value"]: item
        for item in pending["known_facts"]
        if item["fact"]["predicate"] == "object.location"
    }
    target, other = locations["工作包"], locations["抽屉"]
    subject = dict(target["fact"]["subject"], mention="有金夹那支")
    value = fact(
        "床头盒",
        predicate="object.location",
        subject=subject,
        operation="replace",
        targets=target["target_refs"],
    )
    wrong = dict(value, subject=dict(subject, id=other["fact"]["subject"]["id"]))
    assert commit(sync, job, text, wrong)["state"] == "rejected"
    result = commit(sync, job, text, value)
    assert result["state"] == "completed", result
    stored, _ = persisted(sync, result["result"]["group_ids"][0])
    assert stored["canonical"]["subject"]["id"] == target["fact"]["subject"]["id"]
    assert "床头盒" in json.dumps(query(sync, "有金夹那支现在放哪")["evidence"], ensure_ascii=False)


@pytest.mark.parametrize("operation", ["confirm", "retract"])
def test_control_only_entity_update_inherits_exact_subject_and_source_closure(sync, operation):
    setup(sync)
    source = "临时盒子的标签是合成松纹。"
    job, _ = candidate(sync, source)
    value = fact(
        "合成松纹",
        predicate="label",
        subject=dict(kind="entity", id=None, type="box", mention="临时盒子"),
    )
    original = commit(sync, job, source, value)
    group = original["result"]["group_ids"][0]
    episode = next(
        item for item in query(sync, "合成松纹")["evidence"] if item["kind"] == "episode"
    )
    text = "确认之前那条标签。" if operation == "confirm" else "之前那条标签作废，撤回它。"
    job, pending = candidate(sync, text, 2)
    known = next(item for item in pending["known_facts"] if item["fact"]["predicate"] == "label")
    value = dict(known["fact"], operation=operation, target_refs=[ref(sync, group)])
    result = commit(sync, job, text, value)
    assert result["state"] == "completed", result
    successor = result["result"]["group_ids"][0]
    with sync.store.transaction() as db:
        assert (
            len(
                db.execute(
                    "SELECT source_key FROM lineage WHERE group_id=?", (successor,)
                ).fetchall()
            )
            == 2
        )
    forgotten = post(sync, "forget", forget_request(sync, episode, 3))
    assert forgotten.status_code == 200 and forgotten.json()["state"] == "tombstoned"
    assert "合成松纹" not in json.dumps(query(sync, "标签")["evidence"], ensure_ascii=False)


def test_explicit_object_location_is_not_an_author_attribute(sync):
    setup(sync)
    text = "我的耳罩平时收在书柜右边抽屉。"
    job, _ = candidate(sync, text)
    result = commit(sync, job, text, fact("书柜右边抽屉", predicate="possession.storage_location"))
    assert result["state"] == "completed", result
    value, unit = persisted(sync, result["result"]["group_ids"][0])
    assert value["canonical"]["subject"]["kind"] == "entity"
    assert value["canonical"]["subject"]["mention"] == "我的耳罩"
    assert "耳罩" in unit["statement"]


def test_location_only_confirmation_cannot_make_the_destination_its_own_object(sync):
    setup(sync)
    text = "现在能确定的只有位置换到餐边柜，别的没有补充。"
    job, _ = candidate(sync, text)
    value = fact(
        "餐边柜",
        predicate="object.location",
        subject=dict(kind="entity", id=None, type="furniture", mention="餐边柜"),
    )
    assert commit(sync, job, text, value)["state"] == "rejected"


@pytest.mark.parametrize(
    "text,subject,predicate,value",
    [
        ("周二打算去松溪路取东西。", ("place", "松溪路"), "plan.visit", "去松溪路取东西"),
        ("我先把背包找好。", ("object", "背包"), "intention.prepare", "先把背包找好"),
    ],
)
def test_a_location_or_object_cannot_be_the_agent_of_the_authors_intention(
    sync, text, subject, predicate, value
):
    setup(sync)
    job, _ = candidate(sync, text)
    proposed = fact(
        value,
        predicate=predicate,
        modality="planned",
        subject=dict(kind="entity", id=None, type=subject[0], mention=subject[1]),
    )
    result = commit(sync, job, text, proposed)
    assert result["state"] == "completed", result
    stored, _ = persisted(sync, result["result"]["group_ids"][0])
    assert stored["canonical"]["subject"]["kind"] == "author"


@pytest.mark.parametrize("modality", ["asserted", "observed"])
@pytest.mark.parametrize(
    "text,expected",
    [
        ("风大了，我先把门关上，茶还放桌上。", "planned"),
        ("我打算把门关了再去拿包。", "planned"),
        ("我计划把门关上了再去拿包。", "planned"),
        ("我先把门关上了再去拿包。", "planned"),
        ("我已经把门关上了。", "observed"),
        ("我先把门关上了。", "observed"),
        ("我想起昨天已经关上门了。", "observed"),
        ("我先去取包，刚才已经把门关上了。", "observed"),
        ("我希望天晴，但我已经把门关上了。", "observed"),
        ("我希望关上门，但我已经关上门了。", "observed"),
        ("我希望把门关上，但我已经把门关上了。", "observed"),
    ],
)
def test_immediate_intention_and_reported_completion_remain_different(
    sync, text, expected, modality
):
    setup(sync)
    job, _ = candidate(sync, text)
    value = dict(
        fact("关上门", predicate="action.close_door", modality=modality), state="historical"
    )
    result = commit(sync, job, text, value)
    assert result["state"] == "completed", result
    value, unit = persisted(sync, result["result"]["group_ids"][0])
    expected = "planned" if expected == "planned" else modality
    assert value["canonical"]["modality"] == expected
    assert ("计划（不代表已执行）" in unit["statement"]) == (expected == "planned")
    assert ("来源报告已发生" in unit["statement"]) == (expected == "observed")
    if expected == "planned" and "我先" in text:
        assert value["canonical"]["state"] == "current"
    assert unit["subject_person_id"] == sync.scope()["person_id"]
    with sync.store.transaction() as db:
        document = db.execute(
            "SELECT text FROM memory_documents WHERE group_id=?",
            (result["result"]["group_ids"][0],),
        ).fetchone()
    assert json.loads(document["text"])["modality"] == expected


@pytest.mark.parametrize("modality", ["asserted", "observed"])
@pytest.mark.parametrize(
    "text",
    [
        "朋友说，‘我先把门关上’。",
        "如果起风，我先把门关上。",
        "我先把门关上吗？",
        "我先把门关上，朋友先把门关上。",
        "我先把门关上，朋友已经把门关上了。",
        "我先把门关上，‘我先把门关上’只是台词。",
        "我希望把门关上。",
        "朋友说，‘我希望把门关上’。",
        "但愿门关上。",
        "我想象自己把门关上。",
        "我希望关上门，但朋友已经关上门了。",
        "我希望关上门，但‘我已经关上门了’只是台词。",
        "我希望关上门，但如果起风，我已经关上门了。",
        "我希望关上门，但我明天会已经关上门了。",
        "我希望关上门，但我已经准备明天关上门了。",
        "我希望关上门，但我已经“准备”关上门了。",
        "我希望关上门，但我已经“假装”关上门了。",
        "我希望关上门，但我已经把灯关好了。",
        "我希望关上门，但我已经说了‘关上门’。",
    ],
)
def test_action_modality_cannot_promote_a_wish_quote_condition_or_different_subject(
    sync, text, modality
):
    setup(sync)
    job, _ = candidate(sync, text)
    value = fact("关上门", predicate="action.close_door", modality=modality)
    assert commit(sync, job, text, value)["state"] == "rejected"
    with sync.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM automatic_facts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text,predicate,value,subject",
    [
        ("我叫计划。", "identity.name", "计划", None),
        (
            "我的本子叫希望。",
            "object.name",
            "希望",
            dict(kind="entity", id=None, type="notebook", mention="我的本子"),
        ),
        ("我希望天气好一点。", "wish.preference", "天气好一点", None),
    ],
)
def test_non_action_names_and_the_wish_itself_keep_their_asserted_modality(
    sync, text, predicate, value, subject
):
    setup(sync)
    job, _ = candidate(sync, text)
    result = commit(sync, job, text, fact(value, predicate=predicate, subject=subject))
    assert result["state"] == "completed", result
    stored, unit = persisted(sync, result["result"]["group_ids"][0])
    assert stored["canonical"]["modality"] == "asserted"
    assert "计划（不代表已执行）" not in unit["statement"]


def test_refunded_ticket_is_not_a_completed_attendance_date(sync):
    setup(sync)
    text = "展览票改到周一了，周日那张已经退了。"
    job, _ = candidate(sync, text)
    value = dict(
        fact("周日", predicate="event.attendance_date", modality="observed"), state="historical"
    )
    assert propose(sync, job, text, value, quote="周日")["state"] == "rejected"
    assert (
        commit(sync, job, text, fact("周日票已退", predicate="ticket.refund", modality="observed"))[
            "state"
        ]
        == "completed"
    )


def test_ticket_refund_does_not_deny_separately_reported_actual_attendance(sync):
    setup(sync)
    text = "原计划周日的票退掉了，但我周日还是实际去了。"
    job, _ = candidate(sync, text)
    value = dict(
        fact("周日", predicate="event.attendance_date", modality="observed"), state="historical"
    )
    result = propose(sync, job, text, value, quote="但我周日还是实际去了")
    assert result["state"] == "completed", result
    stored, _ = persisted(sync, result["result"]["group_ids"][0])
    assert stored["canonical"]["modality"] == "observed"


@pytest.mark.parametrize(
    "later",
    [
        "但我周日准备到场。",
        "但我周日会到场。",
        "但我周日不一定到场。",
        "但如果有空，我周日会到场。",
        "但我周日没有到场。",
        "但我周日会实际到场。",
        "但‘我周日实际去了’只是举例。",
        "但我周日实际去了没有？",
    ],
)
def test_ticket_refund_exception_requires_a_safe_actual_report_not_a_future_or_quoted_action(
    sync, later
):
    setup(sync)
    text = "原计划周日的票退掉了，" + later
    job, _ = candidate(sync, text)
    value = dict(
        fact("周日", predicate="event.attendance_date", modality="observed"), state="historical"
    )
    assert propose(sync, job, text, value, quote="周日")["state"] == "rejected"


@pytest.mark.parametrize(
    "text",
    [
        "我打算去松溪，朋友打算去松溪。",
        "如果下雨，我打算去松溪。",
        "朋友说，‘我打算去松溪’。",
        "我打算去松溪吗？",
    ],
)
def test_actor_repair_cannot_discard_another_subject_condition_quote_or_question(sync, text):
    setup(sync)
    job, _ = candidate(sync, text)
    value = fact(
        "去松溪",
        predicate="plan.visit",
        modality="planned",
        subject=dict(kind="entity", id=None, type="place", mention="松溪"),
    )
    assert commit(sync, job, text, value)["state"] == "rejected"


def test_unsupported_unit_date_does_not_survive_null_canonical_interval(sync):
    setup(sync)
    text = "我的笔记本以后就叫山岚。"
    job, _ = candidate(sync, text)
    value = fact(
        "山岚",
        predicate="object.name",
        subject=dict(kind="entity", id=None, type="notebook", mention="我的笔记本"),
    )
    result = propose(sync, job, text, value, time="从2026-10-11起，长期有效（未说明有效时间）")
    assert result["state"] == "completed", result
    value, unit = persisted(sync, result["result"]["group_ids"][0])
    assert value["canonical"]["valid_from"] is None
    assert unit["valid_time"] == "未说明有效时间"


def test_validated_canonical_interval_owns_the_unit_interval(sync):
    setup(sync)
    text = "2026年10月1日到2026年11月1日我在晚班。"
    job, _ = candidate(sync, text)
    value = dict(
        fact("晚班", predicate="work.shift"),
        valid_from="2026-10-01T00:00:00Z",
        valid_to="2026-11-01T00:00:00Z",
    )
    result = propose(sync, job, text, value, time="从2026-12-01开始")
    assert result["state"] == "completed", result
    _, unit = persisted(sync, result["result"]["group_ids"][0])
    assert "2026-10-01T00:00:00Z" in unit["valid_time"]
    assert "2026-11-01T00:00:00Z" in unit["valid_time"]
    assert "2026-12-01" not in unit["valid_time"]
