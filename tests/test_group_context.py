"""Cold group recall over real guarded Memory and synthetic HTTPS source owners."""

import copy
import json
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from source_sync_harness import SyncHarness
from test_automatic_facts import add, initialize
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_storage_backends import postgres_store

from tianshu_memory.domain import canonical, utc
from tianshu_memory.postgres import is_postgres
from tianshu_memory.recall_language import self_reference


@pytest.fixture(params=["sqlite", "postgresql"])
def room(tmp_path, source_contracts, source_examples, certificates, monkeypatch, request):
    h = SyncHarness(tmp_path, source_contracts, source_examples, certificates, "group")
    initialize(h)
    h.config["callers"]["companion"]["operations"].append("context_query")
    with ExitStack() as stack:
        if request.param == "postgresql":
            h.store.close()
            _, settings = stack.enter_context(postgres_store(h.store.path, settings=h.config))
            h.config["storage_backends"] = settings["storage_backends"]
        stack.enter_context(h.owners())
        stack.enter_context(h.runtime(monkeypatch))
        h.store = h.service.store
        yield h


def owner(h, *, audience="group", conversation=None):
    """A new real binding and original admission, not a requester impersonation."""
    index = len(h.admissions)
    # add() appends sources too, so actor indices deliberately name their matching turn.
    while len(h.turns) < index:
        h.turns.append(copy.deepcopy(h.turns[0]))
    while len(h.physicals) < index:
        unused = copy.deepcopy(h.physicals[0])
        unused["key"]["message_id"] = f"unused-owner-slot:{len(h.physicals)}"
        h.physicals.append(unused)
    physical, admission, grant = [
        copy.deepcopy(items[0]) for items in (h.physicals, h.admissions, h.grants)
    ]
    account = dict(physical["author"], immutable_account_id=f"group-reader:{index}")
    physical.update(
        author=account,
        key=dict(physical["key"], message_id=f"owner:{index}"),
        physical_receipt_id=f"owner-physical:{index}",
        audience=audience,
        conversation_id=conversation or h.scope()["conversation_id"],
    )
    admission["scope"].update(
        audience=audience, conversation_id=physical["conversation_id"], person_id=None
    )
    admission["selector"]["key"] = physical["key"]
    admission["source"]["message_key"] = dict(physical["key"], revision=1)
    admission["source"]["receipt_id"] = f"owner-admission:{index}"
    admission["physical_receipt_id"] = physical["physical_receipt_id"]
    grant.update(account=account, selector=admission["selector"], entry_id=f"owner-entry:{index}")
    context = copy.deepcopy(h.contexts["synthetic-viewer:0"])
    context.update(
        assertion_ref=f"synthetic-viewer:{index}",
        verified_account=account,
        allowed_scope=admission["scope"],
    )
    h.contexts[context["assertion_ref"]] = context
    h.physicals.append(physical)
    h.admissions.append(admission)
    h.grants.append(grant)
    response = h.post("identity/register", dict(command=h.command(index), account=account))
    assert response.status_code == 200, response.text
    admission["scope"]["person_id"] = response.json()["person_id"]
    turn = copy.deepcopy(h.turns[0])
    turn = json.loads(canonical(turn).replace(turn["turn_id"], f"owner-turn:{index}"))
    turn.update(scope=copy.deepcopy(admission["scope"]), input_sources=[admission["source"]])
    turn["committed_event"].update(
        scope=copy.deepcopy(admission["scope"]),
        conversation_id=physical["conversation_id"],
        event_id=f"owner-event:{index}",
        sources=[admission["source"]],
    )
    if len(h.turns) == index:
        h.turns.append(turn)
    else:
        h.turns[index] = turn
    h.config["callers"]["companion"]["event_scopes"].append(h.scope(index))
    h.save()
    h.core_head["sequence"] += 1
    h.platform_head["sequence"] += 1
    return index


