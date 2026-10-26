"""Real browser registration and guarded storage; source owners are explicit peers."""

import copy

import pytest
from test_browser_catalog import post
from test_group_context import owner
from test_segments import (
    MATERIALS,
    archive,
    forget_original,
    front,
    output,
    pending,
    ready,
    seal,
    submit,
)
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import fingerprint


def bootstrap(h):
    ready(h)
    h.config["callers"]["platform"] = dict(
        token="synthetic-platform-browser-only-credential",
        issuer="platform",
        issuer_url=h.config["callers"]["companion"]["issuer_url"],
        issuer_token="synthetic-origin-token",
        issuer_ca_file=str(h.certificates / "ca.pem"),
        allowed_actors=["actor:a", "actor:b"],
        operations=["browse"],
    )
    context = copy.deepcopy(h.contexts["synthetic-viewer:0"])
    context.update(assertion_ref="synthetic-browser", authenticated_service="platform")
    h.contexts["synthetic-browser"] = context
    h.config["browser_readers"] = dict(
        platform=dict(account=h.account, actor_id=h.scope()["actor_id"], scopes=[h.scope()])
    )
    h.save()


def onebot_owner(h):
    """Install a real QQ binding and native channel before its first source proof."""
    index = owner(
        h,
        audience=h.scope()["audience"],
        account=dict(namespace="qq", immutable_account_id="60003"),
    )
    # The shared owner fixture starts with a WEB channel. OneBot's public bridge
    # actually uses QQ; install that producer fact before Memory sees this source.
    for value in (h.physicals[index], h.admissions[index], h.grants[index], h.turns[index]):
        stack = [value]
        while stack:
            part = stack.pop()
            if isinstance(part, dict):
                if isinstance(part.get("channel"), dict):
                    part["channel"]["namespace"] = "qq"
                stack.extend(part.values())
            elif isinstance(part, list):
                stack.extend(part)
    context = copy.deepcopy(h.contexts[f"synthetic-viewer:{index}"])
    context.update(assertion_ref="synthetic-browser", authenticated_service="platform")
    h.contexts["synthetic-browser"] = context
    reader = h.config["browser_readers"]["platform"]
    reader.update(account=context["verified_account"], scopes=[h.scope(index)])
    h.save()
    return index


def topic_request(h, **values):
    body = dict(
        schema_version=1,
        request_id=h.command()["request_id"],
        scope=h.scope(),
        origin=dict(assertion_ref="synthetic-browser"),
        subject=dict(kind="group", conversation_id=h.scope()["conversation_id"]),
        start_at=None,
        end_at=None,
        limit=100,
        cursor=None,
    )
    body.update(values)
    return body


def committed_topic(h, materials, serial="segment:topic"):
    prepared = pending(h, seal(h, materials, serial)[0])
    value = output(prepared)
    value["topics"] = [
        dict(
            topic_id="topic:local",
            summary="Participants discussed a walk.",
            person_ids=[
                target["scope"]["person_id"] for target in prepared["segment_window"]["targets"]
            ],
            member_ids=[member["member_id"] for member in prepared["segment_window"]["members"]],
        )
    ]
    response, _ = submit(h, prepared, value)
    assert response.status_code == 200, response.text
    return prepared, response.json()


