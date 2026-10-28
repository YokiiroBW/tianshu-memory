"""Actor query and one-level, episode-backed derived memories."""

import json
from datetime import timedelta
from uuid import uuid4

from ..domain import canonical, fingerprint, parse_time, require, terms, utc
from .facts import INTERNAL, permitted, source_refs

OPERATIONS = {"append_derived", "query", "check_memory", "withdraw_memory"}
RETENTION_DAYS = {"short": 7, "medium": 30, "long": 180}
MAX_CANDIDATES = 256


def invalidate_derived(db, episode_ids):
    ids = sorted(set(episode_ids))
    for offset in range(0, len(ids), 256):
        page = ids[offset : offset + 256]
        db.execute(
            "UPDATE actor_derived SET state='unavailable',revision=revision+1 WHERE state='active' AND id IN "
            "(SELECT derived_id FROM actor_derived_basis WHERE episode_id IN ("
            + ",".join("?" for _ in page)
            + "))",
            page,
        )


class Memories:
    def __init__(self, application):
        self.app = application

    def _rows(self, db, actor, refs):
        require(len({(ref["kind"], ref["id"]) for ref in refs}) == len(refs), "invalid_input", 400)
        found = {}
        for kind, table in (("episode", "actor_episodes"), ("derived", "actor_derived")):
            ids = [ref["id"] for ref in refs if ref["kind"] == kind]
            if ids:
                found.update(
                    ((kind, row["id"]), dict(row))
                    for row in db.execute(
                        "SELECT * FROM "
                        + table
                        + " WHERE actor_id=? AND id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        [actor, *ids],
                    )
                )
        return [(ref, found.get((ref["kind"], ref["id"]))) for ref in refs]

    def _basis(self, db, actor, rows):
        basis_refs, all_ids = {}, set()
        for ref, row in rows:
            if row is None or row["state"] != "active":
                continue
            key = ref["kind"], ref["id"]
            if ref["kind"] == "episode":
                basis_refs[key] = [dict(episode_id=row["id"], expected_revision=row["revision"])]
            else:
                basis_refs[key] = json.loads(row["payload"])["episode_refs"]
            all_ids.update(part["episode_id"] for part in basis_refs[key])
        episodes = {}
        ids = sorted(all_ids)
        for offset in range(0, len(ids), 256):
            page = ids[offset : offset + 256]
            episodes.update(
                (row["id"], dict(row))
                for row in db.execute(
                    "SELECT * FROM actor_episodes WHERE actor_id=? AND id IN ("
                    + ",".join("?" for _ in page)
                    + ")",
                    [actor, *page],
                )
            )
        return {
            key: [(part, episodes.get(part["episode_id"])) for part in refs]
            for key, refs in basis_refs.items()
        }

    @staticmethod
    def _refs(bases):
        return list(
            {
                canonical(source): source
                for basis in bases.values()
                for _, row in basis
                if row is not None and row["state"] == "active"
                for source in source_refs(json.loads(row["fact"]))
            }.values()
        )

    def _status(self, ref, row, basis, observed, context):
        if row is None:
            return "not_found", False
        if row["state"] != "active":
            return "withdrawn", False
        if row["revision"] != ref["revision"]:
            return "changed", False
        if ref["kind"] == "derived" and parse_time(row["expires_at"]) <= self.app.clock():
            return "unavailable", False
        if not basis or any(
            part is None
            or part["state"] != "active"
            or part["revision"] != expected["expected_revision"]
            for expected, part in basis
        ):
            return "withdrawn", False
        facts = [json.loads(part["fact"]) for _, part in basis]
        proofs = [observed[canonical(source)] for fact in facts for source in source_refs(fact)]
        readable = all(permitted(fact, context) for fact in facts) and all(
            proof["state"] == "current" and proof["actor_known"] and proof["read_allowed"]
            for proof in proofs
        )
        external = context["audience"]["kind"] != "actor_only"
        disclosure = external and readable and all(proof["disclosure_allowed"] for proof in proofs)
        return (
            ("current", disclosure)
            if readable and (not external or disclosure)
            else ("forbidden", False)
        )

    def _item(self, ref, row, basis):
        episodes = [
            dict(episode_id=part["id"], expected_revision=part["revision"]) for _, part in basis
        ]
        facts = [json.loads(part["fact"]) for _, part in basis]
        refs = list(
            {canonical(source): source for fact in facts for source in source_refs(fact)}.values()
        )
        payload = json.loads(row["payload"]) if ref["kind"] == "derived" else None
        return dict(
            ref=dict(kind=ref["kind"], id=row["id"], revision=row["revision"]),
            kind=payload["kind"] if payload else "episode",
            text=payload["text"] if payload else facts[0]["summary"],
            episode_refs=episodes,
            source_refs=refs,
            recorded_at=row["recorded_at"],
            expires_at=row["expires_at"] if payload else None,
        )

    def _receipt(self, db, request, caller, ref, state):
        result = dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            command=request["operation"],
            idempotency_key=request["idempotency_key"],
            state=state,
            ref=ref,
            domain_version=self.app.store.version(db),
        )
        return self.app._record_receipt(db, caller, request, result, "memory_command_receipt")

    def _withdraw(self, request, actor, caller, recheck):
        self.app._refresh(request, {}, [], INTERNAL, actor, recheck, caller)
        with self.app.store.transaction(write=True) as db:
            ref, row = self._rows(db, request["actor_id"], [request["ref"]])[0]
            require(row is not None, "not_found", 404)
            require(row["revision"] == ref["revision"], "version_conflict", 409)
            table = "actor_episodes" if ref["kind"] == "episode" else "actor_derived"
            db.execute(
                "UPDATE " + table + " SET state='withdrawn',revision=revision+1 WHERE id=?",
                (row["id"],),
            )
            if ref["kind"] == "episode":
                invalidate_derived(db, [row["id"]])
            return self._receipt(
                db, request, caller, {**ref, "revision": row["revision"] + 1}, "withdrawn"
            )

    def _append(self, request, actor, caller, recheck):
        refs = [
            dict(kind="episode", id=part["episode_id"], revision=part["expected_revision"])
            for part in request["episode_refs"]
        ]
        with self.app.store.transaction() as db:
            version = self.app.store.version(db)
            rows = self._rows(db, request["actor_id"], refs)
            bases = self._basis(db, request["actor_id"], rows)
        sources = self._refs(bases)
        require(len(sources) <= 64, "invalid_input", 400)
        context_before = self.app.authority.context(request, INTERNAL)
        observed, version, _ = self.app._prove(
            request,
            sources,
            INTERNAL,
            actor,
            caller,
            recheck,
            version,
            context_before,
        )
        payload = {
            key: request[key]
            for key in ("kind", "text", "episode_refs", "replaces", "importance", "retention")
        }
        identity = fingerprint(dict(actor=request["actor_id"], payload=payload))
        with self.app.store.transaction(write=True) as db:
            require(self.app.store.version(db) == version + 1, "source_changed", 409)
            rows = self._rows(db, request["actor_id"], refs)
            bases = self._basis(db, request["actor_id"], rows)
            require(
                all(
                    self._status(
                        ref, row, bases.get((ref["kind"], ref["id"]), []), observed, INTERNAL
                    )[0]
                    == "current"
                    for ref, row in rows
                ),
                "source_withdrawn",
                409,
            )
            old = db.execute("SELECT * FROM actor_derived WHERE identity=?", (identity,)).fetchone()
            if old is not None:
                require(
                    old["state"] == "active" and parse_time(old["expires_at"]) > self.app.clock(),
                    "source_withdrawn",
                    409,
                )
                return self._receipt(
                    db,
                    request,
                    caller,
                    dict(kind="derived", id=old["id"], revision=old["revision"]),
                    "duplicate",
                )
            replaces = self._rows(db, request["actor_id"], request["replaces"])
            require(
                all(
                    ref["kind"] == "derived"
                    and row is not None
                    and row["state"] == "active"
                    and row["revision"] == ref["revision"]
                    for ref, row in replaces
                ),
                "version_conflict",
                409,
            )
            identity = "derived:" + uuid4().hex
            recorded = self.app.clock()
            db.execute(
                "INSERT INTO actor_derived VALUES (?,?,1,'active',?,?,?,?)",
                (
                    identity,
                    request["actor_id"],
                    fingerprint(dict(actor=request["actor_id"], payload=payload)),
                    canonical(payload),
                    utc(recorded),
                    utc(recorded + timedelta(days=RETENTION_DAYS[request["retention"]])),
                ),
            )
            for part in request["episode_refs"]:
                db.execute(
                    "INSERT INTO actor_derived_basis VALUES (?,?,?,?)",
                    (
                        identity,
                        request["actor_id"],
                        part["episode_id"],
                        part["expected_revision"],
                    ),
                )
            for ref, _ in replaces:
                db.execute(
                    "UPDATE actor_derived SET state='superseded',revision=revision+1 WHERE id=?",
                    (ref["id"],),
                )
            return self._receipt(
                db, request, caller, dict(kind="derived", id=identity, revision=1), "accepted"
            )

    def _candidates(self, db, request, context):
        rows, truncated = [], False
        for kind, table in (("episode", "actor_episodes"), ("derived", "actor_derived")):
            if kind == "episode" and "episode" not in request["kinds"]:
                continue
            if kind == "derived" and set(request["kinds"]) == {"episode"}:
                continue
            found = list(
                db.execute(
                    "SELECT * FROM "
                    + table
                    + " WHERE actor_id=? AND state='active' ORDER BY recorded_at DESC,id DESC LIMIT ?",
                    (request["actor_id"], MAX_CANDIDATES + 1),
                )
            )
            truncated |= len(found) > MAX_CANDIDATES
            for row in found[:MAX_CANDIDATES]:
                if kind == "derived":
                    payload = json.loads(row["payload"])
                    if (
                        payload["kind"] not in request["kinds"]
                        or parse_time(row["expires_at"]) <= self.app.clock()
                    ):
                        continue
                rows.append((dict(kind=kind, id=row["id"], revision=row["revision"]), dict(row)))
        bases = self._basis(db, request["actor_id"], rows)
        words, ranked = set(terms(request["text"])), []
        for ref, row in rows:
            basis = bases.get((ref["kind"], ref["id"]), [])
            if not basis or any(
                part is None
                or part["state"] != "active"
                or part["revision"] != expected["expected_revision"]
                or not permitted(json.loads(part["fact"]), context)
                for expected, part in basis
            ):
                continue
            item = self._item(ref, row, basis)
            if len(item["source_refs"]) > 64:
                continue
            score = len(words & set(terms(item["text"])))
            if score:
                ranked.append((score, row["recorded_at"], ref["id"], ref, item))
        chosen, used = [], 0
        for _, _, _, ref, item in sorted(ranked, key=lambda part: part[:3], reverse=True):
            size = len(canonical(item).encode("utf-8"))
            if len(chosen) == request["limit"] or used + size > request["budget_bytes"]:
                truncated = True
                continue
            chosen.append(ref)
            used += size
        return chosen, truncated

    def execute(self, request, *, actor, caller, recheck):
        operation = request["operation"]
        if operation == "query" and request.get("mode") == "timeline":
            from .timeline import execute

            return execute(self, request, actor=actor, caller=caller, recheck=recheck)
        if operation == "withdraw_memory":
            return self._withdraw(request, actor, caller, recheck)
        if operation == "append_derived":
            return self._append(request, actor, caller, recheck)
        context = request["context"]
        context_before = self.app.authority.context(request, context)
        with self.app.store.transaction() as db:
            version = self.app.store.version(db)
            refs, omitted = (
                self._candidates(db, request, context)
                if operation == "query"
                else (request["memories"], False)
            )
            rows = self._rows(db, request["actor_id"], refs)
            bases = self._basis(db, request["actor_id"], rows)
        sources = self._refs(bases)
        require(len(sources) <= 64 * 64, "invalid_input", 400)
        observed, version, _ = self.app._prove(
            request,
            sources,
            context,
            actor,
            caller,
            recheck,
            version,
            context_before,
        )
        with self.app.store.transaction() as db:
            require(self.app.store.version(db) == version, "source_changed", 409)
            rows = self._rows(db, request["actor_id"], refs)
            bases = self._basis(db, request["actor_id"], rows)
            items = []
            for ref, row in rows:
                basis = bases.get((ref["kind"], ref["id"]), [])
                state, disclosure = self._status(ref, row, basis, observed, context)
                if operation == "query":
                    if state == "current":
                        items.append(self._item(ref, row, basis))
                    else:
                        omitted = True
                else:
                    items.append(
                        dict(
                            ref=ref,
                            current_revision=row["revision"] if row else None,
                            state=state,
                            read_allowed=state == "current",
                            disclosure_allowed=disclosure,
                        )
                    )
            result = dict(
                schema_version=1,
                request_id=request["request_id"],
                actor_id=request["actor_id"],
                role_version=request["role_version"],
                operation=operation,
                context=context,
                domain_version=self.app.store.version(db),
                checked_at=utc(self.app.clock()),
                items=items,
            )
            if operation == "query":
                result["omitted"] = omitted
            self.app.contracts.validate(operation + "_result", result)
            return result
