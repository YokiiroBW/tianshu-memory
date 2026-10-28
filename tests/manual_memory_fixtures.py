"""Synthetic original content and explicit records through the current manual port."""

import copy
import json

from tianshu_memory.domain import canonical, fingerprint


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
    valid_time=None,
    keywords=None,
    conditions=(),
    negations=(),
    explicit=False,
    entity=None,
    expected_status=200,
    expected_state="completed",
    memories=None,
    actor=0,
    sent_at=None,
):
    physical, admission, grant = [
        copy.deepcopy(items[actor]) for items in (h.physicals, h.admissions, h.grants)
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
        sent_at=sent_at or f"2026-10-11T00:00:{second:02d}Z",
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
    turn = copy.deepcopy(h.turns[actor])
    old_id = turn["turn_id"]
    new_id = f"quality-turn:{second}"
    turn = json.loads(canonical(turn).replace(old_id, new_id))
    turn["input_sources"] = [admission["source"]]
    event = turn["committed_event"]
    event.update(event_id=f"quality-event:{second}", sources=[admission["source"]])
    with h.store.transaction() as db:
        event["scope_version"] = h.service._scope_version(db, h.scope(actor))
    # Turn and committed event assert the same owner state.
    if "scope_version" in turn:
        turn["scope_version"] = event["scope_version"]
    h.turns.append(turn)
    h.core_head["sequence"] += 1
    h.platform_head["sequence"] += 1
    from tianshu_memory.memory_context import MemoryContext

    h.service.contracts.load_context()
    proposal = dict(
        command=h.command(actor),
        scope=h.scope(actor),
        batch_ref=None,
        item_id=f"fixture:manual:{second}",
        kind="upsert",
        target=dict(
            category="evidence",
            field_key=key,
            item_key=None,
            record_id=None,
            expected_version=None,
        ),
        units=[
            dict(
                statement=text,
                conditions=list(conditions),
                negations=list(negations),
                valid_time=valid_time or ("当前" if state == "current" else "过去"),
                uncertainty="confirmed" if explicit else "inferred",
                reality="real",
                sources=[admission["source"]],
            )
        ],
        evidence_refs=[admission["source"]],
        proof_ref=None,
    )
    h.service.contracts.validate("memory-context#proposal_request", proposal)
    receipt = MemoryContext(h.service).propose(proposal, h.contexts[f"synthetic-viewer:{actor}"])
    assert receipt["state"] == "committed", receipt
    return receipt, receipt["operation_id"]


def recall(h, text):
    query = h.selection(budget=100000)
    query.update(query_text=text, selection=["evidence"])
    response = h.post("memory/select", query)
    assert response.status_code == 200, response.text
    return response.json()["selected_units"]


def reviewed(h, draft, *, actor=0):
    """Apply complete explicit units through the existing manual application."""
    from tianshu_memory.memory_context import MemoryContext

    request = dict(
        command=h.command(actor),
        scope=draft["scope"],
        batch_ref=None,
        item_id="fixture:reviewed",
        kind="upsert",
        target=dict(
            category=draft["category"],
            field_key=draft.get("field_key"),
            item_key=draft.get("item_key"),
            record_id=None,
            expected_version=None,
        ),
        units=draft["units"],
        evidence_refs=list(
            {
                canonical(source): source for unit in draft["units"] for source in unit["sources"]
            }.values()
        ),
        proof_ref=None,
    )
    h.contracts.validate("memory-context#proposal_request", request)
    result = MemoryContext(h.service).propose(request, h.contexts[f"synthetic-viewer:{actor}"])
    assert result["state"] == "committed", result
    return result


def capture_original(h, identity):
    """Capture a previously received event through the actual current material port."""
    return h.post(
        "memory/candidates/materials",
        dict(schema_version=1, event=h.fixture_events[identity], context_windows=[]),
    )
