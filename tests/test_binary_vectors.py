import json
import sqlite3

import pytest
from test_memory_v2 import setup
from test_source_sync import certificates as certificates
from test_source_sync import source_contracts as source_contracts
from test_source_sync import source_examples as source_examples
from test_source_sync import sync as sync

from tianshu_memory.domain import canonical
from tianshu_memory.persistent_retrieval import prepare, write
from tianshu_memory.retrieval_vectors import dot, pack, unpack, vectors


def test_binary_and_legacy_rows_mix_without_score_regression():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO metadata VALUES ('memory_retrieval_schema','1');
        CREATE TABLE memory_documents(id TEXT PRIMARY KEY,content_hash TEXT);
        CREATE TABLE memory_vectors(document_id TEXT,content_hash TEXT,embedding_revision TEXT,
          dimension INTEGER,vector TEXT,vector_bytes BLOB,
          PRIMARY KEY(document_id,content_hash,embedding_revision));
    """)
    documents = [dict(id="a", content_hash="a1"), dict(id="b", content_hash="b1")]
    db.executemany("INSERT INTO memory_documents VALUES (?,?)", [("a", "a1"), ("b", "b1")])
    value = vectors([[1, 2, 3]], 1)[0]
    assert write(db, documents[0], "r1", value)
    db.execute(
        "INSERT INTO memory_vectors VALUES (?,?,?,?,?,NULL)", ("b", "b1", "r1", 3, canonical(value))
    )
    settings = dict(mode="hybrid", candidates=8, embedding_revision="r1")
    result = prepare(db, documents, ["a", "b"], settings)
    assert dot(value, result.available["a"]) == pytest.approx(
        dot(value, result.available["b"]), abs=1e-6
    )
    assert not result.corrupted
    # JSON is retained for an older release and binary is actually being used.
    row = db.execute(
        "SELECT vector,vector_bytes FROM memory_vectors WHERE document_id='a'"
    ).fetchone()
    assert json.loads(row[0]) == list(value)
    assert row[1] == pack(value)
    assert list(unpack(row[1])) == pytest.approx(value, abs=1e-6)
    db.close()


def test_explicit_legacy_migration_preserves_guard_backup_and_is_idempotent(sync):
    setup(sync)
    with sync.store.transaction() as db:
        db.execute("DELETE FROM metadata WHERE key='memory_retrieval_schema'")
        db.execute("ALTER TABLE memory_vectors DROP COLUMN vector_bytes")
        db.execute("INSERT INTO memory_vectors VALUES ('old','hash','revision',2,'[1,0]')")
    backup = sync.directory / "pre-retrieval.sqlite"
    result = sync.store.migrate_retrieval(backup)
    assert result["vectors_converted"] == 1
    assert backup.is_file() and backup.with_name(backup.name + ".source-guard.json").is_file()
    with sync.store.transaction() as db:
        row = db.execute("SELECT vector,vector_bytes FROM memory_vectors").fetchone()
        assert row["vector"] == "[1,0]" and list(unpack(row["vector_bytes"])) == [1, 0]
    assert sync.store.migrate_retrieval(backup)["already_installed"]


def test_rollback_keeps_json_and_supports_the_old_positional_writer(sync):
    from tianshu_memory.retrieval_migration import rollback_binary

    setup(sync)
    result = rollback_binary(sync.store, sync.directory / "pre-rollback.sqlite")
    assert result["json_vectors_preserved"]
    with sync.store.transaction() as db:
        db.execute("INSERT INTO memory_vectors VALUES ('old','hash','revision',2,'[1,0]')")
        assert db.execute("SELECT vector FROM memory_vectors").fetchone()[0] == "[1,0]"
    assert (
        sync.store.migrate_retrieval(sync.directory / "pre-reapply.sqlite")["vectors_converted"]
        == 1
    )
