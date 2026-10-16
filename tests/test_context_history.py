"""History authority survives additions, but never invalidating content changes."""

import json

import pytest
from test_memory_context import continuity as continuity
from test_memory_context import correction, post_ok, proposal, query_request

from tianshu_memory.memory_context.history import history_version, metadata_key


def request(h, *, expected=None, known=None):
    query = query_request(h)
    return dict(
        query=query["query"],
        requested_scope=query["requested_scope"],
        include_associated=False,
        expected_snapshot=expected,
        known_snapshot=known,
    )


def test_addition_preserves_history_and_content_snapshot_still_changes(continuity):
    h = continuity
    before = post_ok(h, "history", request(h))
    post_ok(h, "propose", proposal(h))
    after = post_ok(h, "history", request(h, known=before["snapshot"]))
    assert after["scope_version"] > before["scope_version"]
    assert after["snapshot"] == before["snapshot"]
    stale = query_request(h, known_scope_version=before["scope_version"])
    assert h.post("memory/context/query", stale).status_code == 409


def test_invalidating_revision_and_unclassified_writer_fail_closed(continuity):
    h = continuity
    post_ok(h, "propose", proposal(h))
    before = post_ok(h, "history", request(h))
    with h.store.transaction() as db:
        h.service._bump(db, h.private)
    assert h.post("memory/context/history", request(h, known=before["snapshot"])).status_code == 409
    current = post_ok(h, "history", request(h))
    with h.store.transaction() as db:
        db.execute("UPDATE scopes SET version=version+1")
        assert history_version(h.service, db, h.private) == h.service._scope_version(db, h.private)
    assert (
        h.post("memory/context/history", request(h, known=current["snapshot"])).status_code == 409
    )


def test_history_capture_requires_the_exact_initial_memory_snapshot(continuity):
    h = continuity
    selected = post_ok(h, "query", query_request(h))
    expected = {
        key: selected[key] for key in ("scope_version", "association_version", "scope_checks")
    }
    post_ok(h, "history", request(h, expected=expected))
    post_ok(h, "propose", proposal(h))
    assert h.post("memory/context/history", request(h, expected=expected)).status_code == 409


def test_history_does_not_grant_another_scope(continuity):
    h = continuity
    body = request(h)
    body["requested_scope"] = dict(body["requested_scope"], person_id="person:unrelated")
    assert h.post("memory/context/history", body).status_code == 403


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_user_revision_invalidates_retained_history(continuity, kind):
    h = continuity
    first = post_ok(h, "propose", proposal(h))
    before = post_ok(h, "history", request(h))
    post_ok(h, "propose", correction(h, first, kind=kind))
    assert h.post("memory/context/history", request(h, known=before["snapshot"])).status_code == 409


def test_input_history_survives_only_automatic_fact_changes(continuity):
    h = continuity
    original_input = post_ok(h, "input-history", request(h))
    original_answer = post_ok(h, "history", request(h))
    with h.store.transaction() as db:
        h.service._bump(db, h.private, source_history_changed=False)
    after = post_ok(h, "input-history", request(h, known=original_input["snapshot"]))
    assert after["scope_version"] > original_input["scope_version"]
    assert (
        h.post("memory/context/history", request(h, known=original_answer["snapshot"])).status_code
        == 409
    )
    with h.store.transaction() as db:
        h.service._bump(db, h.private)
    assert (
        h.post(
            "memory/context/input-history", request(h, known=original_input["snapshot"])
        ).status_code
        == 409
    )


@pytest.mark.parametrize("kind", ["correct", "forget"])
def test_input_history_keeps_explicit_withdrawal_barriers(continuity, kind):
    h = continuity
    first = post_ok(h, "propose", proposal(h))
    before = post_ok(h, "input-history", request(h))
    post_ok(h, "propose", correction(h, first, kind=kind))
    assert (
        h.post("memory/context/input-history", request(h, known=before["snapshot"])).status_code
        == 409
    )


def test_input_history_conservatively_reads_old_metadata_and_unclassified_changes(continuity):
    h = continuity
    with h.store.transaction() as db:
        h.service._bump(db, h.private)
        key = metadata_key(h.private)
        value = json.loads(
            db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]
        )
        value.pop("source_history_version")
        db.execute("UPDATE metadata SET value=? WHERE key=?", (json.dumps(value), key))
        assert (
            history_version(h.service, db, h.private, input_only=True) == value["history_version"]
        )
    before = post_ok(h, "input-history", request(h))
    with h.store.transaction() as db:
        db.execute("UPDATE scopes SET version=version+1")
    assert (
        h.post("memory/context/input-history", request(h, known=before["snapshot"])).status_code
        == 409
    )


def test_input_history_requires_same_scope_permission_and_exact_snapshot(continuity):
    h = continuity
    selected = post_ok(h, "query", query_request(h))
    expected = {
        key: selected[key] for key in ("scope_version", "association_version", "scope_checks")
    }
    post_ok(h, "input-history", request(h, expected=expected))
    post_ok(h, "propose", proposal(h))
    assert h.post("memory/context/input-history", request(h, expected=expected)).status_code == 409
    foreign = request(h)
    foreign["requested_scope"] = dict(h.private, person_id="person:unrelated")
    assert h.post("memory/context/input-history", foreign).status_code == 403
    h.config["callers"]["companion"]["operations"].remove("context_query")
    h.save_config()
    assert h.post("memory/context/input-history", request(h)).status_code == 403
