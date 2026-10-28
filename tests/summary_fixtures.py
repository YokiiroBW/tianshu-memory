"""Current segment fixtures with genuine authenticated source windows and HTTP writes."""

import copy
import hashlib

from test_memory_v2 import post, request, setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical, fingerprint


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare(h):
    setup(h)
    h.summary_replies = getattr(h, "summary_replies", {})
    h.fixture_events = getattr(h, "fixture_events", {})

    def check(body):
        if "manifest" not in body:
            from tianshu_memory.segment_sources import frame

            return dict(
                schema_version=1,
                valid=True,
                channel=body["channel"],
                delivery_channel=body["delivery_channel"],
                frame=dict(frame(h.scope()), actor_id=body["actor_id"]),
            )
        manifest = body["manifest"]
        invalid = []
        turns = {turn["turn_id"]: turn for turn in h.turns}
        for member in manifest["members"]:
            turn = turns.get(member["turn_id"])
            valid = turn is not None and turn["scope"] == member["source_scope"]
            if member["role"] == "user":
                ref = member["reference"]
                valid = (
                    valid
                    and ref["input_revision"] == turn["input_revision"]
                    and ref["source"] in turn["input_sources"]
                )
                key = {
                    key: value
                    for key, value in ref["source"]["message_key"].items()
                    if key != "revision"
                }
                physical = next((row for row in h.physicals if row["key"] == key), None)
                valid = valid and physical is not None and physical["state"] == "active"
                content = h.test_contents.get(canonical(key))
                valid = (
                    valid
                    and content is not None
                    and text_hash(
                        "\n".join(
                            part["text"] for part in content["parts"] if part["kind"] == "text"
                        )
                    )
                    == member["content_sha256"]
                )
            else:
                reply = h.summary_replies.get(member["reference"]["reply_id"])
                valid = (
                    valid
                    and reply is not None
                    and reply["state"] == "sent"
                    and reply["reference"] == member["reference"]
                    and text_hash(reply["text"]) == member["content_sha256"]
                )
            if not valid:
                invalid.append(member["member_id"])
        return dict(
            schema_version=1,
            request_id=body["request_id"],
            request_digest=fingerprint(body),
            window_id=manifest["window_id"],
            state="invalid" if invalid else "valid",
            invalid_member_ids=invalid,
            checked_at="2026-10-24T00:00:00Z",
        )

    h.window_checker = check


def window(h, jobs, *, context=(), assistant=()):
    members, bodies, new_turns = [], [], []
    with h.store.transaction() as db:
        for purpose, ids in (("context", context), ("new", jobs)):
            for identity in ids:
                event = h.fixture_events[identity]
                if purpose == "new":
                    new_turns.append(
                        dict(turn_id=event["aggregate_id"], input_revision=event["input_revision"])
                    )
                for index, source in enumerate(event["sources"]):
                    key = {
                        key: value
                        for key, value in source["message_key"].items()
                        if key != "revision"
                    }
                    content = h.test_contents[canonical(key)]
                    text = "\n".join(
                        part["text"] for part in content["parts"] if part["kind"] == "text"
                    )
                    account = content["author"]
                    binding = h.service._binding(db, account)
                    member = dict(
                        member_id=f"window-member:{event['aggregate_id']}:{index}",
                        turn_id=event["aggregate_id"],
                        turn_sequence=event["turn_sequence"],
                        role="user",
                        speaker=dict(
                            kind="person", person_id=event["scope"]["person_id"], account=account
                        ),
                        source_scope=event["scope"],
                        binding_version=binding["version"],
                        purpose=purpose,
                        reference=dict(input_revision=event["input_revision"], source=source),
                        observed_at=content["sent_at"],
                        content_sha256=text_hash(text),
                        memory_dependencies=dict(
                            group_dependencies=[], episode_dependencies=[], source_dependencies=[]
                        ),
                    )
                    members.append(member)
                    bodies.append(dict(member_id=member["member_id"], text=text))
        event = h.fixture_events[jobs[0]]
    for member, text in assistant:
        members.append(copy.deepcopy(member))
        bodies.append(dict(member_id=member["member_id"], text=text))
    channel = copy.deepcopy(event["sources"][0]["message_key"]["channel"])
    manifest = dict(
        schema_version=1,
        scope=event["scope"],
        channel=channel,
        anchor=new_turns[-1],
        new_turns=new_turns,
        members=members,
        coverage=dict(complete=True, omitted_before=False, omitted_after=False),
    )
    manifest["window_id"] = fingerprint(manifest)
    return dict(manifest=manifest, members=bodies)


def query(h, text, *, intent="current", kinds=("facts", "episodes")):
    payload = request(h, text)
    payload["queries"][0].update(intent=intent, kinds=list(kinds))
    response = post(h, "query", payload)
    assert response.status_code == 200, response.text
    h.contracts.validate("memory-context-v2#query_response", response.json())
    return response.json()


