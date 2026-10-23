"""Incremental document vectors, owned by the existing Retrieval abstraction."""

import json
import time

from .domain import Fault, canonical, require
from .retrieval_vectors import vectors


def rank(retrieval, store, query, documents, lexical_ids, settings, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 8
    by_id = {item["id"]: item for item in documents}
    limit = settings["candidates"]
    scores, available = {}, {}
    complete = True
    corrupted = []
    if settings["mode"] != "keyword" and documents:
        with store.readonly() as db:
            for offset in range(0, len(documents), 256):
                batch = documents[offset : offset + 256]
                for row in db.execute(
                    "SELECT document_id,content_hash,vector FROM memory_vectors WHERE embedding_revision=? AND document_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    [settings["embedding_revision"], *[item["id"] for item in batch]],
                ):
                    if row["content_hash"] == by_id[row["document_id"]]["content_hash"]:
                        try:
                            available[row["document_id"]] = vectors([json.loads(row["vector"])], 1)[
                                0
                            ]
                        except (Fault, ValueError, TypeError):
                            complete = False
                            corrupted.append(
                                (
                                    row["document_id"],
                                    row["content_hash"],
                                    settings["embedding_revision"],
                                    row["vector"],
                                )
                            )
        if corrupted:
            with store.transaction() as db:
                db.executemany(
                    "DELETE FROM memory_vectors WHERE document_id=? AND content_hash=? AND embedding_revision=? AND vector=?",
                    corrupted,
                )
        if available:
            try:
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
                scores = {
                    identity: sum(left * right for left, right in zip(value, vector, strict=True))
                    for identity, vector in available.items()
                }
            except (Fault, OSError, TimeoutError, ValueError, TypeError, KeyError):
                return [
                    by_id[identity] for identity in lexical_ids[:limit] if identity in by_id
                ], False
    semantic = sorted(scores, key=lambda identity: (-scores[identity], identity))[:limit]
    lexical_ids = [identity for identity in lexical_ids if identity in by_id][:limit]
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
    if settings["rerank"] and ranked:
        missing_text = [identity for identity in ranked if "text" not in by_id[identity]]
        if missing_text:
            with store.readonly() as db:
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
    return [by_id[identity] for identity in ranked], complete and (
        settings["mode"] == "keyword" or len(available) == len(documents)
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
    db.execute(
        "INSERT INTO memory_vectors VALUES (?,?,?,?,?) ON CONFLICT(document_id,content_hash,embedding_revision) DO NOTHING",
        (document["id"], document["content_hash"], revision, len(value), canonical(value)),
    )
    return True