def test_group_topics_resolve_all_people_from_real_segment_receipt(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("The topic contract is group-only")
    bootstrap(sync)
    first, _ = front(sync, "A plans a walk.", "topic:a")
    second_owner = owner(sync, account=dict(namespace="qq", immutable_account_id="60002"))
    second, _ = front(sync, "B plans another walk.", "topic:b", second_owner)
    prepared, receipt = committed_topic(sync, [first, second])
    response = post(sync, "topics", topic_request(sync))
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    topic = item["topics"][0]
    assert item["window_id"] == prepared["segment_window"]["window_id"]
    assert set(topic["person_ids"]) == {first["person_id"], second["person_id"]}
    assert {member["person_id"] for member in topic["member_refs"]} == set(topic["person_ids"])
    assert all(
        member["role"] == "human" and member["actor_id"] is None and member["reality"] == "real"
        for member in topic["member_refs"]
    )
    expected = {
        (result["subject_person_id"], ref["record_id"], ref["record_version"])
        for result in receipt["results"]
        for ref in result["memory_refs"]
    }
    assert {
        (ref["subject_person_id"], ref["record_id"], ref["record_version"])
        for ref in topic["memory_refs"]
    } == expected
    assert item["coverage"]["result_person_count"] == 2
    assert not response.json()["omitted_invalidated"]
    assert "source_snapshot" not in response.text and "manifest" not in response.text


def test_group_topics_omit_whole_forgotten_segment_without_hiding_healthy_one(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("The topic contract is group-only")
    bootstrap(sync)
    # The second segment can use the first summary as known context. Forget the
    # later segment so the healthy one has no dependency on the forgotten input.
    healthy, _ = front(sync, "Another walk.", "topic:healthy")
    committed_topic(sync, [healthy], "segment:healthy")
    forgotten, _ = front(sync, "First walk.", "topic:forgotten")
    committed_topic(sync, [forgotten], "segment:forgotten")
    forget_original(sync, forgotten, 0)
    response = post(sync, "topics", topic_request(sync))
    assert response.status_code == 200, response.text
    assert [item["segment_id"] for item in response.json()["items"]] == ["segment:healthy"]
    assert response.json()["omitted_invalidated"]


def test_group_topics_cursor_time_and_scope_are_bound_and_current_origin_is_required(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("The topic contract is group-only")
    bootstrap(sync)
    for serial in ("one", "two"):
        material, _ = front(sync, "A walk.", "pagination:" + serial)
        committed_topic(sync, [material], "segment:" + serial)
    first = post(sync, "topics", topic_request(sync, limit=1))
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    second = post(sync, "topics", topic_request(sync, limit=1, cursor=cursor))
    assert second.status_code == 200, second.text
    assert {first.json()["items"][0]["segment_id"], second.json()["items"][0]["segment_id"]} == {
        "segment:one",
        "segment:two",
    }
    assert (
        post(
            sync,
            "topics",
            topic_request(sync, subject=dict(kind="group", conversation_id="another:group")),
        ).status_code
        == 403
    )
    assert (
        post(
            sync, "topics", topic_request(sync, cursor=cursor, start_at="2026-01-01T00:00:00Z")
        ).status_code
        == 400
    )
    outside = post(sync, "topics", topic_request(sync, end_at="2000-01-01T00:00:00Z"))
    assert outside.status_code == 200 and outside.json()["items"] == []
    sync.contexts["synthetic-browser"]["revoked"] = True
    denied = post(sync, "topics", topic_request(sync))
    assert denied.status_code in {400, 403}, denied.text
    assert "items" not in denied.json()


def test_topics_never_accept_private_scope_or_turn_source_failure_into_empty_success(sync):
    bootstrap(sync)
    if sync.scope()["audience"] != "group":
        assert post(sync, "topics", topic_request(sync)).status_code == 400
        return
    material, _ = front(sync, "A walk.", "topic:unavailable")
    committed_topic(sync, [material])
    sync.status["snapshot"] = 503
    assert post(sync, "topics", topic_request(sync)).status_code == 503


def mapping(h, *, channel=None, self_id="90001", room_id="20002", conversation=None):
    scope = h.config["browser_readers"]["platform"]["scopes"][0]
    if channel is None:
        channel = dict(h.event()["sources"][0]["message_key"]["channel"], namespace="qq")
    value = dict(
        scope=copy.deepcopy(scope),
        channel=channel,
        platform_id="instance-a",
        self_id=self_id,
        room_id=room_id,
        conversation_id=conversation
        or (
            "group:20002"
            if scope["audience"] == "group"
            else "private:"
            + h.config["browser_readers"]["platform"]["account"]["immutable_account_id"]
        ),
    )
    h.config["browser_readers"]["platform"]["archive_mappings"] = {"onebot:fixed": value}
    h.save()
    return value


def archive_request(h, native="never-known", sender=None, **fields):
    config = h.config["browser_readers"]["platform"]["archive_mappings"]["onebot:fixed"]
    digest = "a" * 64
    item = dict(
        evidence_id="audit-evidence:0:" + "b" * 64 + ":archive:" + digest,
        content_hash=digest,
        sender_id=sender
        or h.config["browser_readers"]["platform"]["account"]["immutable_account_id"],
        alias=dict(
            platform="qq",
            account_id=config["self_id"],
            room_id=config["room_id"],
            message_type="group" if config["scope"]["audience"] == "group" else "private",
            id_kind="external",
            message_id=native,
            import_source_id=None,
        ),
    )
    body = dict(
        schema_version=1,
        request_id=h.command()["request_id"],
        scope=copy.deepcopy(config["scope"]),
        origin=dict(assertion_ref="synthetic-browser"),
        mapping_id="onebot:fixed",
        items=[item],
    )
    body.update(fields)
    return body


def test_archive_import_without_core_history_is_usable_but_unsupported_namespaces_are_distinct(
    sync,
):
    bootstrap(sync)
    mapping(sync)
    body = archive_request(sync)
    response = post(sync, "archive-check", body)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["forget_state"] == "no_known_forget"
    assert response.json()["items"][0]["mapping"] == "unmapped"
    body["items"][0]["alias"].update(id_kind="qqnt_source", import_source_id="import:one")
    response = post(sync, "archive-check", body)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["forget_state"] == "unsupported"
    assert response.json()["items"][0]["mapping"] == "unmapped"


def test_archive_core_only_forget_and_sender_identity_do_not_need_observation(sync):
    bootstrap(sync)
    participant = onebot_owner(sync)
    material, event = front(sync, "Do not remember this walk.", "archive:core-only", participant)
    mapping(sync, channel=event["sources"][0]["message_key"]["channel"])
    body = archive_request(sync, event["sources"][0]["message_key"]["message_id"])
    response = post(sync, "archive-check", body)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["mapping"] == "core"
    assert response.json()["items"][0]["forget_state"] == "no_known_forget"
    wrong = copy.deepcopy(body)
    wrong["items"][0]["sender_id"] = wrong["items"][0]["alias"]["account_id"]
    assert post(sync, "archive-check", wrong).json()["items"][0]["forget_state"] == "unsupported"
    forget_original(sync, material, participant)
    response = post(sync, "archive-check", body)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["forget_state"] == "withdrawn"
    with sync.store.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM memory_materials WHERE kind='observation'").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize("direction", ["core", "observation"])
def test_archive_known_physical_alias_closure_keeps_either_forget_negative(sync, direction):
    if sync.scope()["audience"] != "group":
        pytest.skip("The actual two-author observation bridge is group-only")
    bootstrap(sync)
    material, original = archive(sync, text="Do not retain this walk.")
    participant = owner(sync, account=dict(namespace="qq", immutable_account_id="60001"))
    event = front(sync, "Do not retain this walk.", "archive:alias", participant, accept=False)
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
    mapping(
        sync,
        self_id=original["event"]["self_id"],
        conversation=original["event"]["conversation_id"],
    )
    body = archive_request(sync, original["event"]["event_id"], sender="60001")
    before = post(sync, "archive-check", body)
    assert before.status_code == 200 and before.json()["items"][0]["mapping"] == "observation", (
        before.text
    )
    selected = (
        material
        if direction == "observation"
        else dict(
            material, source_key=sync.service.source_identity(event["sources"][0], event["scope"])
        )
    )
    forget_original(sync, selected, participant)
    response = post(sync, "archive-check", body)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["forget_state"] == "withdrawn"


def test_archive_batch_is_ordered_bounded_and_has_no_current_source_proof_claim(sync):
    bootstrap(sync)
    mapping(sync)
    body = archive_request(sync)
    body["items"] = [
        {
            **copy.deepcopy(body["items"][0]),
            "evidence_id": "audit-evidence:0:" + "b" * 64 + ":item" + str(i) + ":" + "a" * 64,
            "alias": {**body["items"][0]["alias"], "message_id": "unknown:" + str(i)},
        }
        for i in range(100)
    ]
    before = len([call for call in sync.calls if call[0] in {"snapshot", "current"}])
    response = post(sync, "archive-check", body)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 100
    assert [(item["evidence_id"], item["content_hash"]) for item in response.json()["items"]] == [
        (item["evidence_id"], item["content_hash"]) for item in body["items"]
    ]
    assert all(item["forget_state"] == "no_known_forget" for item in response.json()["items"])
    assert len([call for call in sync.calls if call[0] in {"snapshot", "current"}]) == before
    body["items"].append(copy.deepcopy(body["items"][0]))
    assert post(sync, "archive-check", body).status_code == 400


def test_archive_config_scope_hash_and_current_origin_fail_closed(sync):
    bootstrap(sync)
    mapping(sync)
    body = archive_request(sync)
    bad_hash = copy.deepcopy(body)
    bad_hash["items"][0]["content_hash"] = "c" * 64
    assert post(sync, "archive-check", bad_hash).status_code == 400
    sync.config["browser_readers"]["platform"]["archive_mappings"]["onebot:fixed"]["scope"][
        "conversation_id"
    ] = "other:conversation"
    sync.save()
    assert post(sync, "archive-check", body).status_code == 403
    mapping(sync)
    sync.contexts["synthetic-browser"]["revoked"] = True
    denied = post(sync, "archive-check", body)
    assert denied.status_code in {400, 403}, denied.text
    assert "items" not in denied.json()
