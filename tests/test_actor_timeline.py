"""UTC occurrence pagination and audience proofs on the original actor authority."""

import base64
import json

import pytest
from test_actor_memory import actor as actor
from test_actor_memory import actor_storage as actor_storage
from test_actor_memory import append, execute, request

from tianshu_memory.domain import Fault, canonical


def timeline(actor, **values):
    return execute(
        actor,
        request(
            "query",
            **(
                dict(
                    mode="timeline",
                    window={"start": "2026-10-26T00:00:00Z", "end": "2026-10-27T00:00:00Z"},
                    cursor=None,
                    kinds=["episode"],
                    limit=32,
                    budget_bytes=32768,
                    context={"purpose": "diary", "audience": {"kind": "actor_only"}},
                )
                | values
            ),
        ),
    )


def seed(actor, times):
    receipt = execute(actor, append(actor))
    with actor["store"].transaction(write=True) as db:
        original = dict(
            db.execute(
                "SELECT * FROM actor_episodes WHERE id=?", (receipt["episode_id"],)
            ).fetchone()
        )
        db.execute("UPDATE actor_episodes SET state='withdrawn' WHERE id=?", (original["id"],))
        for index, stamp in enumerate(times):
            fact = json.loads(original["fact"])
            fact["occurred_at"] = stamp
            # Identical source identity is intentional in these synthetic stored rows;
            # the real source peer is still proved before and after every read.
            db.execute(
                "INSERT INTO actor_episodes VALUES (?,?,1,'active',?,?,?)",
                (
                    f"episode:fixture:{index:04}",
                    "actor:a",
                    f"fixture:{index}",
                    canonical(fact),
                    "2026-10-28T00:00:00Z",
                ),
            )


def test_window_precedes_limit_and_keeps_microseconds_offsets_and_ties(actor):
    seed(
        actor,
        ["2026-10-28T00:00:00Z"] * 270
        + [
            "2026-10-26T08:00:00.000002+08:00",
            "2026-10-26T00:00:00.000001Z",
            "2026-10-26T00:00:00.000002Z",
            "2026-10-27T00:00:00Z",
            "2026-10-25T23:59:59.999999Z",
        ],
    )
    first = timeline(actor, limit=1)
    assert first["items"][0]["ref"]["id"].endswith("0271")
    assert not first["complete"]
    second = timeline(actor, limit=1, cursor=first["next_cursor"])
    third = timeline(actor, limit=1, cursor=second["next_cursor"])
    assert second["items"][0]["ref"]["id"].endswith("0270")
    assert third["items"][0]["ref"]["id"].endswith("0272")
    assert third["complete"] and third["next_cursor"] is None
    assert "learned_at" in third["items"][0]["fact"]


def test_budget_does_not_skip_or_claim_empty_day(actor):
    execute(actor, append(actor))
    first = timeline(actor, budget_bytes=1)
    assert first["items"] == [] and not first["omitted"] and not first["complete"]
    assert first["next_cursor"]
    second = timeline(actor, cursor=first["next_cursor"])
    assert len(second["items"]) == 1 and second["complete"]


def test_page_budget_counts_array_bytes_and_defers_without_omitting(actor):
    seed(actor, ["2026-10-26T01:00:00Z", "2026-10-26T02:00:00Z"])
    expected = timeline(actor)["items"]
    budget = len(canonical(expected[:1]).encode())
    first = timeline(actor, budget_bytes=budget)
    assert first["items"] == expected[:1]
    assert len(canonical(first["items"]).encode()) == budget
    assert not first["complete"] and not first["omitted"]
    second = timeline(actor, budget_bytes=32768, cursor=first["next_cursor"])
    assert first["items"] + second["items"] == expected
    assert second["complete"] and not second["omitted"]


