"""Actor lifecycle ownership, body erasure, projection and explicit upgrade."""

import copy
import json
from datetime import timedelta

import pytest
from test_actor_memories import check, derive, query
from test_actor_memory import actor as actor
from test_actor_memory import actor_storage as actor_storage
from test_actor_memory import append, execute, request

from tianshu_memory import source_recovery
from tianshu_memory.actor_memory.facts import INTERNAL
from tianshu_memory.actor_memory.store import Store, checkpoint
from tianshu_memory.domain import Fault, canonical


def interest(actor):
    return execute(
        actor, request("interests", context=copy.deepcopy(INTERNAL), limit=32, budget_bytes=65536)
    )


def listing(actor, **kwargs):
    values = dict(
        context=copy.deepcopy(INTERNAL),
        states=["active", "superseded", "withdrawn", "expired", "unavailable"],
        kinds=["episode", "preference", "understanding", "reflection", "autobiography"],
        cursor=None,
        limit=32,
        budget_bytes=65536,
    )
    values.update(kwargs)
    return execute(actor, request("manage_list", **values))


def impact(actor, ref):
    return execute(actor, request("memory_impact", context=copy.deepcopy(INTERNAL), ref=ref))


def forget(actor, ref):
    preview = impact(actor, ref)
    return execute(
        actor,
        request(
            "forget_memory",
            context=copy.deepcopy(INTERNAL),
            ref=preview["ref"],
            expected_domain_version=preview["domain_version"],
            idempotency_key="forget:" + ref["id"],
        ),
    )


def test_long_interest_projection_correction_and_history(actor):
    episode = execute(actor, append(actor))
    first = execute(
        actor,
        derive(
            episode,
            kind="preference",
            retention="long",
            interest=dict(topic="music", stance="like"),
        ),
    )
    assert interest(actor)["items"][0]["support_count"] == 1
    # A different formulation over the same actual encounter is not new support.
    execute(
        actor,
        derive(
            episode,
            kind="preference",
            retention="long",
            text="I like these reading pages",
            interest=dict(topic="music", stance="like"),
            idempotency_key="another",
        ),
    )
    assert interest(actor)["items"][0]["support_count"] == 1
    preview = impact(actor, first["ref"])
    revised = request(
        "revise_memory",
        context=copy.deepcopy(INTERNAL),
        idempotency_key="edit",
        ref=first["ref"],
        expected_domain_version=preview["domain_version"],
        text="I now dislike this music.",
        importance="high",
        retention="long",
        interest=dict(topic="music", stance="dislike"),
    )
    old_clock = actor["app"].clock
    actor["app"].clock = lambda: old_clock() + timedelta(days=1)
    receipt = execute(actor, revised)
    assert execute(actor, revised) == receipt
    projected = interest(actor)["items"][0]
    assert projected["stance"] == "dislike"
    assert any(item["state"] == "superseded" for item in projected["history"])
    assert projected["ref"] == receipt["ref"]
    assert listing(actor)["items"]


def test_legacy_preference_remains_unspecified(actor):
    execute(actor, derive(execute(actor, append(actor)), kind="preference"))
    item = interest(actor)["items"][0]
    assert item["topic"] is None and item["stance"] == "unspecified"


@pytest.mark.parametrize("kind", ["episode", "derived"])
def test_forget_erases_body_and_index_preserves_receipts_and_suppresses_rewording(actor, kind):
    episode = execute(actor, append(actor))
    original = derive(episode, text="private marker velvet piano")
    derived = execute(actor, original)
    ref = (
        derived["ref"]
        if kind == "derived"
        else dict(kind="episode", id=episode["episode_id"], revision=1)
    )
    receipt = forget(actor, ref)
    assert receipt["state"] == "withdrawn"
    assert check(actor, derived["ref"])["items"][0]["state"] == "withdrawn"
    assert execute(actor, original) == derived
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, derive(episode, text="reworded late write", idempotency_key="late"))
    with actor["store"].transaction() as db:
        assert (
            db.execute(
                "SELECT payload FROM actor_derived WHERE id=?", (derived["ref"]["id"],)
            ).fetchone()[0]
            == "{}"
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM actor_search_terms WHERE memory_id=?", (derived["ref"]["id"],)
            ).fetchone()[0]
            == 0
        )
        if kind == "episode":
            assert (
                db.execute("SELECT fact FROM actor_episodes WHERE id=?", (ref["id"],)).fetchone()[0]
                == "{}"
            )
    tombstone = next(i for i in listing(actor)["items"] if i["ref"]["id"] == ref["id"])
    assert tombstone["forgotten"] and tombstone["text"] is None and tombstone["episode_refs"] == []


