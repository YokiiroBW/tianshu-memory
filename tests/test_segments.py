"""Bounded multi-person jobs through real Memory HTTP, source barriers and SQL.

Core/Platform owner responses are synthetic. There is no model or live account.
The existing sync fixture also allows the product SQL leaves to be delegated to PG.
"""

import copy
import json
from datetime import timedelta

import pytest
from summary_fixtures import prepare as prepare_summary
from test_group_context import owner
from test_memory_raw_sources import original_turn
from test_memory_v2 import post, request
from test_observations import Verifier
from test_observations import event as observation_event
from test_observations import source as observation_source
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import Fault, canonical, fingerprint, parse_time
from tianshu_memory.observations import ObservationLedger
from tianshu_memory.segment_sources import frame

MATERIALS = "memory/candidates/materials"
SEAL = "memory/candidates/segments"
PROCESS = "memory/candidates/process"


def ready(h):
    contents = getattr(h, "test_contents", {})
    replies = getattr(h, "summary_replies", {})
    prepare_summary(h)
    # Installing another Memory capability does not delete the external owners'
    # originals or acknowledged replies used by already frozen legacy windows.
    h.test_contents.update(contents)
    h.summary_replies.update(replies)
    with h.store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
    if "observation_schema" not in metadata:
        h.store.migrate_observations(h.directory / "before-segment-observations.sqlite")
    if "qq_alias_schema" not in metadata:
        h.store.migrate_qq_aliases(h.directory / "before-segment-aliases.sqlite")
    h.store.migrate_segments(h.directory / "before-segments.sqlite")
    legacy = h.window_checker

    def check(body):
        if "manifest" in body:
            return legacy(body)
        return dict(
            schema_version=1,
            valid=True,
            channel=body["channel"],
            delivery_channel=body["delivery_channel"],
            frame=dict(frame(h.scope()), actor_id=body["actor_id"]),
        )

    h.window_checker = check
    original_response = h.response

    def response(kind, body):
        if kind == "window_check" and "manifest" not in body:
            return check(body)
        return original_response(kind, body)

    h.response = response


def front(h, text, serial, actor=0, *, accept=True, reality="real"):
    turn = original_turn(h, serial, actor=actor)
    event = turn["committed_event"]
    for source in event["sources"]:
        key = {key: value for key, value in source["message_key"].items() if key != "revision"}
        physical = next(item for item in h.physicals if item["key"] == key)
        content = dict(
            message_key=source["message_key"],
            author=physical["author"],
            sent_at="2026-10-24T00:00:00Z",
            kind="message",
            parts=[dict(kind="text", text=text)],
            reply_refs=[],
            mentioned_accounts=[],
        )
        physical["content_digest"] = fingerprint(content)
        physical["classification"]["value"] = reality
        h.test_contents[canonical(key)] = content
    event["reality"] = reality
    with h.store.transaction() as db:
        event["scope_version"] = h.service._scope_version(db, event["scope"])
    if "scope_version" in turn:
        turn["scope_version"] = event["scope_version"]
    if not accept:
        return event
    response = h.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-extraction#segment_material_response", response.json())
    return response.json()["materials"][0], event


def seal(h, materials, serial="segment:one", context=()):
    body = dict(
        schema_version=1,
        segment_id=serial,
        frame=materials[0]["frame"],
        material_refs=[item["material_ref"] for item in materials],
        context_refs=[item["material_ref"] for item in context],
        context_windows=[],
        omitted_context=False,
    )
    response = h.post(SEAL, body)
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-extraction#segment_seal_response", response.json())
    return response.json(), body


def pending(h, sealed):
    body = dict(extraction_schema=6, job_id=sealed["job_id"], batch_ref=None, output=None)
    response = h.post(PROCESS, body)
    assert response.status_code == 200, response.text
    schema = (
        "segment_pending_response" if response.json()["state"] == "pending" else "segment_receipt"
    )
    h.contracts.validate("memory-extraction#" + schema, response.json())
    return response.json()


def output(prepared, *, empty=(), portrait=None):
    results = []
    for target in prepared["segment_window"]["targets"]:
        person = target["scope"]["person_id"]
        memories = (
            []
            if person in empty
            else [
                dict(
                    text="本人讨论了河边散步。",
                    kind="experience",
                    state="described",
                    valid_until=None,
                    source_member_ids=target["new_member_ids"],
                    prior_refs=[],
                    operation="append",
                    importance="normal",
                    retention="medium",
                )
            ]
        )
        results.append(
            dict(
                subject_person_id=person,
                covered_member_ids=target["new_member_ids"],
                memories=memories,
                portrait=portrait,
            )
        )
    return dict(extraction_schema=6, topics=[], results=results)


def submit(h, prepared, value):
    body = dict(
        extraction_schema=6,
        job_id=prepared["job_id"],
        batch_ref=prepared["batch_ref"],
        output=value,
    )
    return h.post(PROCESS, body), body


