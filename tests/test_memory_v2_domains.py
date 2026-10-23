"""Full owner domains remain fresh without binding unrelated owners' raw captures."""

import copy
import sqlite3

import pytest
from test_group_context import owner
from test_memory_context import ProofIssuer
from test_memory_v2 import candidate, commit, fact, post, query, request, setup
from test_memory_v2_boundaries import attach, source_calls, verify
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical
from tianshu_memory.memory_reindex import MemoryReindexer


def room_setup(sync):
    if sync.scope()["audience"] != "group":
        pytest.skip("Multiple original owners in one authorized group")
    setup(sync)
    return owner(sync), owner(sync)


def named(sync, actor, serial, name="小明"):
    text = f"我叫{name}，我喜欢咖啡。"
    job, _ = candidate(sync, text, serial, actor)
    assert (
        commit(sync, job, text, fact(name, predicate="identity.name"), fact("咖啡"))["state"]
        == "completed"
    )


def constraint(result):
    return result["bundle"]["selection_constraints"][0]


QUERIES = {
    "self": "我喜欢什么咖啡",
    "named": "小明家的咖啡是什么",
    "multiple": "我的咖啡和小明家的咖啡分别是什么",
    "room": "群里谁喜欢咖啡",
}


@pytest.mark.parametrize("mode", list(QUERIES))
def test_only_actual_owner_domain_changes_on_unrelated_raw_capture(sync, mode):
    other, unrelated = room_setup(sync)
    named(sync, 0, 1, "阿林")
    named(sync, other, 2)
    selected = query(sync, QUERIES[mode])
    assert constraint(selected)["owner_resolution"]["mode"] == mode
    assert constraint(selected)["candidate_count"] == (6 if mode in {"multiple", "room"} else 3)
    # The first pending read captures source text before any typed proposal commits.
    candidate(sync, "我今天带了雨伞。", 3, unrelated)
    sync.calls.clear()
    response = verify(sync, selected["bundle"])
    assert response.status_code == (409 if mode == "room" else 200), response.text
    assert source_calls(sync) == ["snapshot", "current", "head"]
    fresh = query(sync, QUERIES[mode])
    if mode == "room":
        assert constraint(fresh)["candidate_count"] == 7
        assert (
            constraint(fresh)["candidate_domain_hash"]
            != constraint(selected)["candidate_domain_hash"]
        )
    else:
        assert constraint(fresh) == constraint(selected)
        assert fresh["evidence"] == selected["evidence"]
        assert response.json()["bundle"]["bundle_id"] == selected["bundle"]["bundle_id"]


def test_self_negative_survives_other_owner_capture_but_not_own_new_source(sync):
    other, _ = room_setup(sync)
    selected = query(sync, QUERIES["self"])
    assert selected["evidence"] == []
    assert constraint(selected)["candidate_count"] == 0
    assert constraint(selected)["result_status"] == "no_match"
    candidate(sync, "我喜欢咖啡。", 1, other)
    assert verify(sync, selected["bundle"]).status_code == 200
    assert constraint(query(sync, QUERIES["self"])) == constraint(selected)
    candidate(sync, "不是原来那样。", 2)
    assert verify(sync, selected["bundle"]).status_code == 409


def test_each_subquery_binds_its_own_complete_domain(sync):
    other, unrelated = room_setup(sync)
    named(sync, 0, 1, "阿林")
    named(sync, other, 2)
    payload = request(sync, QUERIES["self"])
    payload["queries"].append(dict(payload["queries"][0], query_id="q2", text=QUERIES["named"]))
    response = post(sync, "query", payload)
    assert response.status_code == 200, response.text
    selected = response.json()
    constraints = selected["bundle"]["selection_constraints"]
    assert [item["candidate_count"] for item in constraints] == [3, 3]
    assert constraints[0]["candidate_domain_hash"] != constraints[1]["candidate_domain_hash"]
    candidate(sync, "今天带了雨伞。", 3, unrelated)
    assert verify(sync, selected["bundle"]).status_code == 200
    candidate(sync, "不是原来那样。", 4, other)
    assert verify(sync, selected["bundle"]).status_code == 409