def test_forget_subset_cannot_resurrect_but_new_actual_source_can(actor):
    first = execute(actor, append(actor))
    actor["owner"].fact["source"]["object_id"] = "reading:two"
    second = execute(actor, {**append(actor), "idempotency_key": "second"})
    payload = derive(
        first,
        episode_refs=[
            dict(episode_id=e["episode_id"], expected_revision=1) for e in (first, second)
        ],
    )
    derived = execute(actor, payload)
    forget(actor, derived["ref"])
    with pytest.raises(Fault, match="source_withdrawn"):
        execute(actor, derive(first, text="subset rewrite", idempotency_key="subset"))
    actor["owner"].fact["source"]["object_id"] = "reading:three"
    third = execute(actor, {**append(actor), "idempotency_key": "third"})
    assert (
        execute(actor, derive(third, text="new experience", idempotency_key="new"))["state"]
        == "accepted"
    )


def test_maintenance_expires_body_and_has_original_receipt(actor):
    episode = execute(actor, append(actor))
    derived = execute(actor, derive(episode))
    clock = actor["app"].clock
    actor["app"].clock = lambda: clock() + timedelta(days=8)
    command = request("maintain", idempotency_key="maintenance", limit=1)
    result = execute(actor, command)
    assert result["expired_count"] == 1
    assert execute(actor, command) == result
    assert (
        execute(actor, request("get_receipt", command="maintain", idempotency_key="maintenance"))
        == result
    )
    row = next(i for i in listing(actor)["items"] if i["ref"]["id"] == derived["ref"]["id"])
    assert row["state"] == "expired" and row["text"] is None


def test_list_pagination_actor_bound_cursor_and_changed_domain(actor):
    episode = execute(actor, append(actor))
    execute(actor, derive(episode))
    page = listing(actor, limit=1)
    assert page["next_cursor"]
    tail = listing(actor, limit=1, cursor=page["next_cursor"])
    assert tail["items"][0]["ref"] != page["items"][0]["ref"]
    with pytest.raises(Fault, match="invalid_input"):
        execute(
            actor,
            request(
                "manage_list",
                actor_id="actor:b",
                context=copy.deepcopy(INTERNAL),
                states=["active"],
                kinds=["episode"],
                cursor=page["next_cursor"],
                limit=1,
                budget_bytes=65536,
            ),
        )
    execute(actor, derive(episode, idempotency_key="new", text="second understanding"))
    with pytest.raises(Fault, match="source_changed"):
        listing(actor, limit=1, cursor=page["next_cursor"])


def test_old_relevant_episode_not_lost_behind_256_new_unrelated_records(actor):
    first = execute(actor, append(actor))
    with actor["store"].transaction(write=True) as db:
        original = dict(
            db.execute("SELECT * FROM actor_episodes WHERE id=?", (first["episode_id"],)).fetchone()
        )
        for i in range(270):
            fact = json.loads(original["fact"])
            fact["summary"] = "Unrelated theatre performance"
            db.execute(
                "INSERT INTO actor_episodes VALUES (?,?,1,'active',?,?,?)",
                (
                    f"episode:extra{i}",
                    "actor:a",
                    f"synthetic-identity-{i}",
                    canonical(fact),
                    "2026-10-27T00:00:00Z",
                ),
            )
    assert query(actor, text="reading pages")["items"][0]["ref"]["id"] == first["episode_id"]


