"""Optional semantic retrieval over complete, already-authorized memory groups."""

import hashlib
import math
import threading
import time
from collections import OrderedDict
from contextlib import closing

from .domain import canonical, require
from .retrieval_scoring import combine, order
from .retrieval_vectors import dot, pack, unpack, vectors


def policy(value):
    require(isinstance(value, dict), "dependency_unavailable", 503)
    require(value.get("mode") in {"keyword", "vector", "hybrid"}, "dependency_unavailable", 503)
    require(type(value.get("rerank")) is bool, "dependency_unavailable", 503)
    require(
        type(value.get("candidates")) is int and 1 <= value["candidates"] <= 100,
        "dependency_unavailable",
        503,
    )
    require(
        all(
            isinstance(value.get(key), str) and 1 <= len(value[key]) <= 256
            for key in ("revision", "embedding_revision")
        ),
        "dependency_unavailable",
        503,
    )
    return value


class Retrieval:
    """Models never choose scope or factual authority; cache contains derived vectors only."""

    def __init__(self, backend):
        self.backend = backend
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.generation = 0
        self.store = None

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.cache.clear()

    def settings_for(self, request):
        kind, separator, _ = request["query_text"].partition(":")
        if not all(request["budget"].values()) or (separator and kind in {"field", "item"}):
            return None
        return self.settings()

    def settings(self):
        return policy(self.backend.call("settings", {}))

    def rank(self, query, eligible, lexical, settings):
        if not eligible:
            return []
        if self.store is not None:
            from .memory_migration import installed
            from .persistent_retrieval import rank

            with closing(self.store.readonly()) as db:
                persistent = installed(db)
                documents = []
                if persistent:
                    identities = [entry[0]["id"] for entry in eligible]
                    for offset in range(0, len(identities), 256):
                        batch = identities[offset : offset + 256]
                        documents.extend(
                            dict(row)
                            for row in db.execute(
                                "SELECT * FROM memory_documents WHERE group_id IN ("
                                + ",".join("?" for _ in batch)
                                + ")",
                                batch,
                            )
                        )
            if persistent:
                by_group = {entry[0]["id"]: entry for entry in eligible}
                ranked, _ = rank(
                    self,
                    self.store,
                    query,
                    documents,
                    ["fact:" + entry[0]["id"] for entry in lexical],
                    settings,
                )
                return [by_group[item["group_id"]] for item in ranked]
        with self.lock:
            generation = self.generation
        deadline = time.monotonic() + 10

        def call(operation, payload):
            require(time.monotonic() < deadline, "dependency_unavailable", 503)
            answer = self.backend.call(operation, payload)
            require(time.monotonic() < deadline, "dependency_unavailable", 503)
            return answer

        limit = settings["candidates"]
        by_id = {entry[0]["id"]: entry for entry in eligible}
        texts = {
            identity: canonical(
                [
                    {
                        key: unit.get(key)
                        for key in (
                            "statement",
                            "conditions",
                            "negations",
                            "valid_time",
                            "uncertainty",
                            "reality",
                        )
                    }
                    for unit in entry[2]
                ]
            )
            for identity, entry in by_id.items()
        }
        lexical_ids = [entry[0]["id"] for entry in lexical[:limit]]
        ranked = lexical_ids
        revision = settings["revision"]
        if settings["mode"] != "keyword":
            ids = list(by_id)
            # No private text or source locator is retained in the derived cache key.
            keys = [
                (
                    settings["embedding_revision"],
                    identity,
                    hashlib.sha256(texts[identity].encode()).hexdigest(),
                )
                for identity in ids
            ]
            with self.lock:
                cached = {key: self.cache[key] for key in keys if key in self.cache}
            missing = [(identity, key) for identity, key in zip(ids, keys) if key not in cached]
            for start in range(0, len(missing), 4):
                batch = missing[start : start + 4]
                answer = call(
                    "embed",
                    {"revision": revision, "texts": [texts[identity] for identity, _ in batch]},
                )
                computed = [pack(value) for value in vectors(answer["vectors"], len(batch))]
                cached.update((key, vector) for (_, key), vector in zip(batch, computed))
                # Keep completed batches even if this cold query reaches its deadline.
                with self.lock:
                    if generation == self.generation:
                        self.cache.update(
                            (key, vector) for (_, key), vector in zip(batch, computed)
                        )
                    while len(self.cache) > 4096:
                        self.cache.popitem(last=False)
            query_vector = vectors(
                call("embed", {"revision": revision, "texts": [query]})["vectors"], 1
            )[0]
            require(
                all(len(cached[key]) == len(query_vector) * 4 for key in keys),
                "dependency_unavailable",
                503,
            )
            with self.lock:
                if generation == self.generation:
                    self.cache.update(cached)
                while len(self.cache) > 4096:
                    self.cache.popitem(last=False)
            scores = {
                identity: dot(unpack(cached[key]), query_vector) for identity, key in zip(ids, keys)
            }
            semantic = sorted(ids, key=lambda identity: (-scores[identity], identity))[:limit]
            if settings["mode"] == "vector":
                ranked = semantic
            else:
                fused = {}
                for ranking in (lexical_ids, semantic):
                    for position, identity in enumerate(ranking, 1):
                        fused[identity] = fused.get(identity, 0) + 1 / (60 + position)
                ranked = sorted(fused, key=lambda identity: (-fused[identity], identity))[:limit]
        documents = [dict(id=identity, group_id=identity) for identity in by_id]
        relevance = (
            {identity: fused[identity] for identity in ranked}
            if settings["mode"] == "hybrid"
            else {identity: scores[identity] for identity in ranked}
            if settings["mode"] == "vector"
            else {identity: 1 / (60 + index) for index, identity in enumerate(ranked, 1)}
        )
        ranked = order(combine(relevance, documents), documents)
        if settings["rerank"] and ranked:
            scores = call(
                "rerank",
                {
                    "revision": revision,
                    "query": query,
                    "documents": [texts[identity] for identity in ranked],
                },
            )["scores"]
            require(
                isinstance(scores, list)
                and len(scores) == len(ranked)
                and all(type(n) in (int, float) and math.isfinite(n) for n in scores),
                "dependency_unavailable",
                503,
            )
            ranked = [
                identity
                for _, identity in sorted(zip(scores, ranked), key=lambda item: (-item[0], item[1]))
            ]
        return [by_id[identity] for identity in ranked]
