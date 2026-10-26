"""The actor query projection keeps its exact episode and content dependency closure."""

import copy
from datetime import timedelta

import pytest
from test_actor_memory import actor as actor
from test_actor_memory import actor_storage as actor_storage
from test_actor_memory import append, execute, read, request

from tianshu_memory.actor_memory.application import INTERNAL
from tianshu_memory.domain import Fault, canonical


def derive(episode, **values):
    payload = request(
        "append_derived",
        idempotency_key="derived:one",
        kind="understanding",
        text="Reading pages helps my understanding.",
        episode_refs=[
            dict(episode_id=episode["episode_id"], expected_revision=episode["revision"])
        ],
        replaces=[],
        importance="high",
        retention="short",
    )
    payload.update(values)
    return payload


def query(actor, **values):
    payload = request(
        "query",
        text="reading pages",
        context=copy.deepcopy(INTERNAL),
        kinds=["episode", "understanding", "preference", "reflection", "autobiography"],
        limit=32,
        budget_bytes=32768,
    )
    payload.update(values)
    return execute(actor, payload)


def check(actor, *refs, **values):
    payload = request("check_memory", memories=list(refs), context=copy.deepcopy(INTERNAL))
    payload.update(values)
    return execute(actor, payload)


@pytest.mark.parametrize("kind", ["understanding", "preference", "reflection", "autobiography"])
def test_derived_receipt_query_and_original_episode_basis(actor, kind):
    episode = execute(actor, append(actor))
    payload = derive(episode, kind=kind)
    first = execute(actor, payload)
    assert execute(actor, {**payload, "request_id": "req:retry"}) == {
        **first,
        "request_id": "req:retry",
    }
    original = execute(
        actor, request("get_receipt", command="append_derived", idempotency_key="derived:one")
    )
    assert original == first
    duplicate = execute(actor, {**payload, "idempotency_key": "different-key"})
    assert duplicate["state"] == "duplicate" and duplicate["ref"] == first["ref"]
    result = query(actor)
    item = next(item for item in result["items"] if item["ref"] == first["ref"])
    assert item["kind"] == kind and item["text"] == payload["text"]
    assert item["episode_refs"] == payload["episode_refs"]
    assert item["source_refs"] == [actor["owner"].fact["source"]]
    assert check(actor, first["ref"])["items"][0]["state"] == "current"
    assert not check(actor, first["ref"])["items"][0]["disclosure_allowed"]


def test_derived_from_other_actor_or_changed_episode_cannot_commit(actor):
    episode = execute(actor, append(actor))
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, {**derive(episode), "actor_id": "actor:b"})
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, derive({**episode, "revision": 2}))
    assert query(actor, actor_id="actor:b")["items"] == []
    with actor["store"].transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM actor_derived").fetchone()[0] == 0


@pytest.mark.parametrize("via", ["episode", "memory-episode", "source"])
def test_episode_negative_invalidates_derived_and_late_write_cannot_resurrect(actor, via):
    episode = execute(actor, append(actor))
    derived = execute(actor, derive(episode))
    if via == "episode":
        execute(
            actor,
            request(
                "withdraw_episode",
                idempotency_key="withdraw:episode",
                episode_id=episode["episode_id"],
                expected_revision=1,
                reason="forgotten",
            ),
        )
    elif via == "memory-episode":
        execute(
            actor,
            request(
                "withdraw_memory",
                idempotency_key="withdraw:episode",
                ref=dict(kind="episode", id=episode["episode_id"], revision=1),
                reason="forgotten",
            ),
        )
    else:
        actor["owner"].states[canonical(actor["owner"].fact["source"])] = "withdrawn"
        execute(
            actor,
            request(
                "apply_source_revision",
                idempotency_key="source:withdrawn",
                source=actor["owner"].fact["source"],
            ),
        )
    item = check(actor, derived["ref"])["items"][0]
    assert (
        item["state"] == "withdrawn" and not item["read_allowed"] and not item["disclosure_allowed"]
    )
    assert query(actor)["items"] == []
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, derive(episode, idempotency_key="late:derived"))
    assert execute(actor, derive(episode)) == derived


def test_explicit_derived_forget_leaves_episode_and_replacement_is_derived_only(actor):
    episode = execute(actor, append(actor))
    first = execute(actor, derive(episode))
    second_request = derive(
        episode,
        idempotency_key="derived:replacement",
        text="Reading pages changed my understanding.",
        replaces=[first["ref"]],
    )
    second = execute(actor, second_request)
    assert check(actor, first["ref"])["items"][0]["state"] == "withdrawn"
    assert execute(actor, read(episode))["episode"]["state"] == "active"
    withdrawn = execute(
        actor,
        request(
            "withdraw_memory",
            idempotency_key="withdraw:derived",
            ref=second["ref"],
            reason="forgotten",
        ),
    )
    assert withdrawn["state"] == "withdrawn" and withdrawn["ref"]["revision"] == 2
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, {**second_request, "idempotency_key": "late:replacement"})
    assert query(actor, kinds=["episode"])["items"]