def test_explicit_schema2_upgrade_backup_and_rebuild(actor):
    execute(actor, append(actor))
    store = actor["store"]
    with store.transaction() as db:
        db.execute("DROP TABLE actor_search_terms")
        db.execute("UPDATE actor_metadata SET value='2' WHERE key='schema'")
        source_recovery.persist(store.guard, checkpoint(db))
    store.close()
    with pytest.raises(Fault, match="dependency_unavailable"):
        Store(actor["config"])
    backup = actor["tmp"] / "schema2-backup.json"
    result = Store.initialize(actor["config"], backup)
    assert result["actor_memory_schema"] == 3
    snapshot = json.loads(backup.read_text())
    assert snapshot["guard"]["schema"] == 2 and len(snapshot["tables"]) == 7
    reopened = Store(actor["config"])
    actor["store"] = actor["app"].store = reopened
    with reopened.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM actor_search_terms").fetchone()[0] > 0


def test_source_withdrawal_clears_derived_and_cannot_feed_interest(actor):
    episode = execute(actor, append(actor))
    derived = execute(
        actor, derive(episode, kind="preference", interest=dict(topic="music", stance="like"))
    )
    actor["owner"].states[canonical(actor["owner"].fact["source"])] = "withdrawn"
    assert interest(actor)["items"] == []
    with actor["store"].transaction() as db:
        assert (
            db.execute(
                "SELECT payload FROM actor_derived WHERE id=?", (derived["ref"]["id"],)
            ).fetchone()[0]
            == "{}"
        )


def test_forget_corrected_memory_erases_history_as_well(actor):
    episode = execute(actor, append(actor))
    original = execute(actor, derive(episode, text="obsolete secret"))
    preview = impact(actor, original["ref"])
    revised = execute(
        actor,
        request(
            "revise_memory",
            context=copy.deepcopy(INTERNAL),
            idempotency_key="edit-history",
            ref=original["ref"],
            expected_domain_version=preview["domain_version"],
            text="corrected secret",
            importance="normal",
            retention="long",
        ),
    )
    preview = impact(actor, revised["ref"])
    assert any(r["id"] == original["ref"]["id"] for r in preview["dependent_refs"])
    forget(actor, revised["ref"])
    with actor["store"].transaction() as db:
        assert all(row[0] == "{}" for row in db.execute("SELECT payload FROM actor_derived"))


def test_private_interest_and_history_cannot_be_disclosed_to_group(actor):
    episode = execute(actor, append(actor))
    execute(
        actor,
        derive(episode, kind="preference", interest=dict(topic="private music", stance="like")),
    )
    from test_actor_memories import query as original_query

    private = interest(actor)
    assert private["items"]
    group = dict(
        purpose="dialogue",
        audience=dict(kind="group", conversation_id="conv:group", assertion_ref="origin:group"),
    )
    assert (
        execute(actor, request("interests", context=group, limit=32, budget_bytes=65536))["items"]
        == []
    )
    assert original_query(actor, actor_id="actor:b")["items"] == []
    assert (
        execute(
            actor,
            request(
                "interests",
                actor_id="actor:b",
                context=copy.deepcopy(INTERNAL),
                limit=32,
                budget_bytes=65536,
            ),
        )["items"]
        == []
    )


def test_idle_maintenance_preserves_domain_and_existing_cursor(actor):
    episode = execute(actor, append(actor))
    execute(actor, derive(episode))
    page = listing(actor, limit=1)
    result = execute(actor, request("maintain", idempotency_key="idle", limit=1))
    assert result["domain_version"] == page["domain_version"]
    assert not result["expired_count"] and not result["compressed_count"]
    assert listing(actor, limit=1, cursor=page["next_cursor"])["items"]