class PassiveOwner(Verifier):
    def __init__(self, h):
        super().__init__()
        self.h, self.revision, self.actor = h, 1, h.scope()["actor_id"]
        self.calls = []
        self.resolve = {}
        self.channel = dict(
            h.event()["sources"][0]["message_key"]["channel"],
            binding_id="binding:observation:stable",
        )

    def __call__(self, body):
        if body["operation"] == "segment_resolve":
            return dict(
                valid=True,
                actor_id=body["actor_id"],
                sources=[
                    dict(item, observation=self.resolve.get(fingerprint(item["source"])))
                    for item in body["sources"]
                ],
            )
        if body["operation"] != "segment":
            return super().__call__(body)
        self.calls.append(copy.deepcopy(body))
        if not self.read_allowed or body["actor_id"] not in (None, self.actor):
            raise Fault("forbidden", 403)
        sources = []
        for selector in body["sources"]:
            value = self.values.get(selector["source_ref"])
            if value is None or selector["archive_epoch"] != self.archive_epoch:
                raise Fault("scope_changed", 409)
            event = value["event"]
            sources.append(
                dict(
                    selector,
                    physical_key={
                        key: event[key]
                        for key in (
                            "platform_id",
                            "self_id",
                            "conversation_id",
                            "account_id",
                            "event_id",
                            "revision",
                        )
                    },
                )
            )
        return dict(
            valid=True,
            actor_id=self.actor,
            instance_id=body["instance_id"],
            self_id=body["self_id"],
            conversation_id=body["conversation_id"],
            channel=self.channel,
            delivery_channel=self.h.event()["sources"][0]["message_key"]["channel"],
            authority=dict(
                owner_id="synthetic-observation-catalog",
                epoch=self.archive_epoch,
                revision=self.revision,
                digest=fingerprint([sources, self.revision, self.actor]),
            ),
            sources=sources,
        )


def archive(
    h,
    *,
    text="旁听的人说准备周末去散步。",
    native="passive:one",
    author="60001",
    accept_material=True,
    sent_at="2026-10-24T00:00:00Z",
    replies=("quoted:previous",),
):
    if not hasattr(h, "passive_owner"):
        h.passive_owner = PassiveOwner(h)
        h.service.observations = ObservationLedger(
            h.store, h.passive_owner, local_commit=lambda: h.service.local_commit()
        )
    physical_conversation = (
        "group:20002" if h.scope()["audience"] == "group" else "private:" + author
    )
    event = observation_event(conversation=physical_conversation, author=author, native=native)
    event.update(
        schema_version=4,
        text=text,
        nickname="旁听作者",
        group_card=None,
        sent_at=sent_at,
        message_context=dict(reply_ids=list(replies), mentioned_accounts=["60002"]),
    )
    value = observation_source(event)
    h.passive_owner.values[value["source_ref"]] = value
    h.service.observations.ingest("companion", value)
    with h.store.transaction() as db:
        account = dict(namespace="qq", immutable_account_id=author)
        person = h.service._binding(db, account)["person_id"]
    scope = dict(h.scope(), person_id=person)
    h.config["callers"]["companion"]["event_scopes"].append(scope)
    h.save()
    if not accept_material:
        return None, value
    response = h.post(MATERIALS, dict(schema_version=1, observation_refs=[value["source_ref"]]))
    assert response.status_code == 200, response.text
    return response.json()["materials"][0], value


def test_front_materials_create_no_job_and_seal_is_atomic_idempotent(sync):
    ready(sync)
    material, event = front(sync, "准备去河边散步。", "first")
    duplicate = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert duplicate.status_code == 200 and duplicate.json()["materials"] == [material]
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    sealed, body = seal(sync, [material])
    assert sync.post(SEAL, body).json() == sealed
    prepared = pending(sync, sealed)
    public = prepared["segment_window"]
    assert public["window_id"] == fingerprint(
        {key: value for key, value in public.items() if key != "window_id"}
    )
    assert public["targets"][0]["new_member_ids"] == [material["material_ref"]]
    done, request_body = submit(sync, prepared, output(prepared, portrait="最近会讨论散步。"))
    assert done.status_code == 200, done.text
    assert done.json()["results"][0]["portrait_state"] == "committed"
    assert sync.post(PROCESS, request_body).json() == done.json()
    assert pending(sync, sealed) == done.json()
    request_body["output"]["results"][0]["memories"][0]["text"] += "不同写入"
    assert sync.post(PROCESS, request_body).status_code == 409
    body["segment_id"] = "another-segment"
    assert sync.post(SEAL, body).status_code == 409
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1