@pytest.mark.parametrize("kind", ["embedding", "rerank"])
@pytest.mark.parametrize("change", ["other", "same", "duplicate_name", "withdraw"])
def test_external_ranking_rechecks_same_owner_domain_and_full_name_namespace(sync, kind, change):
    other, unrelated = room_setup(sync)
    named(sync, 0, 1, "阿林")
    named(sync, other, 2)
    original_grant = sync.grants[-2]  # requester source, before the named owner's source
    backend = attach(sync, "hybrid" if kind == "embedding" else "keyword", kind == "rerank")
    if kind == "embedding":
        assert MemoryReindexer(sync.service).index_step(batch_size=8)["indexed"] == 6

    def change_during_ranking():
        if change == "withdraw":
            original_grant["state"] = "denied"
            sync.platform_head["sequence"] += 1
        elif change == "duplicate_name":
            named(sync, unrelated, 3)
        else:
            candidate(sync, "不是原来那样。", 3, unrelated if change == "other" else 0)

    backend.hook = change_during_ranking
    backend.calls.clear()
    selected = post(
        sync, "query", request(sync, QUERIES["named" if change == "duplicate_name" else "self"])
    )
    assert any(
        operation == ("embed" if kind == "embedding" else "rerank")
        for operation, _ in backend.calls
    )
    assert selected.status_code == (200 if change == "other" else 409), selected.text
    if change == "other":
        assert constraint(selected.json())["candidate_count"] == 3
        assert verify(sync, selected.json()["bundle"]).status_code == 200


@pytest.mark.parametrize("change", ["duplicate", "rename", "withdraw", "unknown_known"])
def test_named_resolution_always_rechecks_the_complete_authorized_namespace(sync, change):
    other, unrelated = room_setup(sync)
    if change != "unknown_known":
        named(sync, other, 1)
    selected = query(sync, QUERIES["named"])
    if change == "unknown_known":
        assert constraint(selected)["owner_resolution"]["mode"] == "unresolved"
        assert constraint(selected)["candidate_count"] == 0
        named(sync, other, 2)
    elif change == "duplicate":
        named(sync, unrelated, 2)
    elif change == "withdraw":
        sync.grants[-1]["state"] = "denied"
        sync.platform_head["sequence"] += 1
    else:
        text = "我改名叫小海。"
        job, pending = candidate(sync, text, 2, other)
        target = next(
            item for item in pending["known_facts"] if item["fact"]["predicate"] == "identity.name"
        )
        assert (
            commit(
                sync,
                job,
                text,
                fact(
                    "小海",
                    predicate="identity.name",
                    operation="replace",
                    targets=target["target_refs"],
                ),
            )["state"]
            == "completed"
        )
    assert verify(sync, selected["bundle"]).status_code == 409


@pytest.mark.parametrize("change", ["raw", "replace", "withdraw"])
def test_selected_owner_changes_still_invalidate_fresh_bundle(sync, change):
    setup(sync)
    text = "我喜欢咖啡。"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact("咖啡"))["state"] == "completed"
    selected = query(sync, QUERIES["self"])
    if change == "withdraw":
        sync.grants[-1]["state"] = "denied"
        sync.platform_head["sequence"] += 1
    else:
        text = "不是原来那样。" if change == "raw" else "我现在改成喜欢红茶。"
        job, pending = candidate(sync, text, 2)
        if change == "replace":
            assert (
                commit(
                    sync,
                    job,
                    text,
                    fact(
                        "红茶",
                        operation="replace",
                        targets=pending["known_facts"][0]["target_refs"],
                    ),
                )["state"]
                == "completed"
            )
    assert verify(sync, selected["bundle"]).status_code == 409


def private_conversation(sync, conversation, *, same_person):
    index = owner(sync, audience="self_private", conversation=conversation)
    if same_person:
        # Another original conversation for the same authenticated synthetic account.
        sync.physicals[index]["author"] = copy.deepcopy(sync.account)
        sync.grants[index]["account"] = copy.deepcopy(sync.account)
        sync.admissions[index]["scope"]["person_id"] = sync.person
        sync.contexts[f"synthetic-viewer:{index}"]["verified_account"] = copy.deepcopy(sync.account)
        for scope in (sync.turns[index]["scope"], sync.turns[index]["committed_event"]["scope"]):
            scope["person_id"] = sync.person
        sync.config["callers"]["companion"]["event_scopes"][-1] = sync.scope(index)
        sync.save()
    return index