def test_fictional_preference_cannot_gain_explicit_interest_tag(actor):
    actor["owner"].fact["provenance"] = "fictional"
    episode = execute(actor, append(actor))
    with pytest.raises(Fault, match="invalid_input"):
        execute(
            actor, derive(episode, kind="preference", interest=dict(topic="fiction", stance="like"))
        )
    execute(actor, derive(episode, kind="preference"))
    assert interest(actor)["items"][0]["support_count"] == 0


def test_maintenance_compacts_duplicate_payload_without_creating_evidence(actor):
    episode = execute(actor, append(actor))
    derived = execute(actor, derive(episode))
    with actor["store"].transaction(write=True) as db:
        row = dict(
            db.execute("SELECT * FROM actor_derived WHERE id=?", (derived["ref"]["id"],)).fetchone()
        )
        db.execute(
            "INSERT INTO actor_derived VALUES (?,?,1,'active',?,?,?,?)",
            (
                "derived:redundant",
                "actor:a",
                "fixture-redundancy",
                row["payload"],
                row["recorded_at"],
                row["expires_at"],
            ),
        )
        db.execute(
            "INSERT INTO actor_derived_basis VALUES (?,?,?,?)",
            ("derived:redundant", "actor:a", episode["episode_id"], 1),
        )
    result = execute(actor, request("maintain", idempotency_key="compact", limit=128))
    assert result["compressed_count"] == 1
    with actor["store"].transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM actor_derived WHERE state='active'").fetchone()[0] == 1
        )


def test_failed_upgrade_preserves_schema2_guard_and_exclusive_backup(actor, monkeypatch):
    store = actor["store"]
    with store.transaction() as db:
        db.execute("DROP TABLE actor_search_terms")
        db.execute("UPDATE actor_metadata SET value='2' WHERE key='schema'")
        source_recovery.persist(store.guard, checkpoint(db))
    store.close()
    backup = actor["tmp"] / "failed-upgrade.json"
    original = source_recovery.persist

    def fail_schema3(path, value, **kwargs):
        if value.get("domain") == "actor-memory" and value.get("schema") == 3:
            raise OSError("simulated checkpoint write failure")
        return original(path, value, **kwargs)

    monkeypatch.setattr(source_recovery, "persist", fail_schema3)
    with pytest.raises(OSError, match="simulated"):
        Store.initialize(actor["config"], backup)
    legacy = Store(actor["config"], allow_legacy=True)
    with legacy.transaction() as db:
        assert checkpoint(db)["schema"] == 2
    legacy.close()
    monkeypatch.setattr(source_recovery, "persist", original)
    with pytest.raises(FileExistsError):
        Store.initialize(actor["config"], backup)
    Store.initialize(actor["config"], actor["tmp"] / "retry-upgrade.json")
    actor["store"] = actor["app"].store = Store(actor["config"])


def test_repeated_read_of_same_original_does_not_increase_interest_support(actor):
    content = dict(
        producer="knowledge", object_id="content:one", revision="1", content_hash="b" * 64
    )
    actor["owner"].fact["content_reads"] = [
        dict(
            content_ref=content,
            coverage=[dict(unit="pages", start=1, end=2, complete=False, gaps=[])],
        )
    ]
    first = execute(actor, append(actor))
    execute(actor, derive(first, kind="preference", interest=dict(topic="music", stance="like")))
    actor["owner"].fact["source"]["object_id"] = "reading:repeat"
    second = execute(actor, {**append(actor), "idempotency_key": "repeat-reading"})
    execute(
        actor,
        derive(
            second,
            kind="preference",
            interest=dict(topic="music", stance="like"),
            idempotency_key="repeat-preference",
        ),
    )
    assert interest(actor)["items"][0]["support_count"] == 1