@pytest.mark.parametrize(
    "bad", ["missing_person", "duplicate_person", "foreign_member", "foreign_ref", "coverage"]
)
def test_multi_owner_proposal_rejects_whole_then_commits_all_with_explicit_empty(sync, bad):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two independent authors in one actual group")
    ready(sync)
    other = owner(sync)
    first, _ = front(sync, "我准备周末去河边。", "a")
    second, _ = front(sync, "我最近在练钢琴。", "b", other)
    sealed, _ = seal(sync, [first, second])
    prepared = pending(sync, sealed)
    value = output(prepared)
    if bad == "missing_person":
        value["results"].pop()
    elif bad == "duplicate_person":
        value["results"][1] = copy.deepcopy(value["results"][0])
    elif bad == "foreign_member":
        value["results"][0]["memories"][0]["source_member_ids"] = [second["material_ref"]]
    elif bad == "foreign_ref":
        value["results"][0]["memories"][0]["prior_refs"] = [
            dict(record_id="not-provided", record_version=1)
        ]
    else:
        value["results"][0]["covered_member_ids"] = []
    response, _ = submit(sync, prepared, value)
    assert response.status_code == 400, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    done, _ = submit(sync, prepared, output(prepared, empty=[second["person_id"]]))
    assert done.status_code == 200, done.text
    results = {item["subject_person_id"]: item for item in done.json()["results"]}
    assert results[first["person_id"]]["memory_refs"]
    assert results[second["person_id"]]["memory_refs"] == []
    assert results[second["person_id"]]["covered_member_ids"] == [second["material_ref"]]


def test_passive_original_has_real_proof_no_core_admission_and_withdrawal(sync):
    ready(sync)
    material, original = archive(sync)
    sealed, _ = seal(sync, [material])
    prepared = pending(sync, sealed)
    member = prepared["segment_window"]["members"][0]
    assert member["kind"] == "observation" and member["reply_refs"] == ["quoted:previous"]
    assert member["mentioned_accounts"] == [dict(namespace="qq", immutable_account_id="60002")]
    assert prepared["segment_window"]["targets"][0]["portrait_input"]["current_text"] == ""
    done, _ = submit(sync, prepared, output(prepared))
    assert done.status_code == 200, done.text
    with sync.service.operation(scope=prepared["segment_window"]["targets"][0]["scope"]) as db:
        assert (
            db.execute(
                "SELECT 1 FROM source_admissions WHERE key=?", (material["source_key"],)
            ).fetchone()
            is None
        )
        record = done.json()["results"][0]["memory_refs"][0]
        group = db.execute(
            "SELECT group_id,payload FROM records WHERE id=?", (record["record_id"],)
        ).fetchone()
        assert sync.service._group_current(db, group["group_id"])
        assert json.loads(group["payload"])["subject_person_id"] == material["person_id"]
    if sync.scope()["audience"] == "group":
        response = post(sync, "query", dict(request(sync, "河边散步"), retrieval_mode="context"))
        assert response.status_code == 200, response.text
        summaries = [item for item in response.json()["evidence"] if item["kind"] == "summary"]
        assert any(item["subject_person_id"] == material["person_id"] for item in summaries)
        assert all(
            item["owner_scope"]["person_id"] == item["subject_person_id"] for item in summaries
        )
    # Every new checkpoint rechecks the actual immutable archive sources.
    previous = len(sync.passive_owner.calls)
    sync.passive_owner.archive_epoch = 2
    sync.passive_owner.revision += 1
    with sync.service.operation(scope=prepared["segment_window"]["targets"][0]["scope"]) as db:
        assert not sync.service._group_current(db, group["group_id"])
        assert (
            db.execute(
                "SELECT state FROM observation_sources WHERE source_ref=?",
                (original["source_ref"],),
            ).fetchone()[0]
            == "active"
        )
    assert len(sync.passive_owner.calls) > previous


def test_multi_owner_sql_failure_rolls_back_whole_receipt_then_same_package_recovers(
    sync, monkeypatch
):
    if sync.scope()["audience"] != "group":
        pytest.skip("A multi-author group transaction")
    ready(sync)
    other = owner(sync)
    one, _ = front(sync, "我准备去散步。", "tx1")
    two, _ = front(sync, "我准备练琴。", "tx2", other)
    prepared = pending(sync, seal(sync, [one, two])[0])
    from tianshu_memory import segment_records

    original = segment_records.write
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Fault("dependency_unavailable", 503)
        return original(*args, **kwargs)

    monkeypatch.setattr(segment_records, "write", fail_second)
    failed, body = submit(sync, prepared, output(prepared))
    assert failed.status_code == 503
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    monkeypatch.setattr(segment_records, "write", original)
    recovered = sync.post(PROCESS, body)
    assert recovered.status_code == 200, recovered.text
    assert len(recovered.json()["results"]) == 2
    assert all(item["memory_refs"] for item in recovered.json()["results"])


