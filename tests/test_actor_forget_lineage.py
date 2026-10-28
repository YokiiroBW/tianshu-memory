"""Forgetting stays closed across expired replacements and merged old evidence."""

from datetime import timedelta

import pytest
from test_actor_lifecycle import forget
from test_actor_memories import derive
from test_actor_memory import actor as actor
from test_actor_memory import actor_storage as actor_storage
from test_actor_memory import append, execute, request

from tianshu_memory.actor_memory.store import Store
from tianshu_memory.domain import Fault


def test_forget_crosses_an_expired_middle_replacement(actor):
    episode = execute(actor, append(actor))
    first = execute(actor, derive(episode, text="Original retained thought", retention="long"))
    initial = actor["app"].clock()
    actor["app"].clock = lambda: initial + timedelta(days=1)
    middle = execute(
        actor,
        derive(
            episode,
            idempotency_key="middle",
            text="Short lived correction",
            retention="short",
            replaces=[first["ref"]],
        ),
    )
    actor["app"].clock = lambda: initial + timedelta(days=2)
    latest = execute(
        actor,
        derive(
            episode,
            idempotency_key="latest",
            text="Current retained thought",
            retention="long",
            replaces=[middle["ref"]],
        ),
    )
    actor["app"].clock = lambda: initial + timedelta(days=10)
    execute(actor, request("maintain", idempotency_key="expire-middle", limit=128))
    actor["store"].close()
    actor["store"] = actor["app"].store = Store(actor["config"])
    forget(actor, latest["ref"])
    with actor["store"].transaction() as db:
        for ref in (first["ref"], middle["ref"], latest["ref"]):
            assert (
                db.execute("SELECT payload FROM actor_derived WHERE id=?", (ref["id"],)).fetchone()[
                    0
                ]
                == "{}"
            )
        retained = " ".join(
            row[0]
            for row in db.execute("SELECT value FROM actor_metadata WHERE key LIKE 'replacement:%'")
        )
        assert middle["ref"]["id"] in retained and first["ref"]["id"] in retained
        assert (
            "Short lived correction" not in retained and "Original retained thought" not in retained
        )


def test_combining_separately_forgotten_bases_is_not_new_evidence(actor):
    first = execute(actor, append(actor))
    old_first = execute(actor, derive(first, text="First old thought"))
    forget(actor, old_first["ref"])
    actor["owner"].fact["source"]["object_id"] = "reading:second-old-source"
    second = execute(actor, {**append(actor), "idempotency_key": "second-episode"})
    old_second = execute(
        actor, derive(second, text="Second old thought", idempotency_key="second-thought")
    )
    forget(actor, old_second["ref"])
    combined = derive(
        first,
        text="Reworded combination of two forgotten thoughts",
        idempotency_key="combined",
        episode_refs=[
            dict(episode_id=e["episode_id"], expected_revision=e["revision"])
            for e in (first, second)
        ],
    )
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, combined)
    actor["owner"].fact["source"]["object_id"] = "reading:genuinely-new-source"
    fresh = execute(actor, {**append(actor), "idempotency_key": "new-episode"})
    new_refs = combined["episode_refs"] + [
        dict(episode_id=fresh["episode_id"], expected_revision=fresh["revision"])
    ]
    assert (
        execute(
            actor, {**combined, "idempotency_key": "with-new-evidence", "episode_refs": new_refs}
        )["state"]
        == "accepted"
    )
