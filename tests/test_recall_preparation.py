"""Local equivalence checks, without claiming a native PostgreSQL execution.

SQLite executes the final relational BM25 aggregation over pre-tokenized rows.
The connection double exercises PG preparation branches with SQLite-compatible
queries; real tokenization, driver and authority transactions need the PG suite.
"""

import json
import math
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime

import pytest

from tianshu_memory import postgres_recall
from tianshu_memory.automatic_facts import filter_current
from tianshu_memory.domain import canonical
from tianshu_memory.fact_queries import snapshot_for_groups
from tianshu_memory.recall_index import eligible_groups, rank_groups
from tianshu_memory.source_authority import SourceAuthority


@pytest.mark.parametrize(
    ("documents", "query_terms", "hits"),
    [
        (
            [
                ("first", "receipt retry retry coffee"),
                ("second", "receipt wait"),
                ("unmatched", "other long words no match"),
                ("empty", ""),
            ],
            ["receipt", "retry"],
            ["first", "second"],
        ),
        (
            [("first", "receipt receipt extra"), ("second", "receipt")],
            ["receipt"],
            ["first", "second"],
        ),
        ([("operator-word", "or and"), ("other", "unmatched")], [], ["operator-word"]),
        ([("first", "receipt")], ["missing"], []),
        ([("empty", "")], ["missing"], ["empty"]),
        ([], ["receipt"], []),
    ],
)
def test_relevant_term_aggregation_matches_original_formula(documents, query_terms, hits):
    frequencies = {key: Counter(text.split()) for key, text in documents}
    lengths = {key: sum(values.values()) for key, values in frequencies.items()}
    average = sum(lengths.values()) / len(documents) if documents else None
    counts = {term: sum(term in values for values in frequencies.values()) for term in query_terms}
    expected = {}
    for identity in hits:
        score = 0
        for term in query_terms:
            frequency = frequencies[identity][term]
            if frequency > 0 and average > 0:
                score += (
                    max(
                        0.000001,
                        math.log((len(documents) - counts[term] + 0.5) / (counts[term] + 0.5)),
                    )
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * lengths[identity] / average))
                )
        expected[identity] = -score
    with closing(sqlite3.connect(":memory:")) as db:
        # PostgreSQL GREATEST ignores NULL arguments, unlike Python's max.
        db.create_function(
            "greatest", 2, lambda *values: max(value for value in values if value is not None)
        )
        db.create_function("ln", 1, lambda value: math.log(value) if value is not None else None)
        db.executescript(
            "CREATE TABLE frequencies(group_id TEXT,lexeme TEXT,frequency REAL);"
            "CREATE TABLE lengths(group_id TEXT,length REAL);"
            "CREATE TABLE corpus(total REAL,average_length REAL);"
            "CREATE TABLE hits(group_id TEXT);"
            "CREATE TABLE document_frequencies(term TEXT,documents REAL);"
        )
        db.executemany(
            "INSERT INTO frequencies VALUES (?,?,?)",
            [
                (key, term, count)
                for key, values in frequencies.items()
                for term, count in values.items()
            ],
        )
        db.executemany("INSERT INTO lengths VALUES (?,?)", lengths.items())
        db.execute("INSERT INTO corpus VALUES (?,?)", (len(documents), average))
        db.executemany("INSERT INTO hits VALUES (?)", [(identity,) for identity in hits])
        db.executemany("INSERT INTO document_frequencies VALUES (?,?)", counts.items())
        # Exercise the actual new product aggregation, not a copied SQL formula.
        suffix = postgres_recall.SQL.partition("relevant_frequencies AS MATERIALIZED")[2]
        actual = dict(db.execute("WITH relevant_frequencies AS MATERIALIZED" + suffix))
    assert actual == pytest.approx(expected, abs=1e-12)


def test_bm25_reattaches_only_the_current_input_text_without_returning_it_from_sql():
    calls = []

    class Scores:
        def native(self, sql, parameters):
            calls.append((sql, parameters))
            return [{"group_id": "hit", "relevance": 0.0}]

    documents = [("hit", "原文 or"), ("unmatched", "all authorized words"), ("empty", "")]
    assert list(postgres_recall.rank(Scores(), iter(documents), '"OR"')) == [
        {"group_id": "hit", "relevance": 0.0, "text": "原文 or"}
    ]
    assert len(calls) == 1
    assert json.loads(calls[0][1][0]) == [
        {"group_id": identity, "text": text} for identity, text in documents
    ]
    assert calls[0][1][1:] == ('"OR"', '"OR"')


class RelationalPostgres:
    """A local branch double, not a PostgreSQL driver/backend or FTS substitute."""

    def __init__(self, db):
        self.db = db
        self.statements = []
        self.lineage_columns = []

    def execute(self, sql, parameters=()):
        self.statements.append(sql)
        rows = self.db.execute(sql, parameters)
        if "FROM lineage l JOIN sources" in sql:
            self.lineage_columns.append([column[0] for column in rows.description])
        return rows

    def native(self, sql, parameters=()):
        return self.execute(sql, parameters)