@pytest.mark.parametrize("scope_error", ["foreign_frame", "duplicate_new", "reuse_context_as_new"])
def test_seal_scope_and_coverage_reservations_are_not_caller_claims(sync, scope_error):
    ready(sync)
    one, _ = front(sync, "我准备去散步。", "seal1")
    body = dict(
        schema_version=1,
        segment_id="bad-segment",
        frame=one["frame"],
        material_refs=[one["material_ref"]],
        context_refs=[],
        context_windows=[],
        omitted_context=False,
    )
    if scope_error == "foreign_frame":
        body["frame"] = dict(body["frame"], conversation_id="unrelated-room")
    elif scope_error == "duplicate_new":
        body["material_refs"].append(one["material_ref"])
    else:
        body["context_refs"].append(one["material_ref"])
    response = sync.post(SEAL, body)
    assert response.status_code in {400, 403}, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_segment_claims").fetchone()[0] == 0


def test_new_state_replaces_exact_prior_and_retention_is_independent(sync):
    ready(sync)
    first, _ = front(sync, "我考虑下个月去散步。", "state1")
    sealed, _ = seal(sync, [first])
    prepared = pending(sync, sealed)
    value = output(prepared)
    value["results"][0]["memories"][0].update(
        kind="agreement",
        state="considering",
        valid_until="2027-01-01T00:00:00Z",
        retention="short",
        importance="high",
    )
    done, _ = submit(sync, prepared, value)
    assert done.status_code == 200, done.text
    old = done.json()["results"][0]["memory_refs"][0]
    second, _ = front(sync, "这个安排取消了。", "state2")
    prepared2 = pending(sync, seal(sync, [second], "segment:second")[0])
    assert old in [
        {key: item[key] for key in ("record_id", "record_version")}
        for item in prepared2["segment_window"]["targets"][0]["known_memories"]
    ]
    value2 = output(prepared2)
    value2["results"][0]["memories"][0].update(
        kind="agreement", state="cancelled", operation="replace", prior_refs=[old]
    )
    done2, _ = submit(sync, prepared2, value2)
    assert done2.status_code == 200, done2.text
    with sync.store.transaction() as db:
        previous = db.execute(
            "SELECT g.state,s.expires_at,i.effective_at FROM groups g JOIN records r ON r.group_id=g.id JOIN memory_summaries s ON s.group_id=g.id JOIN memory_segment_items i ON i.group_id=g.id WHERE r.id=?",
            (old["record_id"],),
        ).fetchone()
        assert previous["state"] == "superseded"
        # A pending agreement now receives long/high from the server rule,
        # irrespective of the model's short suggestion and before its valid-until.
        assert parse_time(previous["expires_at"]) > parse_time("2027-01-01T00:00:00Z")
        assert (
            db.execute(
                "SELECT state FROM history WHERE record_id=? AND version=1", (old["record_id"],)
            ).fetchone()[0]
            == "active"
        )
        state = db.execute(
            "SELECT i.state FROM memory_segment_items i JOIN records r ON r.group_id=i.group_id WHERE r.id=?",
            (done2.json()["results"][0]["memory_refs"][0]["record_id"],),
        ).fetchone()[0]
        assert state == "cancelled"


def test_optional_portrait_pointer_conflict_keeps_experiences(sync):
    ready(sync)
    one, _ = front(sync, "第一段去散步。", "portrait1")
    two, _ = front(sync, "第二段去散步。", "portrait2")
    first = pending(sync, seal(sync, [one], "segment:p1")[0])
    second = pending(sync, seal(sync, [two], "segment:p2")[0])
    assert submit(sync, second, output(second, portrait="最近愿意散步。"))[0].status_code == 200
    response, _ = submit(sync, first, output(first, portrait="另一个画像。"))
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["memory_refs"]
    assert response.json()["results"][0]["portrait_state"] == "conflict"


def test_whole_source_checkpoint_failure_and_publisher_denial_write_nothing(sync):
    ready(sync)
    material, _ = front(sync, "河边散步。", "failure")
    sealed, _ = seal(sync, [material])
    prepared = pending(sync, sealed)
    sync.status["current"] = 503
    response, body = submit(sync, prepared, output(prepared))
    assert response.status_code == 503
    sync.status.clear()
    sync.config["callers"]["companion"]["event_scopes"] = []
    sync.save()
    assert sync.post(PROCESS, body).status_code == 403
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("order", ["passive_first", "frontend_first"])
def test_genuine_resolve_unifies_distinct_core_and_native_ids_without_duplicate_coverage(
    sync, order
):
    if sync.scope()["audience"] != "group":
        pytest.skip("A physical observed group conversation")
    ready(sync)
    material, original = archive(
        sync,
        text="周末去河边散步。",
        native="native:real-event",
        accept_material=order == "passive_first",
    )
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "周末去河边散步。", "core-uuid-differs", actor, accept=False)
    assert event["sources"][0]["message_key"]["message_id"] != original["event"]["event_id"]
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        source_ref=original["source_ref"],
        source_digest=original["source_digest"],
        scope_version=original["scope_version"],
        archive_epoch=1,
    )
    if order == "passive_first":
        prepared = pending(sync, seal(sync, [material])[0])
        assert submit(sync, prepared, output(prepared))[0].status_code == 200
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    if material is None:
        material = response.json()["materials"][0]
    assert response.json()["materials"] == [material]
    passive = sync.post(
        MATERIALS, dict(schema_version=1, observation_refs=[original["source_ref"]])
    )
    assert passive.status_code == 200 and passive.json()["materials"] == [material]
    if order == "frontend_first":
        prepared = pending(sync, seal(sync, response.json()["materials"])[0])
        assert submit(sync, prepared, output(prepared))[0].status_code == 200
    duplicate = dict(
        schema_version=1,
        segment_id="segment:duplicate",
        frame=material["frame"],
        material_refs=[material["material_ref"]],
        context_refs=[],
        context_windows=[],
        omitted_context=False,
    )
    assert sync.post(SEAL, duplicate).status_code == 409
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 1
        assert (
            db.execute("SELECT material_id FROM memory_material_aliases").fetchone()[0]
            == material["material_ref"]
        )


