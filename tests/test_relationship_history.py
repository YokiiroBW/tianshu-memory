"""Reason retention, bounded reads and unchanged authority/DTO contracts."""

import pytest
from test_relationships import command
from test_relationships import relationship as relationship

from tianshu_memory.domain import Fault
from tianshu_memory.relationships.history import read


def test_manual_reason_is_retained_replay_is_exact_and_not_a_second_score(relationship):
    h, app, _ = relationship
    request, first = command(h, app, "adjust_affinity", delta=5, reason="合成验收调整")
    assert (
        app.manage(
            request,
            authorization="Bearer test-only-companion-secret",
            assertion_ref="origin-private",
        )
        == first
    )
    result = read(
        app,
        first["pair"],
        authorization="Bearer test-only-companion-secret",
        assertion_ref="origin-private",
        request_id="history-read",
    )
    assert result["projection"] == first
    assert len(result["items"]) == 1
    assert result["items"][0]["reason"] == "合成验收调整"
    assert result["items"][0]["delta"] == 5
    assert not result["has_more"]
    assert "operator" not in str(result) and "source_ref" not in str(result)


def test_history_is_bounded_and_actor_person_isolated(relationship):
    h, app, _ = relationship
    for number in range(25):
        _, projection = command(h, app, "adjust_affinity", delta=1, reason=f"合成{number}")
    result = read(
        app,
        projection["pair"],
        authorization="Bearer test-only-companion-secret",
        assertion_ref="origin-private",
        request_id="history-bounded",
    )
    assert len(result["items"]) == 20 and result["has_more"]
    assert result["projection"]["score"] == 25
    with pytest.raises(Fault):
        read(
            app,
            dict(projection["pair"], actor_id="actor:other"),
            authorization="Bearer test-only-companion-secret",
            assertion_ref="origin-private",
            request_id="history-other",
        )


@pytest.mark.parametrize("mutation", ["group", "revoked", "permission"])
def test_history_requires_live_private_management_authority(relationship, mutation):
    h, app, _ = relationship
    _, result = command(h, app, "set_freeze", frozen=True)
    origin = "origin-private"
    if mutation == "group":
        origin = "origin-group"
    elif mutation == "revoked":
        h.config["origins"][origin]["revoked"] = True
    else:
        h.config["callers"]["companion"]["operations"].remove("relationships.manage")
    h.save_config()
    with pytest.raises(Fault):
        read(
            app,
            result["pair"],
            authorization="Bearer test-only-companion-secret",
            assertion_ref=origin,
            request_id="history-denied",
        )
