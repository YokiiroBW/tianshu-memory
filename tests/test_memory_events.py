import copy
import json

from test_contextual_recall import contextual_query
from test_memory_review import saved
from test_segments import ready
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical
from tianshu_memory.event_projection import project


def claim(
    identity,
    attribute,
    value,
    *,
    slot="global",
    person=None,
    speaker="a",
    qualification="direct",
    relation="introduces",
    replaces=(),
):
    return dict(
        id=identity,
        event_id="event",
        slot=slot,
        attribute=attribute,
        value=value,
        person_id=person,
        speaker=speaker,
        qualification=qualification,
        relation=relation,
        replaces=list(replaces),
        observed_at="2026-10-29T00:00:00Z",
        valid_from=None,
        valid_to=None,
        recorded_at="2026-10-30T00:00:00Z",
    )


def test_five_steps_update_only_the_explicit_event_attribute_and_person():
    claims = [
        claim("proposal", "time", "13:00"),
        claim("a-joined", "participation", "joined", slot="participant", person="a"),
        claim(
            "delivery-pending", "delivery", "pending", slot="participant", person="c", speaker="c"
        ),
    ]
    assert "approval" not in project(claims)["global_slots"]
    claims.append(claim("condition", "conditions", "approval pending"))
    assert project(claims)["global_slots"]["time"]["current"][0]["value"] == "13:00"
    claims.append(claim("reschedule", "time", "14:00", relation="updates", replaces=["proposal"]))
    assert project(claims)["global_slots"]["time"]["current"][0]["value"] == "14:00"
    claims.append(
        claim(
            "a-left",
            "participation",
            "withdrawn",
            slot="participant",
            person="a",
            relation="cancels",
            replaces=["a-joined"],
        )
    )
    claims.append(
        claim(
            "delivered",
            "delivery",
            "completed",
            slot="participant",
            person="c",
            speaker="c",
            relation="updates",
            replaces=["delivery-pending"],
        )
    )
    view = project(claims)
    assert view["participants"]["a"]["participation"]["current"][0]["value"] == "withdrawn"
    assert view["participants"]["c"]["delivery"]["current"][0]["value"] == "completed"
    assert len(view["global_slots"]["time"]["history"]) == 1
    assert project(list(reversed(claims))) == view


def test_third_party_report_and_later_claim_do_not_become_a_persons_own_promise():
    claims = [
        claim("real", "time", "14:00"),
        claim(
            "report",
            "commitment",
            "agreed",
            slot="participant",
            person="b",
            speaker="a",
            qualification="third_party",
        ),
        claim("later", "time", "15:00"),
    ]
    view = project(claims)
    assert "b" not in view["participants"]
    assert len(view["global_slots"]["time"]["current"]) == 2
    assert {row["reason"] for row in view["disputes"]} == {"unconfirmed_report", "parallel_claims"}