def test_authoritative_fictional_conflict_does_not_relabel_obs_as_real_or_rewrite_receipt(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("An observed group with a real Core classification")
    ready(sync)
    material, original = archive(sync, text="剧本里我会飞。")
    sealed, _ = seal(sync, [material])
    prepared = pending(sync, sealed)
    done, body = submit(sync, prepared, output(prepared))
    assert done.status_code == 200, done.text
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "剧本里我会飞。", "fictional", actor, accept=False, reality="fictional")
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        source_ref=original["source_ref"],
        source_digest=original["source_digest"],
        scope_version=original["scope_version"],
        archive_epoch=1,
    )
    failed = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert failed.status_code == 409, failed.text
    assert sync.post(PROCESS, body).json() == done.json()
    with sync.store.transaction() as db:
        assert db.execute(
            "SELECT 1 FROM suppression WHERE source_key=?", (material["source_key"],)
        ).fetchone()
        assert (
            db.execute(
                "SELECT state FROM observation_sources WHERE source_ref=?",
                (original["source_ref"],),
            ).fetchone()[0]
            == "active"
        )
        group = db.execute(
            "SELECT group_id FROM records WHERE id=?",
            (done.json()["results"][0]["memory_refs"][0]["record_id"],),
        ).fetchone()[0]
        assert not sync.service._group_current(db, group)


def forget_original(h, material, actor):
    from test_memory_context import ProofIssuer

    with h.store.transaction() as db:
        episode = db.execute(
            "SELECT id,source_revision FROM memory_documents WHERE kind='episode' AND source_key=?",
            (material["source_key"],),
        ).fetchone()
    body = dict(
        command=h.command(actor),
        scope=h.scope(actor),
        batch_ref=None,
        item_id="forget:physical-alias",
        kind="forget_episode",
        target=dict(episode_ref=episode["id"], episode_version=episode["source_revision"]),
        evidence_refs=h.event(actor)["sources"],
        proof_ref="test:proof",
    )
    proofs = ProofIssuer()
    h.service.memory_context.proofs = proofs
    with h.store.transaction() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM memory_materials WHERE id=?", (material["material_ref"],)
            ).fetchone()[0]
        )
    proofs.issue(body, "revision", [payload["member"]["speaker"]["account"]], [h.scope(actor)])
    response = post(h, "forget", body)
    assert response.status_code == 200 and response.json()["state"] == "tombstoned", response.text


@pytest.mark.parametrize("direction", ["core", "observation"])
def test_exact_alias_forget_closes_both_originals_and_pending_job_without_revival(sync, direction):
    if sync.scope()["audience"] != "group":
        pytest.skip("A genuine observation/Core bridge")
    ready(sync)
    material, original = archive(sync, text="不再保留这次散步。")
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "不再保留这次散步。", "deletion-core", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        source_ref=original["source_ref"],
        source_digest=original["source_digest"],
        scope_version=original["scope_version"],
        archive_epoch=1,
    )
    accepted = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert accepted.status_code == 200, accepted.text
    prepared = pending(sync, seal(sync, [material])[0])
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])
    if direction == "observation":
        forget_original(sync, material, actor)
    else:
        # The Core copy is a genuine independently accepted episode. The user
        # selects it, not the observation alias that happens to share a body.
        core_material = dict(material, source_key=core_key)
        forget_original(sync, core_material, actor)
    response, _ = submit(sync, prepared, output(prepared))
    assert response.status_code == 200 and response.json()["state"] == "invalidated", response.text
    assert (
        sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[])).status_code
        == 409
    )
    assert (
        sync.post(
            MATERIALS, dict(schema_version=1, observation_refs=[original["source_ref"]])
        ).status_code
        == 409
    )
    with sync.store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT source_key FROM suppression")} >= {
            core_key,
            material["source_key"],
        }
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT state FROM observation_sources WHERE source_ref=?",
                (original["source_ref"],),
            ).fetchone()[0]
            == "active"
        )