def test_submicrosecond_fraction_uses_same_truncated_key_for_window_and_cursor(actor):
    seed(
        actor,
        [
            "2026-10-26T00:00:00.0000019Z",
            "2026-10-26T08:00:00.00000199+08:00",
            "2026-10-26T00:00:00.0000021Z",
            "2026-10-26T23:59:59.9999999Z",
            "2026-10-25T23:59:59.9999999Z",
            "2026-10-27T00:00:00.0000001Z",
        ],
    )
    cursor, found = None, []
    # Bounded traversal catches a repeated page instead of hanging on it.
    for _ in range(4):
        page = timeline(actor, cursor=cursor, limit=1)
        found.extend(item["ref"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert found == [f"episode:fixture:{index:04}" for index in range(4)]
    assert cursor is None and page["complete"] and not page["omitted"]


def test_permanently_oversized_fact_advances_without_claiming_completeness(actor):
    seed(actor, ["2026-10-26T01:00:00Z", "2026-10-26T02:00:00Z"])
    with actor["store"].transaction(write=True) as db:
        row = db.execute(
            "SELECT fact FROM actor_episodes WHERE id='episode:fixture:0000'"
        ).fetchone()
        fact = json.loads(row[0])
        fact["content_reads"] = [
            dict(
                content_ref=fact["source"],
                coverage=[
                    dict(
                        unit="characters",
                        start=0,
                        end=100,
                        complete=False,
                        gaps=["g" * 128] * 32,
                    )
                ]
                * 10,
            )
        ]
        actor["contracts"].validate("source_fact", fact)
        db.execute(
            "UPDATE actor_episodes SET fact=? WHERE id='episode:fixture:0000'", (canonical(fact),)
        )
    first = timeline(actor, limit=1)
    assert not first["items"] and first["omitted"] and not first["complete"]
    second = timeline(actor, cursor=first["next_cursor"], limit=1)
    assert second["items"][0]["ref"]["id"] == "episode:fixture:0001"


def test_unknown_occurrence_is_not_replaced_with_learning_time(actor):
    actor["owner"].fact["occurred_at"] = None
    execute(actor, append(actor))
    result = timeline(actor)
    assert result["items"] == [] and result["omitted"] and not result["complete"]


def test_cursor_binds_context_window_actor_role_and_domain(actor):
    seed(actor, ["2026-10-26T01:00:00Z", "2026-10-26T02:00:00Z"])
    first = timeline(actor, limit=1)
    for change in (
        {"window": {"start": "2026-10-25T00:00:00Z", "end": "2026-10-27T00:00:00Z"}},
        {"context": {"purpose": "management", "audience": {"kind": "actor_only"}}},
        {"actor_id": "actor:b"},
    ):
        with pytest.raises(Fault, match="invalid_input"):
            timeline(actor, cursor=first["next_cursor"], **change)
    tampered = json.loads(base64.urlsafe_b64decode(first["next_cursor"]))
    tampered["after"][0] = "2099-01-01T00:00:00.000000+00:00"
    with pytest.raises(Fault, match="invalid_input"):
        timeline(actor, cursor=base64.urlsafe_b64encode(canonical(tampered).encode()).decode())
    with actor["store"].transaction(write=True):
        pass
    with pytest.raises(Fault, match="source_changed"):
        timeline(actor, cursor=first["next_cursor"])


def test_public_never_upgrades_actor_only_and_checks_owner_again(actor):
    execute(actor, append(actor))
    context = {"purpose": "diary", "audience": {"kind": "public"}}
    hidden = timeline(actor, context=context)
    assert not hidden["items"] and hidden["omitted"] and not hidden["complete"]
    actor["owner"].context_allowed = False
    with pytest.raises(Fault, match="forbidden"):
        timeline(actor, context=context)


def test_public_fact_still_requires_current_owner_disclosure(actor):
    actor["owner"].fact["source_constraints"] = {"kind": "public"}
    execute(actor, append(actor))
    context = {"purpose": "diary", "audience": {"kind": "public"}}
    assert len(timeline(actor, context=context)["items"]) == 1
    original = actor["owner"].sources

    def deny(*args, **kwargs):
        result = original(*args, **kwargs)
        for proof in result.values():
            proof["disclosure_allowed"] = False
        return result

    actor["owner"].sources = deny
    result = timeline(actor, context=context)
    assert not result["items"] and result["omitted"] and not result["complete"]


def test_source_revocation_during_page_proof_is_rejected(actor):
    execute(actor, append(actor))
    owner = actor["owner"]

    def revoke(facts):
        owner.states[canonical(owner.fact["source"])] = "withdrawn"
        owner.hook = None

    owner.hook = revoke
    with pytest.raises(Fault, match="source_changed"):
        timeline(actor)