def query(h, text="群里那只猫叫什么", **kwargs):
    request = dict(
        h.selection(budget=100000),
        query_text=text,
        selection=["evidence", "identity"],
        include_associated=False,
        time_range=None,
        limit=32,
        known_association_version=None,
        known_scope_checks=None,
    )
    request.update(kwargs)
    return request


def recalled(h, request=None):
    response = h.post("memory/context/query", request or query(h))
    assert response.status_code == 200, response.text
    return response.json()


def probe(h, result):
    return query(
        h,
        budget={"tokens": 0, "bytes": 0},
        known_scope_version=result["scope_version"],
        known_association_version=result["association_version"],
        known_scope_checks=result["scope_checks"],
    )


def history(h, selected=None, known=None):
    return dict(
        query=query(h)["query"],
        requested_scope=h.scope(),
        include_associated=False,
        expected_snapshot=None
        if selected is None
        else {
            key: selected[key] for key in ("scope_version", "association_version", "scope_checks")
        },
        known_snapshot=known,
    )


def cat(h, actor, name="栗团", second=1, **kwargs):
    return add(h, "我的猫叫" + name, "pet.cat.name", name, actor=actor, second=second, **kwargs)


def named_cats(h):
    a, b = owner(h), owner(h)
    add(h, "我叫阿川", "identity.name", "阿川", actor=a, second=1)
    cat(h, a, "栗团", second=2)
    add(h, "我叫小禾", "identity.name", "小禾", actor=b, second=3)
    cat(h, b, "栗糖", second=4)
    return a, b


def test_cold_group_recall_preserves_owner_and_legacy_select_stays_exact(room):
    h = room
    a = owner(h)
    cat(h, a)
    h.calls.clear()
    selected = recalled(h)
    assert [unit["subject_person_id"] for unit in selected["selected_units"]] == [
        h.scope(a)["person_id"]
    ]
    assert [check["scope"] for check in selected["scope_checks"]] == [h.scope(), h.scope(a)]
    assert all(unit["visibility"] == "shared_projection" for unit in selected["selected_units"])
    assert [kind for kind, _, _ in h.calls if kind != "origin"] == ["snapshot", "current", "head"]
    current = next(body for kind, body, _ in h.calls if kind == "current")
    assert current["viewer"]["scope"] == h.scope()
    assert any(admission["scope"] == h.scope(a) for admission in current["admissions"])
    legacy = h.selection()
    legacy.update(query_text="群里那只猫叫什么", selection=["evidence"])
    response = h.post("memory/select", legacy)
    assert response.status_code == 200 and response.json()["selected_units"] == []
    assert recalled(h, probe(h, selected))["scope_checks"] == selected["scope_checks"]


def test_group_never_expands_private_other_room_or_actor(room):
    h = room
    a = owner(h, audience="self_private")
    b = owner(h, conversation="other-room")
    cat(h, a, second=1)
    cat(h, b, second=2)
    cat(h, 1, second=3)  # same account/room, another actor
    assert recalled(h)["selected_units"] == []
    request = query(h, include_associated=True)
    assert recalled(h, request)["scope_checks"] == [recalled(h)["scope_checks"][0]]


@pytest.mark.parametrize("change", ["withdraw", "denied", "binding", "viewer"])
def test_zero_budget_and_history_recheck_each_owner_and_actual_viewer(room, change):
    h = room
    a = owner(h)
    cat(h, a)
    selected = recalled(h)
    saved = h.post("memory/context/history", history(h, selected))
    assert saved.status_code == 200, saved.text
    if change == "withdraw":
        h.physicals[-1]["state"] = "withdrawn"
        h.core_head["sequence"] += 1
    elif change == "denied":
        h.grants[-1]["state"] = "denied"
        h.platform_head["sequence"] += 1
    elif change == "binding":
        with h.store.transaction() as db:
            db.execute(
                "UPDATE accounts SET version=version+1 WHERE person_id=?",
                (h.scope(a)["person_id"],),
            )
    else:
        h.contexts["synthetic-viewer:0"]["revoked"] = True
        h.platform_head["sequence"] += 1
    # The origin response schema itself excludes revoked=true before domain authorization.
    statuses = {400} if change == "viewer" else {403, 409, 503}
    assert h.post("memory/context/query", probe(h, selected)).status_code in statuses
    assert (
        h.post("memory/context/history", history(h, known=saved.json()["snapshot"])).status_code
        in statuses
    )