def test_fact_filter_and_history_rank_share_one_explicit_transaction_snapshot(monkeypatch):
    query = "以前《夜航》的标题"
    facts = {
        identity: dict(
            key="title",
            value=title,
            valid_from=None,
            valid_to=None,
            state="current",
            entity=dict(id=entity, kind="notebook", mention=mention),
        )
        for identity, title, entity, mention in [
            ("old", "暮色", "notebook-one", "第一本笔记本"),
            ("new", "夜航", "notebook-one", "第一本笔记本"),
            ("other", "晨光", "notebook-two", "第二本笔记本"),
        ]
    }
    payloads = {identity: canonical(fact) for identity, fact in facts.items()}
    decoded = Counter()
    loads = json.loads

    def counted(value, *args, **kwargs):
        if value in payloads.values():
            decoded[value] += 1
        return loads(value, *args, **kwargs)

    def matching(_db, documents, _expression):
        return [dict(group_id=identity, text=text, relevance=0) for identity, text in documents]

    monkeypatch.setattr(json, "loads", counted)
    monkeypatch.setattr(postgres_recall, "rank", matching)
    candidates = [
        (
            dict(id=identity, scope="scope"),
            [dict(id="record-" + identity, version=1)],
            [dict(statement=fact["value"])],
        )
        for identity, fact in facts.items()
    ]
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE metadata(key TEXT,value TEXT);"
            "INSERT INTO metadata VALUES ('automatic_fact_schema','1');"
            "CREATE TABLE automatic_facts(group_id TEXT,payload TEXT,superseded_by TEXT);"
            "CREATE TABLE search_index(record_id TEXT,record_version INTEGER,text TEXT,scope TEXT);"
        )
        db.executemany(
            "INSERT INTO automatic_facts VALUES (?,?,?)",
            [(key, value, "new" if key == "old" else None) for key, value in payloads.items()],
        )
        db.executemany(
            "INSERT INTO search_index VALUES (?,1,?,'scope')",
            [("record-" + key, fact["value"]) for key, fact in facts.items()],
        )
        remote = RelationalPostgres(db)
        snapshot = snapshot_for_groups(remote, candidates)

        def clock():
            return datetime(2026, 10, 21, tzinfo=UTC)

        filtered = filter_current(remote, candidates, query, clock, fact_snapshot=snapshot)
        ranked = rank_groups(None, remote, filtered, ["夜航"], query, fact_snapshot=snapshot)
        assert {entry[0]["id"] for entry in ranked} == {"old", "new"}
        assert decoded == Counter({value: 1 for value in payloads.values()})
        assert sum("automatic_fact_schema" in sql for sql in remote.statements) == 1
        assert sum("FROM automatic_facts" in sql for sql in remote.statements) == 1

        db.commit()
        # A new operation must capture the new payload; no connection/owner cache.
        changed = canonical(dict(facts["new"], valid_from="2027-01-01T00:00:00Z"))
        db.execute("UPDATE automatic_facts SET payload=? WHERE group_id='new'", (changed,))
        db.commit()
        fresh = snapshot_for_groups(remote, candidates)
        assert fresh is not snapshot and fresh.decoded["new"]["valid_from"] is not None
        current = filter_current(remote, candidates, "夜航", clock, fact_snapshot=fresh)
        assert [entry[0]["id"] for entry in current] == ["other"]


def test_sqlite_does_not_capture_a_fact_snapshot_or_add_a_read():
    statements = []
    with closing(sqlite3.connect(":memory:")) as db:
        db.set_trace_callback(statements.append)
        assert snapshot_for_groups(db, [({"id": "unused"}, [], [])]) is None
    assert statements == []


@pytest.mark.parametrize("batch_admission", [False, True])
def test_reduced_lineage_projection_retains_both_authority_paths(h, batch_admission):
    h.seed([h.draft(field_key=f"coffee-{i}") for i in range(2)])
    with h.store.transaction() as db:
        if batch_admission:
            # Only current/current_many are under test, without remote owner I/O.
            db.execute("CREATE TEMP TABLE source_admissions(key TEXT,verified INTEGER)")
            db.execute("CREATE TEMP TABLE suppression(source_key TEXT)")
            db.execute("INSERT INTO source_admissions SELECT key,1 FROM sources")
            h.service.source_authority = object.__new__(SourceAuthority)
        groups = db.execute("SELECT * FROM groups ORDER BY id").fetchall()
        remote = RelationalPostgres(db)
        result = eligible_groups(h.service, remote, groups, h.private)
        assert [entry[0]["id"] for entry in result] == [group["id"] for group in groups]
        assert remote.lineage_columns == [
            ["group_id", "required_revision", "required_epoch", "key", "state", "revision", "epoch"]
        ]
        remote.statements.clear()
        db.execute("UPDATE sources SET epoch=epoch+1")
        assert eligible_groups(h.service, remote, groups, h.private) == []
        assert not any("FROM records" in sql for sql in remote.statements)