def test_query_filters_private_candidates_before_bounded_index_limit(actor):
    from tianshu_memory.actor_memory import search

    actor["owner"].fact["source_constraints"] = {"kind": "public"}
    first = execute(actor, append(actor))
    with actor["store"].transaction(write=True) as db:
        original = dict(
            db.execute("SELECT * FROM actor_episodes WHERE id=?", (first["episode_id"],)).fetchone()
        )
        for i in range(270):
            fact = json.loads(original["fact"])
            fact["source_constraints"] = {"kind": "actor_only"}
            key = f"episode:private{i}"
            db.execute(
                "INSERT INTO actor_episodes VALUES (?,?,1,'active',?,?,?)",
                (key, "actor:a", f"private-identity-{i}", canonical(fact), "2026-10-27T00:00:00Z"),
            )
            search.index(db, "actor:a", "episode", key, fact["summary"])
    group = dict(
        purpose="dialogue",
        audience=dict(kind="group", conversation_id="conv:group", assertion_ref="origin:group"),
    )
    result = query(actor, context=group)
    assert [i["ref"]["id"] for i in result["items"]] == [first["episode_id"]]
    internal = query(actor)
    assert internal["omitted"] and len(internal["items"]) <= 32


def test_management_http_uses_existing_caller_and_actor_boundary(actor, contracts):
    from fastapi.testclient import TestClient
    from test_actor_memory import http_app

    execute(actor, append(actor))
    app, _ = http_app(actor, contracts)
    payload = request(
        "manage_list",
        context=copy.deepcopy(INTERNAL),
        states=["active"],
        kinds=["episode"],
        cursor=None,
        limit=32,
        budget_bytes=32768,
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-actor-token"}
        response = client.post(
            "/internal/v1/actor-self-memory/operate", json=payload, headers=headers
        )
        assert response.status_code == 200 and response.json()["items"]
        assert (
            client.post(
                "/internal/v1/actor-self-memory/operate",
                json={**payload, "actor_id": "actor:b"},
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            client.post("/internal/v1/actor-self-memory/operate", json=payload).status_code == 401
        )


def test_long_lexical_token_has_bounded_index_key(actor):
    actor["owner"].fact["summary"] = "a" * 4000
    first = execute(actor, append(actor))
    with actor["store"].transaction() as db:
        tokens = [
            row[0]
            for row in db.execute(
                "SELECT term FROM actor_search_terms WHERE memory_id=?", (first["episode_id"],)
            )
        ]
    assert tokens and all(len(token) == 64 for token in tokens)


def test_interest_scope_precedes_topic_window(actor):
    actor["owner"].fact["source_constraints"] = {"kind": "public"}
    public = execute(actor, append(actor))
    visible = execute(
        actor,
        derive(
            public,
            kind="preference",
            retention="long",
            interest=dict(topic="visible old interest", stance="like"),
        ),
    )
    actor["owner"].fact["source"]["object_id"] = "reading:private"
    actor["owner"].fact["source_constraints"] = {"kind": "actor_only"}
    private = execute(actor, {**append(actor), "idempotency_key": "private-episode"})
    with actor["store"].transaction(write=True) as db:
        for index in range(270):
            key = f"derived:private-topic-{index}"
            command = derive(
                private,
                kind="preference",
                retention="long",
                interest=dict(topic=f"private topic {index}", stance="like"),
            )
            payload = {
                k: command[k]
                for k in (
                    "kind",
                    "text",
                    "episode_refs",
                    "replaces",
                    "importance",
                    "retention",
                    "interest",
                )
            }
            db.execute(
                "INSERT INTO actor_derived VALUES (?,?,1,'active',?,?,?,?)",
                (
                    key,
                    "actor:a",
                    key,
                    canonical(payload),
                    "2026-10-27T00:00:00Z",
                    "2027-04-01T00:00:00Z",
                ),
            )
            db.execute(
                "INSERT INTO actor_derived_basis VALUES (?,?,?,?)",
                (key, "actor:a", private["episode_id"], 1),
            )
    group = dict(
        purpose="dialogue",
        audience=dict(kind="group", conversation_id="conv:group", assertion_ref="origin:group"),
    )
    projected = execute(actor, request("interests", context=group, limit=32, budget_bytes=65536))
    assert [item["ref"] for item in projected["items"]] == [visible["ref"]]
    assert not projected["omitted"]