def test_known_owner_set_is_not_a_group_roster_or_a_scope_grant(room):
    h = room
    a = owner(h)
    cat(h, a)
    selected = recalled(h)
    b = owner(h)  # unrelated newly bound member has no shared memory
    assert recalled(h, probe(h, selected))["scope_checks"] == selected["scope_checks"]
    forged = probe(h, selected)
    forged["known_scope_checks"].append(dict(forged["known_scope_checks"][0], scope=h.scope(b)))
    assert h.post("memory/context/query", forged).status_code == 409
    assert (
        h.post(
            "memory/context/history",
            history(h, dict(selected, scope_checks=forged["known_scope_checks"])),
        ).status_code
        == 409
    )


def test_history_retains_additions_but_expected_content_and_changed_history_reject(room):
    h = room
    a = owner(h)
    cat(h, a)
    selected = recalled(h)
    saved = h.post("memory/context/history", history(h, selected)).json()
    add(h, "我喜欢陶艺", "interest.pottery", "陶艺", actor=a, second=2)
    assert h.post("memory/context/history", history(h, selected)).status_code == 409
    assert h.post("memory/context/history", history(h, known=saved["snapshot"])).status_code == 200
    with h.store.transaction() as db:
        h.service._bump(db, h.scope(a))
    assert h.post("memory/context/history", history(h, known=saved["snapshot"])).status_code == 409


def test_names_use_unique_current_identity_witness_and_keep_self_topics(room):
    h = room
    a, _ = named_cats(h)
    selected = recalled(h, query(h, "阿川那只猫叫什么"))
    assert {unit["statement"] for unit in selected["selected_units"]} == {
        "我叫阿川",
        "我的猫叫栗团",
    }
    assert {unit["subject_person_id"] for unit in selected["selected_units"]} == {
        h.scope(a)["person_id"]
    }
    assert len(selected["dependency_groups"]) == 2
    assert recalled(h, query(h, "我的宠物名字和种类是什么"))["selected_units"] == []
    cat(h, 0, "雪球", second=5)
    add(h, "我叫晓北", "identity.name", "晓北", second=6)
    mine = recalled(h, query(h, "我的猫叫什么"))
    assert [unit["statement"] for unit in mine["selected_units"]] == ["我的猫叫雪球"]
    assert len(mine["scope_checks"]) == 1
    pet = recalled(h, query(h, "我的宠物名字和种类是什么"))
    assert [unit["statement"] for unit in pet["selected_units"]] == ["我的猫叫雪球"]
    assert self_reference("我的猫叫什么")
    assert not self_reference("阿川说‘我的猫’，群里那只猫叫什么")


def test_multiple_owner_payloads_and_rank_index_use_bounded_batches(room, monkeypatch):
    h = room
    named_cats(h)
    transaction = h.store.transaction
    statements = []

    @contextmanager
    def observe():
        with transaction() as db:
            if is_postgres(db):
                native = db.native

                def record(sql, parameters=(), **options):
                    statements.append(sql)
                    return native(sql, parameters, **options)

                db.native = record
            else:
                db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(h.store, "transaction", observe)
    selected = recalled(h)
    assert {unit["statement"] for unit in selected["selected_units"]} == {
        "我的猫叫栗团",
        "我的猫叫栗糖",
    }
    indexes = [
        sql for sql in statements if "SELECT record_id,record_version,text FROM search_index" in sql
    ]
    assert len(indexes) == 1 and "WHERE scope IN (" in indexes[0]
    if h.store.backend == "postgresql":
        bodies = [sql for sql in statements if "SELECT * FROM records WHERE group_id" in sql]
        assert len(bodies) == 1 and "group_id IN (" in bodies[0]


