"""Whole-scope association endpoints, with unchanged current binding checks."""

import pytest
from test_memory_context import ProofIssuer
from test_memory_v2 import setup
from test_memory_v2_domains import private_conversation
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.memory_context.authorization import authorized_scopes


@pytest.mark.parametrize("read_from", ["source", "target"])
def test_association_matches_both_complete_endpoints_and_current_bindings(sync, read_from):
    if sync.scope()["audience"] != "self_private":
        pytest.skip("Explicit private association only")
    setup(sync)
    sync.service.contracts.load_context()
    other = private_conversation(sync, "private:scope-endpoint", same_person=False)
    sync.config["callers"]["companion"]["operations"].append("context_association")
    sync.save()
    proof = ProofIssuer()
    sync.service.memory_context.proofs = proof
    link = dict(
        command=sync.command(),
        action="link",
        source_account=sync.account,
        target_account=sync.physicals[other]["author"],
        association_id=None,
        expected_version=0,
        scopes=[sync.scope(), sync.scope(other)],
        proof_ref="scope-endpoint:proof",
    )
    proof.issue(
        link, "association", [link["source_account"], link["target_account"]], link["scopes"]
    )
    response = sync.post("memory/context/association", link)
    assert response.status_code == 200, response.text
    identity = response.json()["association_id"]
    scope, peer = link["scopes"] if read_from == "source" else list(reversed(link["scopes"]))
    with sync.store.transaction() as db:
        assert authorized_scopes(sync.service, db, scope, True) == [
            (scope, None, 1),
            (peer, identity, 1),
        ]
        assert authorized_scopes(sync.service, db, scope, False) == [(scope, None, 1)]
        # A matching person alone never admits another conversation, actor or audience.
        for field, value in (
            ("actor_id", "actor:unlinked"),
            ("person_id", "person:unlinked"),
            ("conversation_id", "private:unlinked"),
            ("audience", "group"),
        ):
            changed = dict(scope, **{field: value})
            assert authorized_scopes(sync.service, db, changed, True) == [(changed, None, 1)]
        # Stale consent on either binding remains unusable even with an exact endpoint.
        for side in ("source", "target"):
            column = side + "_binding"
            db.execute(
                "UPDATE context_associations SET " + column + "=" + column + "+1 WHERE id=?",
                (identity,),
            )
            assert authorized_scopes(sync.service, db, scope, True) == [(scope, None, 1)]
            db.execute(
                "UPDATE context_associations SET " + column + "=" + column + "-1 WHERE id=?",
                (identity,),
            )
        assert authorized_scopes(sync.service, db, scope, True) == [
            (scope, None, 1),
            (peer, identity, 1),
        ]