def test_passive_reply_context_is_selected_before_freeze_and_late_forget_invalidates_it(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("Two authors replying in one group")
    ready(sync)
    question, _ = archive(sync, text="明天几点见？", native="question", replies=())
    first = pending(sync, seal(sync, [question], "segment:question")[0])
    assert submit(sync, first, output(first))[0].status_code == 200
    answer, _ = archive(sync, text="九点。", native="answer", author="60002", replies=["question"])
    prepared = pending(sync, seal(sync, [answer], "segment:answer")[0])
    members = {item["member_id"]: item for item in prepared["segment_window"]["members"]}
    assert set(members) == {question["material_ref"], answer["material_ref"]}
    assert members[question["material_ref"]]["text"] == "明天几点见？"
    assert prepared["segment_window"]["targets"][0]["new_member_ids"] == [answer["material_ref"]]
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    forget_original(sync, question, actor)
    response, _ = submit(sync, prepared, output(prepared))
    assert response.status_code == 200 and response.json()["state"] == "invalidated", response.text
    later, _ = archive(
        sync, text="还在九点见。", native="later", author="60002", replies=["question"]
    )
    fresh = pending(sync, seal(sync, [later], "segment:later")[0])
    assert question["material_ref"] not in {
        item["member_id"] for item in fresh["segment_window"]["members"]
    }
    assert fresh["segment_window"]["coverage"]["omitted_context"] is True
    assert submit(sync, fresh, output(fresh))[0].status_code == 200


def test_physical_message_never_reuses_another_actor_material_or_coverage(sync):
    ready(sync)
    first, original = archive(sync)
    sync.passive_owner.actor = "actor:b"
    other_scope = dict(first["frame"], person_id=first["person_id"], actor_id="actor:b")
    sync.config["callers"]["companion"]["event_scopes"].append(other_scope)
    sync.save()
    response = sync.post(
        MATERIALS, dict(schema_version=1, observation_refs=[original["source_ref"]])
    )
    assert response.status_code == 200, response.text
    second = response.json()["materials"][0]
    assert second["frame"]["actor_id"] == "actor:b"
    assert (
        second["material_ref"] != first["material_ref"]
        and second["source_key"] != first["source_key"]
    )
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM memory_segment_claims").fetchone()[0] == 0


def test_schema6_needs_explicit_migration_and_consume_stops_creating_new_singletons(sync):
    prepare_summary(sync)
    event = front(sync, "新段不要退回单句。", "no-migration", accept=False)
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 503, response.text
    ready(sync)
    accepted = sync.post("memory/turn-commits", event)
    assert accepted.status_code == 200 and accepted.json()["candidate_job_ref"] is None, (
        accepted.text
    )
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_forgotten_optional_core_context_does_not_poison_new_or_reselect_after_seal(sync):
    from summary_fixtures import assistant_member, window
    from summary_fixtures import pending as legacy_pending
    from summary_fixtures import submit as legacy_submit
    from test_memory_v2 import candidate

    prepare_summary(sync)
    old = candidate(sync, "旧的散步安排。", 1, capture=False)[0]
    old_window = window(sync, [old])
    bot = assistant_member(sync, old_window["manifest"]["members"][0])
    sync.core_head["sequence"] += 1
    old_window = window(sync, [old], assistant=[bot])
    done, _ = legacy_submit(sync, legacy_pending(sync, [old], old_window), "旧的散步安排。")
    ready(sync)
    forgotten = sync.post("memory/revise", sync.revision(done["result"]["record_ids"][0]))
    assert forgotten.status_code == 200, forgotten.text
    event = front(sync, "健康的新安排。", "after-forget", accept=False)
    accepted = sync.post(
        MATERIALS, dict(schema_version=1, event=event, context_windows=[old_window])
    )
    assert accepted.status_code == 200, accepted.text
    material = accepted.json()["materials"][0]
    body = dict(
        schema_version=1,
        segment_id="segment:optional-window",
        frame=material["frame"],
        material_refs=[material["material_ref"]],
        context_refs=[],
        context_windows=[old_window],
        omitted_context=False,
    )
    sealed = sync.post(SEAL, body)
    assert sealed.status_code == 200, sealed.text
    prepared = pending(sync, sealed.json())
    assert [member["member_id"] for member in prepared["segment_window"]["members"]] == [
        material["material_ref"]
    ]
    assert prepared["segment_window"]["coverage"]["omitted_context"] is True
    sync.status["current"] = 503
    # Lost create acknowledgements resolve before reselecting optional context.
    assert sync.post(SEAL, body).json() == sealed.json()
    sync.status.clear()
    assert submit(sync, prepared, output(prepared))[0].status_code == 200


def test_pure_core_higher_revision_is_a_new_material_with_exact_revision_coverage(sync):
    ready(sync)
    first, event = front(sync, "原来周六见。", "revision")
    prepared = pending(sync, seal(sync, [first], "segment:rev1")[0])
    assert submit(sync, prepared, output(prepared))[0].status_code == 200
    key = {
        key: value for key, value in event["sources"][0]["message_key"].items() if key != "revision"
    }
    physical = next(row for row in sync.physicals if row["key"] == key)
    admission = next(row for row in sync.admissions if row["selector"]["key"] == key)
    physical.update(revision=2, kind="edit", physical_receipt_id="physical:revision:2")
    content = dict(
        sync.test_contents[canonical(key)],
        message_key=dict(key, revision=2),
        kind="edit",
        parts=[dict(kind="text", text="改为周日见。")],
    )
    physical["content_digest"] = fingerprint(content)
    sync.test_contents[canonical(key)] = content
    admission["source"]["message_key"]["revision"] = 2
    admission["source"]["receipt_id"] = "admission:revision:2"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    event.update(
        event_id="event:raw:revision:2",
        input_revision=2,
        aggregate_version=event["aggregate_version"] + 1,
        sources=[copy.deepcopy(admission["source"])],
    )
    sync.turns[-1].update(
        input_revision=2,
        aggregate_version=event["aggregate_version"],
        input_sources=event["sources"],
    )
    sync.core_head["sequence"] += 1
    sync.platform_head["sequence"] += 1
    check = dict(
        sync.check(),
        turn_id=event["aggregate_id"],
        input_revision=event["input_revision"],
        scope=event["scope"],
        sources=event["sources"],
    )
    refreshed = sync.post("memory/source-sync/check", check)
    if refreshed.status_code == 503:
        # The first checkpoint commits the old revision's invalidation before
        # rejecting its old frozen turn check. The next uses only current rows.
        refreshed = sync.post("memory/source-sync/check", check)
    assert refreshed.status_code == 200, refreshed.text
    with sync.store.transaction() as db:
        event["scope_version"] = sync.service._scope_version(db, event["scope"])
        current = db.execute(
            "SELECT revision,state FROM sources WHERE key=?", (first["source_key"],)
        ).fetchone()
        assert tuple(current) == (2, "active")
    event.update(
        event_id="event:raw:revision:2:current",
        aggregate_version=event["aggregate_version"] + 1,
    )
    sync.turns[-1]["aggregate_version"] = event["aggregate_version"]
    if "scope_version" in sync.turns[-1]:
        sync.turns[-1]["scope_version"] = event["scope_version"]
    sync.core_head["sequence"] += 1
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    second = response.json()["materials"][0]
    assert (
        second["material_ref"] != first["material_ref"]
        and second["source_key"] == first["source_key"]
    )
    prepared2 = pending(sync, seal(sync, [second], "segment:rev2")[0])
    assert submit(sync, prepared2, output(prepared2))[0].status_code == 200
    with sync.store.transaction() as db:
        assert {
            row[0]
            for row in db.execute(
                "SELECT revision FROM source_writes WHERE source_key=?", (first["source_key"],)
            )
        } == {1, 2}


def test_canonical_episode_deduplicates_recall_and_retains_exact_aliases_once(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("A genuine observation/Core bridge")
    ready(sync)
    material, original = archive(sync, text="晚饭后去河边散步。")
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "晚饭后去河边散步。", "retention-alias", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        source_ref=original["source_ref"],
        source_digest=original["source_digest"],
        scope_version=original["scope_version"],
        archive_epoch=1,
    )
    assert (
        sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[])).status_code
        == 200
    )
    prepared = pending(sync, seal(sync, [material])[0])
    value = output(prepared)
    value["results"][0]["memories"][0].update(retention="short", importance="high")
    assert submit(sync, prepared, value)[0].status_code == 200
    core_key = sync.service.source_identity(event["sources"][0], event["scope"])
    from tianshu_memory.memory_documents import episodes

    with sync.service.operation(scope=event["scope"]) as db:
        rows = episodes(sync.service, db, [event["scope"]])
        assert {row["source_key"] for row in rows} == {material["source_key"]}
        lives = list(
            db.execute(
                "SELECT * FROM memory_source_lifecycle WHERE source_key IN (?,?)",
                (core_key, material["source_key"]),
            )
        )
        assert len(lives) == 2 and len({row["expires_at"] for row in lives}) == 1
        assert parse_time(lives[0]["expires_at"]) - parse_time("2026-10-24T00:00:00Z") == timedelta(
            days=7
        )