def test_duplicate_name_and_truncated_window_do_not_claim_unique_owner(room, monkeypatch):
    h = room
    a, b = named_cats(h)
    monkeypatch.setattr("tianshu_memory.memory_context.scopes.GROUP_WINDOW", 3)
    truncated = recalled(h, query(h, "小禾那只猫叫什么"))
    assert not truncated["selected_units"] and truncated["coverage"]["complete"] is False
    monkeypatch.setattr("tianshu_memory.memory_context.scopes.GROUP_WINDOW", 4096)
    add(h, "我叫阿川", "identity.name", "阿川", actor=b, second=5)
    assert recalled(h, query(h, "阿川那只猫叫什么"))["selected_units"] == []
    assert {unit["subject_person_id"] for unit in recalled(h)["selected_units"]} == {
        h.scope(a)["person_id"],
        h.scope(b)["person_id"],
    }


def test_name_uniqueness_cannot_ignore_an_unselected_identity_category(room):
    h = room
    a, b = named_cats(h)
    result, _ = add(h, "我叫阿川", "identity.name", "阿川", actor=b, second=5)
    with h.store.transaction() as db:
        group = result["group_ids"][0]
        db.execute("UPDATE groups SET category='identity' WHERE id=?", (group,))
        for row in db.execute(
            "SELECT id,payload FROM records WHERE group_id=?", (group,)
        ).fetchall():
            unit = json.loads(row["payload"])
            unit["category"] = "identity"
            db.execute("UPDATE records SET payload=? WHERE id=?", (canonical(unit), row["id"]))
    selected = recalled(h, query(h, "阿川那只猫叫什么", selection=["evidence"]))
    assert not selected["selected_units"]
    assert a != b


def test_quoted_self_reference_does_not_become_requester_identity(room):
    h = room
    a = owner(h)
    cat(h, a)
    selected = recalled(h, query(h, "群里有人说‘我的猫’，那只猫叫什么"))
    assert [unit["subject_person_id"] for unit in selected["selected_units"]] == [
        h.scope(a)["person_id"]
    ]


def test_group_read_is_not_a_wire_flag_or_a_write_permission(room):
    h = room
    a = owner(h)
    cat(h, a)
    request = query(h)
    request["group_read"] = True
    assert h.post("memory/context/query", request).status_code == 400
    assert recalled(h)["selected_units"]
    operation = query(h)["query"]
    operation.update(idempotency_key="cross-owner-write", deadline_at="2030-01-01T00:00:00Z")
    h.config["callers"]["companion"]["operations"].append("context_propose")
    h.save()
    proposed = dict(
        command=operation,
        scope=h.scope(a),
        batch_ref=None,
        item_id="cross-owner",
        kind="no_op",
        target=dict(
            category="evidence",
            field_key="test.owner",
            item_key=None,
            record_id=None,
            expected_version=None,
        ),
        units=[],
        evidence_refs=[],
        proof_ref=None,
    )
    # The existing author-only authorization still rejects a read owner used as a writer.
    response = h.post("memory/context/propose", proposed)
    assert response.status_code == 403, response.text


def test_name_witness_and_target_must_fit_together(room):
    h = room
    named_cats(h)
    full = recalled(h, query(h, "阿川那只猫叫什么"))
    one_group = recalled(h, query(h, "阿川那只猫叫什么", limit=1))
    assert one_group["selected_units"] == [] and "budget" in one_group["omissions"]
    budget = full["budget_used"]["bytes"] - 1
    short = recalled(h, query(h, "阿川那只猫叫什么", budget={"tokens": budget, "bytes": budget}))
    assert short["selected_units"] == [] and short["coverage"]["complete"] is False
    assert len(short["scope_checks"]) == 1


