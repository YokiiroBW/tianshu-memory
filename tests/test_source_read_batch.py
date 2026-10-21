"""Local consumption checks for the confirmed read_batch SELECT contract.

These use a result double, not native PostgreSQL syntax or transaction evidence.
The existing native source-snapshot cases exercise CAS, guard and rollback.
"""

from copy import deepcopy

import pytest

from tianshu_memory.domain import canonical, fingerprint
from tianshu_memory.postgres_source_snapshot import ADMISSIONS_SQL, SourceSnapshot


class BatchResults:
    def __init__(self, rows, *, failure=False):
        self.rows, self.failure = rows, failure
        self.calls = []

    def native(self, *_args, **_kwargs):
        raise AssertionError("SourceSnapshot must use the confirmed batch, not individual reads")

    def read_batch(self, requests):
        self.calls.append(deepcopy(requests))
        assert all(sql.startswith("SELECT ") for sql, _ in requests)
        if self.failure:
            raise RuntimeError("synthetic batched read failed")
        # Return one confirmed iterable per original statement in request order.
        return [deepcopy(self.rows[sql.split(" FROM ", 1)[1].split()[0]]) for sql, _ in requests]


def test_source_batch_keeps_every_domain_and_receipt_alias_with_transaction_local_rows():
    physical = dict(key={"message": "synthetic-message"}, author={"account": "synthetic-author"})
    admission = dict(
        selector={"message": "synthetic-message", "actor": "synthetic-actor"},
        source={"receipt_id": "synthetic-receipt"},
    )
    grant = {"actor": "synthetic-actor"}
    snapshot = dict(
        physicals=[physical], admissions=[admission], turns=[dict(turn_id="synthetic-turn")]
    )
    access = dict(grants=[dict(selector=grant)])
    physical_key, admission_key = fingerprint(physical["key"]), fingerprint(admission["selector"])
    alias = dict(key="legacy-alias", physical_key=physical_key, payload=None, receipt=None)
    collision = dict(
        key="unrequested-actor",
        physical_key="unrequested-physical",
        payload="synthetic-payload",
        receipt="synthetic-receipt",
    )
    rows = {
        "owner_heads": [dict(owner="core", sequence=7), dict(owner="platform", sequence=5)],
        "source_observations": [dict(kind="physical", key=physical_key, sequence=7)],
        "physical_sources": [dict(key=physical_key, sequence=7)],
        "accounts": [dict(account_key=canonical(physical["author"]), person_id="synthetic-person")],
        "sources": [dict(key=admission_key, state="active", epoch=2)],
        "suppression": [dict(source_key=admission_key)],
        "source_admissions": [alias, collision],
    }
    db = BatchResults(rows)
    current = SourceSnapshot(db, snapshot, access)
    assert len(db.calls) == 1 and len(db.calls[0]) == 7
    requests = db.calls[0]
    assert [sql.split(" FROM ", 1)[1].split()[0] for sql, _ in requests] == list(rows)
    observations = json_observations(requests[1][1][0])
    assert observations == {
        ("physical", physical_key),
        ("admission", admission_key),
        ("turn", "synthetic-turn"),
        ("access", fingerprint(grant)),
    }
    assert requests[2][1] == ([physical_key],)
    assert requests[3][1] == ([canonical(physical["author"])],)
    assert requests[4][1] == requests[5][1] == ([admission_key],)
    assert requests[6] == (ADMISSIONS_SQL, ([physical_key], [admission_key], ["synthetic-receipt"]))
    assert current.heads == {row["owner"]: row for row in rows["owner_heads"]}
    assert current.observations == {("physical", physical_key): rows["source_observations"][0]}
    assert current.physicals == {physical_key: rows["physical_sources"][0]}
    assert current.bindings == {canonical(physical["author"]): rows["accounts"][0]}
    assert current.suppressed == {admission_key}
    assert current.aliases[physical_key] == {alias["key"]}
    assert current.receipts["synthetic-receipt"] == {collision["key"]}
    current.withdraw(admission_key)
    assert current.sources[admission_key]["epoch"] == 3
    assert current.sources[admission_key]["state"] == "withdrawn"
    # A later _apply reads its own current rows. Neither the previous snapshot's
    # in-transaction withdrawal nor its heads can leak into this new capture.
    db.rows["owner_heads"][0]["sequence"] = 8
    after = SourceSnapshot(db, snapshot, access)
    assert after.heads["core"]["sequence"] == 8 and current.heads["core"]["sequence"] == 7
    assert after.sources[admission_key]["epoch"] == 2
    assert len(db.calls) == 2


def json_observations(payload):
    import json

    return {(row["kind"], row["key"]) for row in json.loads(payload)}


@pytest.mark.parametrize("failure", [False, True])
def test_empty_domains_keep_original_skips_and_a_failed_batch_cannot_supply_rows(failure):
    db = BatchResults(
        {"owner_heads": [], "source_observations": [], "source_admissions": []}, failure=failure
    )
    snapshot, access = dict(physicals=[], admissions=[], turns=[]), dict(grants=[])
    if failure:
        with pytest.raises(RuntimeError, match="synthetic batched read failed"):
            SourceSnapshot(db, snapshot, access)
    else:
        result = SourceSnapshot(db, snapshot, access)
        assert result.physicals == result.bindings == result.sources == result.admissions == {}
        assert result.suppressed == set()
    assert len(db.calls) == 1 and len(db.calls[0]) == 3
    assert db.calls[0][-1] == (ADMISSIONS_SQL, ([], [], []))