def test_late_core_alias_after_natural_expiry_never_revives_canonical_episode(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("A genuine observation/Core bridge")
    ready(sync)
    material, original = archive(sync, text="晚饭后去河边散步。")
    prepared = pending(sync, seal(sync, [material])[0])
    value = output(prepared)
    value["results"][0]["memories"][0]["retention"] = "short"
    assert submit(sync, prepared, value)[0].status_code == 200
    with sync.store.transaction() as db:
        expiry = db.execute(
            "SELECT expires_at FROM memory_source_lifecycle WHERE source_key=?",
            (material["source_key"],),
        ).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM memory_material_aliases").fetchone()[0] == 0
    sync.service.clock = lambda: parse_time(expiry) + timedelta(seconds=1)
    actor = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "晚饭后去河边散步。", "late-retention-alias", actor, accept=False)
    sync.passive_owner.resolve[fingerprint(event["sources"][0])] = dict(
        instance_id=original["event"]["platform_id"],
        self_id=original["event"]["self_id"],
        conversation_id=original["event"]["conversation_id"],
        source_ref=original["source_ref"],
        source_digest=original["source_digest"],
        scope_version=original["scope_version"],
        archive_epoch=1,
    )
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    assert response.json()["materials"] == [material]
    for intent, count in (("current", 0), ("history", 1)):
        query = request(sync, "河边散步", actor=actor)
        query["queries"][0].update(kinds=["episodes"], intent=intent)
        recalled = post(sync, "query", query)
        assert recalled.status_code == 200, recalled.text
        episodes = [item for item in recalled.json()["evidence"] if item["kind"] == "episode"]
        assert len(episodes) == count
        if episodes:
            assert episodes[0]["text"] == "晚饭后去河边散步。"
            with sync.store.transaction() as db:
                assert (
                    db.execute(
                        "SELECT source_key FROM memory_documents WHERE id=?",
                        (episodes[0]["episode_id"],),
                    ).fetchone()[0]
                    == material["source_key"]
                )
    key = sync.service.source_identity(event["sources"][0], event["scope"])
    with sync.store.transaction() as db:
        assert db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()[0] == "active"
        assert (
            db.execute(
                "SELECT expires_at FROM memory_source_lifecycle WHERE source_key=?",
                (material["source_key"],),
            ).fetchone()[0]
            == expiry
        )
        assert (
            db.execute("SELECT COUNT(*) FROM memory_documents WHERE kind='episode'").fetchone()[0]
            == 2
        )


