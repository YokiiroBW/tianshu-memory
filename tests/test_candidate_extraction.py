"""Retired automatic protocols are rejected by the current authenticated endpoint."""

import pytest
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

PATH = "memory/candidates/process"


@pytest.mark.parametrize("protocol", [None, 3, 4, 5])
def test_retired_extraction_protocol_cannot_read_or_commit(sync, protocol):
    from test_segments import front, ready

    ready(sync)
    material, _ = front(sync, "当前资料不能交回旧提取器。", "retired-protocol")
    payload = dict(job_id=material["material_ref"], output=None)
    if protocol is not None:
        payload["extraction_schema"] = protocol
    response = sync.post(PATH, payload)
    assert response.status_code == 400, response.text
    with sync.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM write_ledger").fetchone()[0] == 0