@pytest.mark.parametrize("mode", ["self", "named"])
def test_private_association_keeps_approved_scopes_and_excludes_unlinked_same_person(sync, mode):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Explicit private association only")
    setup(sync)
    linked = private_conversation(sync, "private:linked", same_person=mode == "self")
    # An unlinked private conversation of that exact person must stay outside the domain.
    hidden = private_conversation(sync, "private:hidden", same_person=True)
    if mode == "named":
        account = sync.physicals[linked]["author"]
        person = sync.scope(linked)["person_id"]
        sync.physicals[hidden]["author"] = copy.deepcopy(account)
        sync.grants[hidden]["account"] = copy.deepcopy(account)
        sync.admissions[hidden]["scope"]["person_id"] = person
        sync.contexts[f"synthetic-viewer:{hidden}"]["verified_account"] = copy.deepcopy(account)
        sync.turns[hidden]["scope"]["person_id"] = person
        sync.turns[hidden]["committed_event"]["scope"]["person_id"] = person
        sync.config["callers"]["companion"]["event_scopes"][-1] = sync.scope(hidden)
        sync.save()
    named(sync, linked, 1)
    named(sync, hidden, 2)
    sync.config["callers"]["companion"]["operations"].append("context_association")
    sync.save()
    proof = ProofIssuer()
    sync.service.memory_context.proofs = proof
    link = dict(
        command=sync.command(),
        action="link",
        source_account=sync.account,
        target_account=sync.physicals[linked]["author"],
        association_id=None,
        expected_version=0,
        scopes=[sync.scope(), sync.scope(linked)],
        proof_ref="domain:association",
    )
    proof.issue(
        link, "association", [link["source_account"], link["target_account"]], link["scopes"]
    )
    response = sync.post("memory/context/association", link)
    assert response.status_code == 200, response.text
    association = response.json()
    payload = request(sync, QUERIES[mode])
    payload["include_associated"] = True
    selected = post(sync, "query", payload)
    assert selected.status_code == 200, selected.text
    selected = selected.json()
    assert selected["evidence"]
    assert {canonical(item["owner_scope"]) for item in selected["evidence"]} == {
        canonical(sync.scope(linked))
    }
    assert constraint(selected)["candidate_count"] == 3
    candidate(sync, "不是原来那样。", 3, hidden)
    assert verify(sync, selected["bundle"]).status_code == 200
    revoke = dict(
        command=sync.command(),
        action="revoke",
        source_account=sync.account,
        target_account=None,
        association_id=association["association_id"],
        expected_version=1,
        scopes=[],
        proof_ref=None,
    )
    assert sync.post("memory/context/association", revoke).status_code == 200
    assert verify(sync, selected["bundle"]).status_code == 409


@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_sqlite_rank_releases_its_corpus_after_error_or_cancel(h, monkeypatch, error):
    from tianshu_memory import recall_index

    h.seed()
    with h.store.transaction() as db:
        groups = db.execute(
            "SELECT * FROM groups WHERE scope=?", (canonical(h.private),)
        ).fetchall()
        eligible = recall_index.eligible_groups(h.service, db, groups, h.private)

        def fail_document(_text):
            raise error("synthetic interrupted corpus preparation")

        with monkeypatch.context() as patch:
            patch.setattr(recall_index, "normalize_objects", fail_document)
            with pytest.raises(error):
                recall_index.rank_groups(h.service, db, eligible, ["咖啡"], "咖啡")
        assert (
            db.execute(
                "SELECT count(*) FROM sqlite_temp_master WHERE name LIKE 'allowed_search%'"
            ).fetchone()[0]
            == 0
        )
        assert recall_index.rank_groups(h.service, db, eligible, ["咖啡"], "咖啡")
        # A failed CREATE never grants this invocation ownership of an existing table.
        db.execute("CREATE TEMP TABLE allowed_search (sentinel TEXT)")
        db.execute("INSERT INTO temp.allowed_search VALUES ('preserve')")
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            recall_index.rank_groups(h.service, db, eligible, ["咖啡"], "咖啡")
        assert db.execute("SELECT sentinel FROM temp.allowed_search").fetchone()[0] == "preserve"