def test_new_material_journals_delivery_updates_and_settles_without_singleton(sync):
    from test_relationship_source_sync import body, proof

    from tianshu_memory.relationship_migration import migrate

    ready(sync)
    migrate(sync.store, sync.directory / "segment-relationships.sqlite", clock=sync.service.clock)
    sync.config["callers"]["companion"]["operations"].append("relationships.settle")
    sync.save()
    event = front(sync, "今天一起散步。", "journal", accept=False)
    event.update(delivery_state="failed", reply_ids=[])
    first = copy.deepcopy(event)
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    rejected = sync.post("relationships/settle", body(sync, candidate=proof(sync, event)))
    assert rejected.status_code == 409, rejected.text
    event.update(
        event_id="event:journal:sent",
        aggregate_version=event["aggregate_version"] + 1,
        delivery_state="sent",
        reply_ids=["reply:journal:sent"],
    )
    sync.turns[-1].update(
        aggregate_version=event["aggregate_version"],
        phase="sent",
        delivery_state="sent",
        reply_ids=event["reply_ids"],
    )
    sync.core_head["sequence"] += 1
    response = sync.post(MATERIALS, dict(schema_version=1, event=event, context_windows=[]))
    assert response.status_code == 200, response.text
    assert response.json()["candidate_job_ref"] is None
    # The owner now reports sent; an obsolete failed packet retains its original
    # fresh-proof rejection, and cannot replace the newer received event.
    replay = sync.post("memory/turn-commits", dict(first, event_id="event:journal:late"))
    assert replay.status_code == 503, replay.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert sync.service.read_committed(db, event["aggregate_id"]) == canonical(event)
    candidate = body(sync, candidate=proof(sync, event))
    settled = sync.post("relationships/settle", candidate)
    assert settled.status_code == 200, settled.text
    assert settled.json()["settlement"]["applied_delta"] == (
        0 if sync.scope()["audience"] == "group" else 1
    )
    assert sync.post("relationships/settle", candidate).json() == settled.json()


def test_materials_never_accepts_replayed_scope_changed_null_job_receipt(sync):
    ready(sync)
    event = front(sync, "范围已经变化。", "rejected-journal", accept=False)
    event["scope_version"] = 999
    first = sync.post("memory/turn-commits", event)
    assert first.status_code == 200 and first.json()["state"] == "scope_changed", first.text
    replay = dict(event, event_id="event:rejected-journal:replay")
    response = sync.post(MATERIALS, dict(schema_version=1, event=replay, context_windows=[]))
    assert response.status_code == 409 and response.json()["code"] == "scope_changed", response.text
    with sync.store.transaction() as db:
        assert sync.service.read_committed(db, event["aggregate_id"]) is None
        assert db.execute("SELECT COUNT(*) FROM memory_materials").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
