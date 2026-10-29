"""Candidate failures expose code locations without source text or credentials."""

from test_segments import ready
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.candidate_http import failure_location
from tianshu_memory.domain import Fault


def test_candidate_failure_location_excludes_exception_text():
    error = Fault("dependency_unavailable", 503)
    error.source_owner = "core"
    error.source_operation = "segment_frame"
    error.source_http_status = 403
    error.private_text = "private source or token"
    diagnostic = failure_location(error)
    assert diagnostic == dict(
        source_owner="core", source_operation="segment_frame", source_http_status=403
    )


def test_candidate_failure_location_ignores_unregistered_peer_values():
    error = Fault("dependency_unavailable", 503)
    error.source_owner = "private source or token"
    error.source_operation = "private source or token"
    error.source_http_status = True
    assert failure_location(error) == {}


def test_candidate_http_retains_only_machine_failure_after_business_start(sync, monkeypatch):
    from tianshu_memory.segments import Segments

    ready(sync)

    def unavailable(self, request, publisher):
        error = Fault("dependency_unavailable", 503)
        error.source_owner = "core"
        error.source_operation = "segment_frame"
        error.source_http_status = 403
        error.private_text = "private source or token"
        raise error

    monkeypatch.setattr(Segments, "execute", unavailable)
    response = sync.post("memory/candidates/process", dict(extraction_schema=6))
    assert response.status_code == 503
    value = response.json()
    assert value["execution_state"] == "unknown"
    assert value["diagnostic"]["source_http_status"] == 403
    assert value["diagnostic"]["site"].startswith("tianshu_memory.candidate_http:execute:")
    assert "private source or token" not in response.text