def test_source_backed_projection_keeps_time_axes_and_fails_whole_after_forget(sync):
    ready(sync)
    sync.store.migrate_events(sync.directory / "pre-events.sqlite")
    _, first = saved(
        sync,
        "event-one",
        "约好一起取书。",
        "2026-10-25T00:00:00Z",
        kind="agreement",
        state="confirmed",
        valid_until="2026-12-01T00:00:00Z",
    )
    ref = first["results"][0]["memory_refs"][0]
    _, second = saved(
        sync,
        "event-two",
        "这个取书安排取消。",
        "2026-10-26T00:00:00Z",
        kind="agreement",
        state="cancelled",
        operation="replace",
        prior_refs=[ref],
    )
    with sync.store.transaction() as db:
        state = db.execute("SELECT * FROM memory_event_state").fetchone()
        assert state["version"] == 2
        basis = json.loads(state["basis"])
        assert len(basis["groups"]) == 2
        assert len({source["source_key"] for source in basis["sources"]}) >= 2
        assert all(claim["valid_from"] is None for claim in basis["claims"])
        assert basis["claims"][0]["observed_at"] == "2026-10-25T00:00:00Z"
        assert basis["claims"][0]["valid_to"] == "2026-12-01T00:00:00Z"
        event_id = state["event_id"]
    request = dict(event_id=event_id, scope=sync.scope())
    result = sync.post("memory/candidates/event-state", request)
    assert result.status_code == 200, result.text
    assert result.json()["available"] is True
    participant = result.json()["slots"]["participants"][sync.scope()["person_id"]]
    assert participant["reported_state"]["current"][0]["value"] == "cancelled"
    # Forget one necessary old source, even though the current summary has newer evidence.
    with sync.store.transaction() as db:
        key = basis["sources"][0]["source_key"]
        source = db.execute("SELECT payload FROM sources WHERE key=?", (key,)).fetchone()
        payload = json.loads(source[0])
    selector = payload.get("message_key")
    if selector is not None:
        original = {key: value for key, value in selector.items() if key != "revision"}
        physical = next(row for row in sync.physicals if row["key"] == original)
        physical.update(
            state="withdrawn",
            kind="retract",
            revision=physical["revision"] + 1,
            physical_receipt_id="withdrawn:event-basis",
            content_digest="b" * 64,
        )
        sync.core_head["sequence"] += 1
    else:
        # The same domain check also rejects an explicit local record forget.
        response = sync.post("memory/revise", sync.revision(ref["record_id"], "forget"))
        assert response.status_code == 200, response.text
    result = sync.post("memory/candidates/event-state", request)
    assert result.status_code in {200, 403, 409}, result.text
    if result.status_code == 200:
        assert result.json() == dict(available=False, event_id=event_id)
    foreign = copy.deepcopy(request)
    foreign["scope"]["person_id"] = "unbound-foreign-person"
    denied = sync.post("memory/candidates/event-state", foreign)
    assert denied.status_code in {403, 404}
    assert canonical(participant) not in denied.text


def test_explicit_action_state_and_thought_revision_use_the_same_projector(sync):
    ready(sync)
    sync.store.migrate_events(sync.directory / "pre-events.sqlite")
    _, first = saved(
        sync,
        "thought-one",
        "此前偏好纸质书。",
        "2026-10-25T00:00:00Z",
        kind="thought",
        state="described",
    )
    saved(
        sync,
        "thought-two",
        "现在明确改为偏好电子书。",
        "2026-10-26T00:00:00Z",
        kind="thought",
        state="described",
        operation="replace",
        prior_refs=first["results"][0]["memory_refs"],
    )
    saved(
        sync,
        "action-one",
        "取书任务已完成。",
        "2026-10-26T01:00:00Z",
        kind="experience",
        state="completed",
    )
    with sync.store.transaction() as db:
        assert {row[0] for row in db.execute("SELECT kind FROM memory_event_state")} == {
            "preference",
            "task",
        }


def test_explicit_cross_window_relation_keeps_the_whole_authorized_basis_in_selection(sync):
    ready(sync)
    sync.store.migrate_events(sync.directory / "pre-events.sqlite")
    _, first = saved(
        sync,
        "cross-one",
        "考虑一起取书。",
        "2026-10-25T00:00:00Z",
        kind="agreement",
        state="considering",
    )
    saved(
        sync,
        "cross-two",
        "确认一起取书。",
        "2026-10-26T00:00:00Z",
        kind="agreement",
        state="confirmed",
        operation="append",
        prior_refs=first["results"][0]["memory_refs"],
    )
    one = contextual_query(sync, "取书", limits=dict(facts=1, episodes=0))
    assert one["evidence"] == [] and "budget" in one["omissions"]
    two = contextual_query(sync, "取书", limits=dict(facts=2, episodes=0))
    assert {item["unit"]["statement"] for item in two["evidence"]} == {
        "考虑一起取书。",
        "确认一起取书。",
    }
