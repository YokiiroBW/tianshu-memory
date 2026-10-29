"""Incremental document vectors, owned by the existing Retrieval abstraction."""

import json
import time
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime

from .domain import Fault, canonical, require
from .retrieval_migration import installed as binary_installed
from .retrieval_scoring import combine, order
from .retrieval_vectors import dot, pack, unpack, vectors


@dataclass(frozen=True)
class PreparedQueryVector:
    text: str
    settings: str
    vector: tuple | None


def prepare_query_vector(retrieval, text, settings, *, deadline):
    """Derive only the caller's query before opening a source/Store transaction.

    None means this backend must retain its existing external ranking phase.
    A result with no vector captures a failed bounded attempt; it must fall back
    lexically without calling that backend again inside the source boundary.
    """
    bounded = getattr(retrieval.backend, "embed_query_before", None)
    if settings["mode"] == "keyword" or settings["rerank"] or not callable(bounded):
        return None
    binding = canonical(settings)
    value = None
    try:
        require(time.monotonic() < deadline, "dependency_unavailable", 503)
        response = bounded(text, settings["revision"], deadline)
        require(time.monotonic() < deadline, "dependency_unavailable", 503)
        value = vectors(response["vectors"], 1)[0]
    except (Fault, OSError, TimeoutError, ValueError, TypeError, KeyError):
        pass
    return PreparedQueryVector(text, binding, value)


@dataclass
class PreparedRanking:
    by_id: dict
    document_count: int
    lexical_ids: list
    settings: dict
    available: dict
    corrupted: list
    query_vector: PreparedQueryVector | None = None
    trace: dict | None = None
    summaries: dict | None = None
    now: datetime | None = None

    @property
    def external(self):
        return bool(
            (self.available and self.query_vector is None)
            or (self.settings["rerank"] and self.lexical_ids)
        )


def prepare(db, documents, lexical_ids, settings, *, query_vector=None, summaries=None, now=None):
    """Read the existing vectors without calling a backend or opening another transaction."""
    by_id = {item["id"]: item for item in documents}
    available, corrupted = {}, []
    if settings["mode"] != "keyword" and documents:
        binary_column = "vector_bytes" if binary_installed(db) else "NULL AS vector_bytes"
        for offset in range(0, len(documents), 256):
            batch = documents[offset : offset + 256]
            for row in db.execute(
                f"SELECT document_id,content_hash,vector,{binary_column} FROM memory_vectors WHERE embedding_revision=? AND document_id IN ("
                + ",".join("?" for _ in batch)
                + ")",
                [settings["embedding_revision"], *[item["id"] for item in batch]],
            ):
                if row["content_hash"] == by_id[row["document_id"]]["content_hash"]:
                    try:
                        available[row["document_id"]] = (
                            unpack(row["vector_bytes"])
                            if row["vector_bytes"] is not None
                            else vectors([json.loads(row["vector"])], 1)[0]
                        )
                    except (Fault, ValueError, TypeError):
                        corrupted.append(
                            (
                                row["document_id"],
                                row["content_hash"],
                                settings["embedding_revision"],
                                row["vector"],
                                row["vector_bytes"],
                            )
                        )
    return PreparedRanking(
        by_id,
        len(documents),
        [identity for identity in lexical_ids if identity in by_id][: settings["candidates"]],
        settings,
        available,
        corrupted,
        query_vector,
        summaries=summaries,
        now=now,
    )


def discard_corrupt(db, plan):
    if plan.corrupted:
        sql = "DELETE FROM memory_vectors WHERE document_id=? AND content_hash=? AND embedding_revision=? AND vector=?"
        if binary_installed(db):
            db.executemany(
                sql + " AND (vector_bytes=? OR (vector_bytes IS NULL AND ? IS NULL))",
                [(*row, row[-1]) for row in plan.corrupted],
            )
        else:
            db.executemany(sql, [row[:4] for row in plan.corrupted])