def assistant_member(h, human, text="好的，我们之后继续聊旅行。", *, memory_dependencies=None):
    turn = next(turn for turn in h.turns if turn["turn_id"] == human["turn_id"])
    reply_id = "summary-reply:" + human["turn_id"]
    reference = dict(
        reply_id=reply_id,
        segment_sequence=1,
        result_version=1,
        receipt_digest=fingerprint(
            dict(reply_id=reply_id, state="sent", channel_message_ids=["sent:" + reply_id])
        ),
    )
    h.summary_replies[reply_id] = dict(state="sent", reference=reference, text=text)
    turn["reply_ids"] = [*turn["reply_ids"], reply_id]
    member = dict(
        human,
        member_id="assistant:" + human["member_id"],
        role="assistant",
        speaker=dict(kind="actor", actor_id=human["source_scope"]["actor_id"]),
        purpose="context",
        reference=reference,
        content_sha256=text_hash(text),
    )
    if memory_dependencies is not None:
        member["memory_dependencies"] = memory_dependencies
    return member, text


def withdraw_source(h, job):
    event = h.fixture_events[job]
    key = {
        key: value for key, value in event["sources"][0]["message_key"].items() if key != "revision"
    }
    physical = next(item for item in h.physicals if item["key"] == key)
    physical.update(
        state="withdrawn",
        kind="retract",
        revision=physical["revision"] + 1,
        physical_receipt_id="withdrawn:" + job,
        content_digest="b" * 64,
    )
    h.core_head["sequence"] += 1


def current_segments(h):
    """Install only the current material journal; keep the owners' existing data."""
    with h.store.transaction() as db:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
    if "observation_schema" not in metadata:
        h.store.migrate_observations(h.directory / "fixture-before-observations.sqlite")
    if "qq_alias_schema" not in metadata:
        h.store.migrate_qq_aliases(h.directory / "fixture-before-aliases.sqlite")
    h.store.migrate_segments(h.directory / "fixture-before-segments.sqlite")


def pending(h, jobs, supplied):
    """Build the tested memory through schema 6, retaining the complete Core context."""
    from test_segments import pending as segment_pending
    from test_segments import seal

    current_segments(h)
    materials = []
    for identity in jobs:
        response = h.post(
            "memory/candidates/materials",
            dict(schema_version=1, event=h.fixture_events[identity], context_windows=[]),
        )
        assert response.status_code == 200, response.text
        materials.extend(response.json()["materials"])
    # The supplied Core window remains real context, including BOT dependencies.
    # Include it when sealing rather than reinterpreting its words in the fixture.
    if supplied["manifest"]["members"]:
        body = dict(
            schema_version=1,
            segment_id="fixture:window:" + fingerprint(jobs),
            frame=materials[0]["frame"],
            material_refs=[item["material_ref"] for item in materials],
            context_refs=[],
            context_windows=[supplied],
            omitted_context=False,
        )
        response = h.post("memory/candidates/segments", body)
        assert response.status_code == 200, response.text
        sealed = response.json()
    else:
        sealed, _ = seal(h, materials, "fixture:segment:" + fingerprint(jobs))
    h.fixture_events[sealed["job_id"]] = h.fixture_events[jobs[0]]
    return segment_pending(h, sealed)


def submit(h, prepared, summary, *, portrait=None, importance="normal"):
    """Submit genuine schema 6; expose only derived inspection fields for read tests."""
    from test_segments import submit as segment_submit

    values = []
    for target in prepared["segment_window"]["targets"]:
        memories = (
            []
            if summary is None
            else [
                dict(
                    text=summary,
                    kind="experience",
                    state="described",
                    valid_until=None,
                    source_member_ids=target["new_member_ids"],
                    prior_refs=[],
                    operation="append",
                    importance=importance,
                    retention={"low": "short", "normal": "medium", "high": "long"}[importance],
                )
            ]
        )
        values.append(
            dict(
                subject_person_id=target["scope"]["person_id"],
                covered_member_ids=target["new_member_ids"],
                memories=memories,
                portrait=portrait,
            )
        )
    response, body = segment_submit(
        h, prepared, dict(extraction_schema=6, topics=[], results=values)
    )
    assert response.status_code == 200, response.text
    receipt = response.json()
    h.contracts.validate("memory-extraction#segment_receipt", receipt)
    return inspect_receipt(h, receipt), body


def inspect_receipt(h, receipt):
    """Derive read-test conveniences from an actual schema-6 receipt and stored rows."""
    # These are SQL inspection conveniences, not an old-protocol HTTP response.
    refs = [ref for value in receipt["results"] for ref in value["memory_refs"]]
    portrait_refs = [ref for value in receipt["results"] for ref in value["portrait_refs"]]
    with h.store.transaction() as db:
        groups = list(
            dict.fromkeys(
                db.execute(
                    "SELECT group_id FROM records WHERE id=?", (ref["record_id"],)
                ).fetchone()[0]
                for ref in refs
            )
        )
    inspection = dict(
        group_ids=groups,
        record_ids=[ref["record_id"] for ref in refs + portrait_refs],
        summary_record_refs=refs,
        summary_state="committed" if refs else "skipped",
        portrait=dict(state=receipt["results"][0]["portrait_state"], record_refs=portrait_refs),
    )
    return dict(receipt, result=inspection)
