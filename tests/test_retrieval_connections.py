"""Read handles must return to their owner on every persistent retrieval path."""

import sqlite3

import pytest
from test_memory_v2 import (
    SyntheticVectors,
    candidate,
    commit,
    fact,
    query,
    setup,
)
from test_memory_v2 import (
    certificates as certificates,
)
from test_memory_v2 import (
    source_contracts as source_contracts,
)
from test_memory_v2 import (
    source_examples as source_examples,
)
from test_memory_v2 import (
    sync as sync,
)
from test_retrieval import Backend

from tianshu_memory.memory_reindex import MemoryReindexer
from tianshu_memory.retrieval import Retrieval


@pytest.fixture
def reads(sync, monkeypatch):
    """Observe real SQLite/PG handles without changing their transaction semantics."""
    setup(sync)
    acquired = []
    original = type(sync.store).readonly

    class Read:
        def __init__(self, store):
            self.connection = original(store)
            self.closed = False
            acquired.append(self)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            self.connection.close()
            self.closed = True

    monkeypatch.setattr(type(sync.store), "readonly", lambda store: Read(store))
    try:
        yield acquired
    finally:
        for handle in acquired:
            if not handle.closed:
                handle.close()


def test_idle_index_and_continuous_hybrid_queries_release_reads(sync, reads):
    backend = SyntheticVectors()
    sync.service.retrieval = Retrieval(backend)
    sync.service.retrieval.store = sync.store
    indexer = MemoryReindexer(sync.service)
    for _ in range(24):
        assert indexer.index_step() == {"indexed": 0, "state": "complete"}
        assert all(handle.closed for handle in reads)
    assert len(reads) == 24
    text = "我喜欢乌龙茶。"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact("乌龙茶"))["state"] == "completed"
    assert indexer.index_step()["indexed"] == 2
    embedded = sum(map(len, backend.document_batches))
    for _ in range(24):
        answer = query(sync, "我喜欢什么茶")
        assert answer["query_results"][0]["coverage"]["index_complete"]
        assert any(item.get("fact", {}).get("value") == "乌龙茶" for item in answer["evidence"])
        assert indexer.index_step() == {"indexed": 0, "state": "complete"}
        assert all(handle.closed for handle in reads)
    assert backend.query_calls == 24
    assert sum(map(len, backend.document_batches)) == embedded
    # V2 vector preparation now reuses its source transaction; only the
    # initial index step and the 48 idle steps need separate read handles.
    assert len(reads) == 49


def test_legacy_rank_and_rerank_materialization_release_reads(sync, reads):
    from tianshu_memory.persistent_retrieval import rank

    text = "我喜欢咖啡。"
    job, _ = candidate(sync, text)
    assert commit(sync, job, text, fact("咖啡"))["state"] == "completed"
    backend = Backend("hybrid", True)
    retrieval = sync.service.retrieval = Retrieval(backend)
    retrieval.store = sync.store
    assert MemoryReindexer(sync.service).index_step()["indexed"] == 2
    with sync.store.transaction() as db:
        groups = [
            (dict(row), []) for row in db.execute("SELECT * FROM groups WHERE state='active'")
        ]
        documents = [dict(row) for row in db.execute("SELECT * FROM memory_documents")]
    before = len(reads)
    assert retrieval.rank("咖啡", groups, groups, retrieval.settings()) == groups
    assert len(reads) == before + 2
    assert all(handle.closed for handle in reads)
    for document in documents:
        document.pop("text")
    settings = dict(retrieval.settings(), mode="keyword")
    before = len(reads)
    selected, complete = rank(
        retrieval, sync.store, "咖啡", documents, [item["id"] for item in documents], settings
    )
    assert complete and selected
    assert len(reads) == before + 1
    assert all(handle.closed for handle in reads)
    assert len([call for call in backend.calls if call[0] == "rerank"]) == 2


def test_index_read_failure_releases_connection(sync, reads, monkeypatch):
    from tianshu_memory import memory_reindex

    sync.service.retrieval = Retrieval(SyntheticVectors())

    def unavailable(db):
        raise sqlite3.OperationalError("synthetic read failure")

    monkeypatch.setattr(memory_reindex, "installed", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="synthetic read failure"):
        MemoryReindexer(sync.service).index_step()
    assert len(reads) == 1 and reads[0].closed