def test_private_basis_cannot_be_disclosed_or_relabelled_by_a_public_basis(actor):
    actor["owner"].fact["source_constraints"] = dict(
        kind="private", person_id="person:a", conversation_id="conv:private"
    )
    private = execute(actor, append(actor))
    actor["owner"].sequence += 1
    actor["owner"].fact["source"].update(object_id="reading:public", content_hash="b" * 64)
    actor["owner"].fact["source_constraints"] = dict(kind="public")
    public = execute(actor, {**append(actor), "idempotency_key": "append:public"})
    derived = execute(
        actor,
        derive(
            private,
            episode_refs=[
                dict(episode_id=private["episode_id"], expected_revision=1),
                dict(episode_id=public["episode_id"], expected_revision=1),
            ],
        ),
    )
    group = dict(
        purpose="dialogue",
        audience=dict(kind="group", conversation_id="conv:group", assertion_ref="origin:group"),
    )
    item = check(actor, derived["ref"], context=group)["items"][0]
    assert item["state"] == "forbidden" and not item["read_allowed"]
    assert query(actor, kinds=["understanding"], context=group)["items"] == []
    assert query(actor, kinds=["understanding"])["items"][0]["ref"] == derived["ref"]


def test_content_withdrawal_invalidates_derived_and_query_proves_all_bases(actor):
    content = dict(
        producer="knowledge", object_id="content:one", revision="1", content_hash="b" * 64
    )
    actor["owner"].fact["content_reads"] = [
        dict(
            content_ref=content,
            coverage=[dict(unit="pages", start=1, end=2, complete=False, gaps=["page three"])],
        )
    ]
    episode = execute(actor, append(actor))
    derived = execute(actor, derive(episode))
    actor["owner"].calls.clear()
    result = query(actor, kinds=["understanding"])
    assert result["items"][0]["ref"] == derived["ref"]
    assert all(content in refs for kind, refs, _ in actor["owner"].calls if kind == "check")
    actor["owner"].states[canonical(content)] = "withdrawn"
    result = query(actor, kinds=["understanding"])
    assert result["items"] == [] and result["omitted"]
    assert check(actor, derived["ref"])["items"][0]["state"] == "withdrawn"


def test_query_budget_is_whole_item_and_retention_does_not_extend_source(actor):
    episode = execute(actor, append(actor))
    derived = execute(actor, derive(episode))
    result = query(actor, kinds=["understanding"], budget_bytes=1)
    assert result["items"] == [] and result["omitted"]
    initial = query(actor, kinds=["understanding"])["items"][0]
    original_clock = actor["app"].clock
    actor["app"].clock = lambda: original_clock() + timedelta(days=8)
    assert query(actor, kinds=["understanding"])["items"] == []
    assert check(actor, derived["ref"])["items"][0]["state"] == "unavailable"
    assert execute(actor, read(episode))["episode"]["state"] == "active"
    with actor["store"].transaction() as db:
        assert (
            db.execute(
                "SELECT expires_at FROM actor_derived WHERE id=?", (derived["ref"]["id"],)
            ).fetchone()[0]
            == initial["expires_at"]
        )


def test_query_does_not_swallow_source_unavailable_or_mid_checkpoint_change(actor):
    episode = execute(actor, append(actor))
    execute(actor, derive(episode))
    actor["owner"].states[canonical(actor["owner"].fact["source"])] = "unavailable"
    with pytest.raises(Fault, match="dependency_unavailable"):
        query(actor)
    actor["owner"].states.clear()

    def change(facts):
        actor["owner"].hook = None
        actor["owner"].sequence += 1

    actor["owner"].hook = change
    with pytest.raises(Fault, match="source_changed"):
        query(actor)


def test_empty_actor_query_still_checks_current_role_and_external_context(actor):
    actor["owner"].context_allowed = False
    with pytest.raises(Fault, match="forbidden"):
        query(
            actor,
            context=dict(
                purpose="dialogue",
                audience=dict(kind="group", conversation_id="conv:a", assertion_ref="origin:a"),
            ),
        )
    actor["owner"].context_allowed = True
    actor["owner"].allowed = False
    with pytest.raises(Fault, match="forbidden"):
        query(actor)
