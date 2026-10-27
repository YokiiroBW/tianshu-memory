"""Run the same real query regressions on the existing opt-in isolated PG owner."""

import pytest
from test_query_vector_boundary import (
    test_identical_query_text_embeds_once_per_request_without_reusing_authority as vector_case,
)
from test_query_vector_boundary import (
    test_query_kinds_do_not_rank_the_other_domain_but_keep_source_verification as kinds_case,
)
from test_storage_backends import certificates as certificates
from test_storage_backends import pg_sync as pg_sync
from test_storage_backends import source_contracts as source_contracts
from test_storage_backends import source_examples as source_examples


@pytest.mark.parametrize("failure", [False, True])
def test_postgres_same_text_vectors_remain_request_local(pg_sync, monkeypatch, failure):
    vector_case(pg_sync, monkeypatch, failure)


@pytest.mark.parametrize("kinds", [["facts"], ["episodes"]])
def test_postgres_query_kinds_keep_fresh_selected_source_proof(pg_sync, monkeypatch, kinds):
    kinds_case(pg_sync, monkeypatch, kinds)