def test_owner_budget_counts_only_whole_groups_that_actually_fit(room):
    h = room
    authors = [owner(h) for _ in range(18)]
    for second, author in enumerate(authors, 1):
        cat(h, author, f"栗团{second}", second=second)
    selected = recalled(h)
    assert len(selected["scope_checks"]) == 17
    assert len(selected["selected_units"]) == 16
    assert selected["coverage"]["complete"] is False and "budget" in selected["omissions"]
    assert {entry["scope"]["person_id"] for entry in selected["scope_checks"]} == {
        h.person,
        *(unit["subject_person_id"] for unit in selected["selected_units"]),
    }
    first = recalled(h, query(h, limit=1))
    assert len(first["scope_checks"]) == 2
    assert recalled(h, probe(h, first))["scope_checks"] == first["scope_checks"]


def test_mixed_private_lineage_is_rejected_before_payload_read(room):
    h = room
    a, b = owner(h), owner(h, audience="self_private")
    cat(h, a, second=1)
    cat(h, b, second=2)
    with h.store.transaction() as db:
        group = db.execute(
            "SELECT id FROM groups WHERE scope=?", (canonical(h.scope(a)),)
        ).fetchone()[0]
        source = db.execute(
            "SELECT key,revision,epoch FROM sources WHERE scope=?", (canonical(h.scope(b)),)
        ).fetchone()
        db.execute("INSERT INTO lineage VALUES (?,?,?,?)", (group, *source))
        db.execute("UPDATE records SET payload='not-json' WHERE group_id=?", (group,))
    assert recalled(h)["selected_units"] == []


def test_old_alias_hints_require_the_same_fresh_whole_group_domain(room):
    h = room
    a, b = owner(h), owner(h, audience="self_private")
    old, _ = add(
        h,
        "周日约在银杏咖啡馆，先在东门等",
        "event.appointment",
        actor=a,
        second=1,
        keywords=["周日", "银杏咖啡馆", "东门"],
    )
    current, _ = add(
        h,
        "周日东门太晒了，换到后门等",
        "event.appointment",
        actor=a,
        second=2,
        keywords=["周日", "后门", "等候"],
    )
    request = query(h, "银杏咖啡馆最后在哪儿等？")
    selected = recalled(h, request)
    assert {unit["record_id"] for unit in selected["selected_units"]} == set(current["record_ids"])
    cat(h, b, second=3)
    with h.store.transaction() as db:
        source = db.execute(
            "SELECT key,revision,epoch FROM sources WHERE scope=?", (canonical(h.scope(b)),)
        ).fetchone()
        db.execute("INSERT INTO lineage VALUES (?,?,?,?)", (old["group_ids"][0], *source))
    assert recalled(h, request)["selected_units"] == []


@pytest.mark.parametrize("change", ["none", "source", "grant", "witness"])
def test_external_rank_rechecks_group_sources_owners_and_witness(room, change):
    h = room
    named_cats(h)

    class Ranker:
        def settings_for(self, request):
            return dict(mode="hybrid", candidates=100)

        def invalidate(self):
            pass

        def rank(self, query, candidates, lexical, settings):
            if change == "source":
                h.physicals[-1]["state"] = "withdrawn"
                h.core_head["sequence"] += 1
            elif change == "grant":
                h.grants[-1]["state"] = "denied"
                h.platform_head["sequence"] += 1
            elif change == "witness":
                h.physicals[-4]["state"] = "withdrawn"
                h.core_head["sequence"] += 1
            return lexical

    h.service.retrieval = Ranker()
    response = h.post("memory/context/query", query(h, "阿川那只猫叫什么"))
    if change == "none":
        assert response.status_code == 200, response.text
        assert len(response.json()["selected_units"]) == 2
    else:
        assert response.status_code in {409, 503}, response.text