def rank(retrieval, store, query, documents, lexical_ids, settings, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 8
    reader = (
        closing(store.readonly())
        if settings["mode"] != "keyword" and documents
        else nullcontext(None)
    )
    with reader as db:
        plan = prepare(db, documents, lexical_ids, settings)
    if plan.corrupted:
        with store.transaction() as db:
            discard_corrupt(db, plan)
    return rank_prepared(retrieval, store, query, plan, deadline=deadline)


def rank_prepared(retrieval, store, query, plan, *, deadline=None, query_id=None):
    result, complete = _rank_prepared(retrieval, store, query, plan, deadline=deadline)
    if plan.settings.get("trace", False):
        from .diagnostics import record_recall_trace
        from .recall_trace import build

        plan.trace = build(query_id, plan, result, complete)
        record_recall_trace(plan.trace)
    return result, complete


def _rank_prepared(retrieval, store, query, plan, *, deadline=None):
    """Use one prepared snapshot; callers release their authority transaction for external work."""
    if plan.external and deadline is None:
        deadline = time.monotonic() + 8
    by_id, available, settings = plan.by_id, plan.available, plan.settings
    lexical_ids, limit = plan.lexical_ids, settings["candidates"]
    scores = {}
    if available:
        try:
            if plan.query_vector is not None:
                prepared = plan.query_vector
                require(
                    prepared.text == query
                    and prepared.settings == canonical(settings)
                    and prepared.vector is not None,
                    "dependency_unavailable",
                    503,
                )
                # This derived vector has no authority TTL. Waiting for a fresh
                # source boundary does not consume a second embedding budget.
                value = prepared.vector
            else:
                require(time.monotonic() < deadline, "dependency_unavailable", 503)
                query_method = getattr(retrieval.backend, "embed_query", None)
                bounded_query = getattr(retrieval.backend, "embed_query_before", None)
                response = (
                    bounded_query(query, settings["revision"], deadline)
                    if bounded_query
                    else query_method(query, settings["revision"])
                    if query_method
                    else retrieval.backend.call(
                        "embed", {"revision": settings["revision"], "texts": [query]}
                    )
                )
                value = vectors(response["vectors"], 1)[0]
            require(
                all(len(vector) == len(value) for vector in available.values()),
                "dependency_unavailable",
                503,
            )
            scores = {identity: dot(value, vector) for identity, vector in available.items()}
        except (Fault, OSError, TimeoutError, ValueError, TypeError, KeyError):
            return [by_id[identity] for identity in lexical_ids], False
    semantic = sorted(scores, key=lambda identity: (-scores[identity], identity))[:limit]
    if settings["mode"] == "vector":
        ranked = semantic or lexical_ids
    elif settings["mode"] == "hybrid":
        fused = {}
        for ranking in (lexical_ids, semantic):
            for position, identity in enumerate(ranking, 1):
                fused[identity] = fused.get(identity, 0) + 1 / (60 + position)
        ranked = sorted(fused, key=lambda identity: (-fused[identity], identity))[:limit]
    else:
        ranked = lexical_ids
    relevance = (
        {identity: fused[identity] for identity in ranked}
        if settings["mode"] == "hybrid"
        else {identity: scores[identity] for identity in ranked}
        if settings["mode"] == "vector" and semantic
        else {identity: 1 / (60 + index) for index, identity in enumerate(ranked, 1)}
    )
    ranked = order(
        combine(relevance, list(by_id.values()), plan.summaries, plan.now), list(by_id.values())
    )
    if settings["rerank"] and ranked:
        missing_text = [identity for identity in ranked if "text" not in by_id[identity]]
        if missing_text:
            with closing(store.readonly()) as db:
                rows = db.execute(
                    "SELECT id,text,content_hash FROM memory_documents WHERE id IN ("
                    + ",".join("?" for _ in missing_text)
                    + ")",
                    missing_text,
                )
                for row in rows:
                    if row["content_hash"] == by_id[row["id"]]["content_hash"]:
                        by_id[row["id"]] = dict(by_id[row["id"]], text=row["text"])
        try:
            require(time.monotonic() < deadline, "dependency_unavailable", 503)
            result = retrieval.backend.call(
                "rerank",
                {
                    "revision": settings["revision"],
                    "query": query,
                    "documents": [by_id[identity]["text"] for identity in ranked],
                },
            )["scores"]
            require(len(result) == len(ranked), "dependency_unavailable", 503)
            ranked = [
                identity
                for _, identity in sorted(zip(result, ranked), key=lambda item: (-item[0], item[1]))
            ]
        except (Fault, OSError, TimeoutError, ValueError, TypeError, KeyError):
            return [by_id[identity] for identity in ranked], False
    return [by_id[identity] for identity in ranked], not plan.corrupted and (
        settings["mode"] == "keyword" or len(available) == plan.document_count
    )


def missing(db, revision, limit=4):
    return [
        dict(row)
        for row in db.execute(
            "SELECT d.* FROM memory_documents d WHERE NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.document_id=d.id AND v.content_hash=d.content_hash AND v.embedding_revision=?) ORDER BY d.id LIMIT ?",
            (revision, limit),
        )
    ]


def write(db, document, revision, vector):
    current = db.execute(
        "SELECT content_hash FROM memory_documents WHERE id=?", (document["id"],)
    ).fetchone()
    if current is None or current[0] != document["content_hash"]:
        return False
    value = vectors([list(vector)], 1)[0]
    if binary_installed(db):
        db.execute(
            "INSERT INTO memory_vectors(document_id,content_hash,embedding_revision,dimension,vector,vector_bytes) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(document_id,content_hash,embedding_revision) "
            "DO UPDATE SET vector=excluded.vector,vector_bytes=excluded.vector_bytes,dimension=excluded.dimension",
            (
                document["id"],
                document["content_hash"],
                revision,
                len(value),
                canonical(value),
                pack(value),
            ),
        )
        return True
    db.execute(
        "INSERT INTO memory_vectors(document_id,content_hash,embedding_revision,dimension,vector) VALUES (?,?,?,?,?) ON CONFLICT(document_id,content_hash,embedding_revision) DO NOTHING",
        (document["id"], document["content_hash"], revision, len(value), canonical(value)),
    )
    return True
