"""Target attribution is checked through the current multi-person processing port."""

import copy

import pytest
from test_group_context import owner
from test_segments import front, output, pending, ready, seal, submit
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync


@pytest.mark.parametrize("damage", ["foreign_target", "foreign_source", "missing_target"])
def test_current_segment_rejects_cross_person_attribution_atomically(sync, damage):
    if sync.scope()["audience"] != "group":
        pytest.skip("Different people participate in one real authorized group")
    ready(sync)
    other = owner(sync)
    one, _ = front(sync, "我今天去河边散步。", "one")
    two, _ = front(sync, "我今天去图书馆。", "two", actor=other)
    prepared = pending(sync, seal(sync, [one, two])[0])
    value = copy.deepcopy(output(prepared))
    if damage == "foreign_target":
        value["results"][0]["subject_person_id"] = "person:not-in-this-segment"
    elif damage == "foreign_source":
        value["results"][0]["memories"][0]["source_member_ids"] = value["results"][1][
            "covered_member_ids"
        ]
    else:
        value["results"].pop()
    response, _ = submit(sync, prepared, value)
    assert response.status_code == 400 and response.json()["code"] == "invalid_input", response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
    accepted, _ = submit(sync, prepared, output(prepared))
    assert accepted.status_code == 200 and accepted.json()["state"] == "completed", accepted.text
    assert {item["subject_person_id"] for item in accepted.json()["results"]} == {
        sync.scope()["person_id"],
        sync.scope(other)["person_id"],
    }