@pytest.mark.parametrize("revoke", [False, True])
def test_time_range_uses_one_real_viewer_batch_and_rechecks_after_io(room, revoke):
    h = room
    named_cats(h)
    include = h.mutate

    def during_time(kind, body, response):
        include(kind, body, response)
        if revoke and body.get("include_content"):
            h.grants[-1]["state"] = "denied"
            h.platform_head["sequence"] += 1

    h.mutate = during_time
    h.calls.clear()
    response = h.post(
        "memory/context/query",
        query(h, time_range={"from": "2026-10-11T00:00:00Z", "to": "2026-10-12T00:00:00Z"}),
    )
    if revoke:
        assert response.status_code in {409, 503}, response.text
    else:
        assert response.status_code == 200, response.text
        assert len(response.json()["selected_units"]) == 2
    reads = [body for kind, body, _ in h.calls if kind == "snapshot" and body["include_content"]]
    assert len(reads) == 1 and len(reads[0]["selectors"]) == 2
    assert all(
        body["viewer"]["scope"] == h.scope() for kind, body, _ in h.calls if kind == "current"
    )


def test_current_name_witness_can_predate_the_target_time_window(room):
    h = room
    a = owner(h)
    add(h, "我叫阿川", "identity.name", "阿川", actor=a, second=1, sent_at="2026-10-01T00:00:00Z")
    cat(h, a, second=2, sent_at="2026-10-20T00:00:00Z")
    h.calls.clear()
    selected = recalled(
        h,
        query(
            h,
            "阿川那只猫叫什么",
            time_range={"from": "2026-10-19T00:00:00Z", "to": "2026-10-26T00:00:00Z"},
        ),
    )
    assert {unit["statement"] for unit in selected["selected_units"]} == {
        "我叫阿川",
        "我的猫叫栗团",
    }
    assert selected["coverage"]["missing_source_times"] == 0
    time_reads = [
        body for kind, body, _ in h.calls if kind == "snapshot" and body["include_content"]
    ]
    assert len(time_reads) == 1
    assert [selector["key"]["message_id"] for selector in time_reads[0]["selectors"]] == [
        "quality-source:2"
    ]
    outside = recalled(
        h,
        query(
            h,
            "阿川那只猫叫什么",
            time_range={"from": "2026-10-22T00:00:00Z", "to": "2026-10-23T00:00:00Z"},
        ),
    )
    assert not outside["selected_units"]


@pytest.mark.parametrize("change", ["expiry", "same-name-start", "new-name-write"])
def test_external_io_reproves_current_name_and_room_uniqueness(room, change):
    h = room
    moment = [datetime(2026, 10, 21, tzinfo=UTC)]
    h.service.clock = lambda: moment[0]
    a, b = owner(h), owner(h)
    add(
        h,
        "我叫阿川，有效期截止2026-10-21T00:00:01Z" if change == "expiry" else "我叫阿川",
        "identity.name",
        "阿川",
        actor=a,
        second=1,
        valid_to=utc(moment[0] + timedelta(seconds=1)) if change == "expiry" else None,
    )
    cat(h, a, second=2)
    if change == "same-name-start":
        add(
            h,
            "我从2026-10-21T00:00:01Z起叫阿川",
            "identity.name",
            "阿川",
            actor=b,
            second=3,
            valid_from=utc(moment[0] + timedelta(seconds=1)),
        )
    else:
        add(h, "我叫小禾", "identity.name", "小禾", actor=b, second=3)
    cat(h, b, "栗糖", second=4)
    assert len(recalled(h, query(h, "阿川那只猫叫什么"))["selected_units"]) == 2

    class Ranker:
        def settings_for(self, request):
            return dict(mode="hybrid", candidates=100)

        def invalidate(self):
            pass

        def rank(self, query_text, candidates, lexical, settings):
            if change == "new-name-write":
                with h.store.transaction() as db:
                    before = h.service._scope_version(db, h.scope())
                add(h, "我叫阿川", "identity.name", "阿川", actor=b, second=5)
                with h.store.transaction() as db:
                    assert h.service._scope_version(db, h.scope()) == before
            else:
                moment[0] += timedelta(seconds=2)
            return lexical

    h.service.retrieval = Ranker()
    response = h.post("memory/context/query", query(h, "阿川那只猫叫什么"))
    assert response.status_code in {409, 503}, response.text
