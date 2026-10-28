"""Actor source checkpoints, guarded commits and exact original command receipts."""

import json
from uuid import uuid4

from ..domain import canonical, fingerprint, now, require, utc
from . import lifecycle, search
from .facts import INTERNAL, identity, permitted, source_refs, version_tuple
from .memories import OPERATIONS as MEMORY_OPERATIONS
from .memories import Memories, invalidate_derived

COMMANDS = {
    "append_episode",
    "withdraw_episode",
    "apply_source_revision",
    "append_derived",
    "withdraw_memory",
    "revise_memory",
    "forget_memory",
    "maintain",
}


class Application:
    def __init__(self, store, contracts, authority, *, clock=now):
        self.store, self.contracts, self.authority, self.clock = store, contracts, authority, clock

    def _receipt(self, db, caller, request):
        command = request.get("command", request["operation"])
        row = db.execute(
            "SELECT digest,result FROM actor_receipts WHERE caller=? AND actor_id=? AND command=? AND idempotency_key=?",
            (caller, request["actor_id"], command, request["idempotency_key"]),
        ).fetchone()
        if row is None:
            return None
        if request["operation"] != "get_receipt":
            require(
                row[0]
                == fingerprint(
                    {key: value for key, value in request.items() if key != "request_id"}
                ),
                "idempotency_conflict",
                409,
            )
        result = json.loads(row[1])
        return (
            result
            if request["operation"] == "get_receipt"
            else {**result, "request_id": request["request_id"]}
        )

    def _save_receipt(self, db, caller, request, *, state, episode_id=None, revision=1):
        result = dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            command=request["operation"],
            idempotency_key=request["idempotency_key"],
            state=state,
            episode_id=episode_id,
            revision=revision,
            domain_version=self.store.version(db),
        )
        return self._record_receipt(db, caller, request, result, "command_receipt")

    def _record_receipt(self, db, caller, request, result, definition):
        self.contracts.validate(definition, result)
        digest = fingerprint({key: value for key, value in request.items() if key != "request_id"})
        db.execute(
            "INSERT INTO actor_receipts VALUES (?,?,?,?,?,?)",
            (
                caller,
                request["actor_id"],
                request["operation"],
                request["idempotency_key"],
                digest,
                canonical(result),
            ),
        )
        return result

    def _rows(self, db, request):
        refs = (
            request["episodes"]
            if request["operation"] == "check_episode"
            else [
                dict(
                    episode_id=request["episode_id"], expected_revision=request["expected_revision"]
                )
            ]
        )
        require(len({ref["episode_id"] for ref in refs}) == len(refs), "invalid_input", 400)
        return [
            (
                ref,
                db.execute(
                    "SELECT * FROM actor_episodes WHERE id=? AND actor_id=?",
                    (ref["episode_id"], request["actor_id"]),
                ).fetchone(),
            )
            for ref in refs
        ]

    def _observe(self, request, refs, context):
        result = self.authority.sources(request, refs, context=context)
        require(
            all(item["state"] != "unavailable" for item in result.values()),
            "dependency_unavailable",
            503,
        )
        return result

    def _refresh(self, request, before, refs, context, actor, recheck, caller):
        after = self._observe(request, refs, context) if refs else {}
        require(canonical(before) == canonical(after), "source_changed", 409)
        require(canonical(self.authority.actor(request)) == canonical(actor), "source_changed", 409)
        require(recheck() == caller, "forbidden", 403)
        return after

    def _source_changes(self, db, actor_id, observed):
        pairs = sorted(
            {
                (item["source"]["producer"], item["source"]["object_id"])
                for item in observed.values()
            }
        )
        heads, dependencies = {}, {}
        for offset in range(0, len(pairs), 128):
            page = pairs[offset : offset + 128]
            predicate = "(producer,object_id) IN (" + ",".join("(?,?)" for _ in page) + ")"
            parameters = [actor_id, *(value for pair in page for value in pair)]
            for row in db.execute(
                "SELECT * FROM actor_sources WHERE actor_id=? AND " + predicate, parameters
            ):
                heads[(row["producer"], row["object_id"])] = dict(row)
            predicate = predicate.replace("producer,object_id", "d.producer,d.object_id")
            for row in db.execute(
                "SELECT d.* FROM actor_dependencies d JOIN actor_episodes e ON e.id=d.episode_id WHERE d.actor_id=? AND e.state='active' AND "
                + predicate,
                parameters,
            ):
                dependencies.setdefault((row["producer"], row["object_id"]), []).append(dict(row))
        updates, invalidated = {}, set()
        for item in observed.values():
            source = item["source"]
            pair = source["producer"], source["object_id"]
            old = heads.get(pair)
            behind = (
                old is not None
                and old["generation"] == item["source_generation"]
                and item["source_sequence"] < old["sequence"]
            )
            require(not behind or item["state"] != "current", "dependency_unavailable", 503)
            changed = item["state"] != "current" or not item["actor_known"]
            for row in dependencies.get(pair, []):
                original = json.loads(row["source"])
                if (
                    original == source
                    and changed
                    or original != source
                    and item["state"] == "current"
                    and item["actor_known"]
                ):
                    invalidated.add(row["episode_id"])
            values = dict(
                actor_id=actor_id,
                producer=pair[0],
                object_id=pair[1],
                source=canonical(source),
                generation=item["source_generation"],
                sequence=item["source_sequence"],
                access_version=item["access_version"],
                state=item["state"],
            )
            if not behind and old != values:
                updates[pair] = tuple(values.values())
                heads[pair] = values
        return list(updates.values()), invalidated

    @staticmethod
    def _apply(db, changes):
        updates, invalidated = changes
        for episode_id in sorted(invalidated):
            db.execute(
                "UPDATE actor_episodes SET state='unavailable',revision=revision+1,fact='{}' WHERE id=? AND state='active'",
                (episode_id,),
            )
            search.remove(db, "episode", episode_id)
        invalidate_derived(db, invalidated)
        for row in updates:
            db.execute(
                "INSERT INTO actor_sources VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(actor_id,producer,object_id) DO UPDATE SET source=excluded.source,generation=excluded.generation,sequence=excluded.sequence,access_version=excluded.access_version,state=excluded.state",
                row,
            )

    def execute(self, request, *, caller, recheck):
        self.contracts.validate("request", request)
        require(recheck() == caller, "forbidden", 403)
        operation = request["operation"]
        require(
            operation
            in COMMANDS | MEMORY_OPERATIONS | {"get_episode", "check_episode", "get_receipt"},
            "invalid_input",
            400,
        )
        actor = self.authority.actor(request)
        with self.store.gate:
            if operation in COMMANDS or operation == "get_receipt":
                with self.store.transaction() as db:
                    receipt = self._receipt(db, caller, request)
                if receipt is not None:
                    self._refresh(request, {}, [], INTERNAL, actor, recheck, caller)
                    return receipt
                require(operation != "get_receipt", "not_found", 404)
            if operation in MEMORY_OPERATIONS:
                return Memories(self).execute(request, actor=actor, caller=caller, recheck=recheck)
            if operation == "withdraw_episode":
                self._refresh(request, {}, [], INTERNAL, actor, recheck, caller)
                with self.store.transaction(write=True) as db:
                    rows = self._rows(db, request)
                    row = rows[0][1]
                    require(row is not None, "not_found", 404)
                    require(
                        row["revision"] == request["expected_revision"], "version_conflict", 409
                    )
                    lifecycle.forget(
                        db,
                        request["actor_id"],
                        dict(kind="episode", id=row["id"], revision=row["revision"]),
                    )
                    return self._save_receipt(
                        db,
                        caller,
                        request,
                        state="withdrawn",
                        episode_id=row["id"],
                        revision=row["revision"] + 1,
                    )
            if operation in {"append_episode", "apply_source_revision"}:
                refs, context, rows = [request["source"]], INTERNAL, []
            else:
                context = request["context"]
                with self.store.transaction() as db:
                    rows = self._rows(db, request)
                refs = list(
                    {
                        canonical(ref): ref
                        for _, row in rows
                        if row is not None and row["state"] == "active"
                        for ref in source_refs(json.loads(row["fact"]))
                    }.values()
                )
            with self.store.transaction() as db:
                initial_version = self.store.version(db)
            context_before = self.authority.context(request, context)
            facts = (
                self.authority.sources(request, refs, facts=True)
                if operation == "append_episode"
                else {}
            )
            if facts:
                fact = facts[canonical(refs[0])]
                require(fact["relation"] == request["perspective"], "forbidden", 403)
                for content in fact["content_reads"]:
                    require(
                        all(part["end"] >= part["start"] for part in content["coverage"]),
                        "dependency_unavailable",
                        503,
                    )
                refs = list({canonical(ref): ref for ref in source_refs(fact)}.values())
            require(len(refs) <= 64 * 33, "invalid_input", 400)
            observed, initial_version, invalidated = self._prove(
                request,
                refs,
                context,
                actor,
                caller,
                recheck,
                initial_version,
                context_before,
                fact=fact if facts else None,
            )
            if operation == "append_episode":
                require(
                    all(
                        item["state"] == "current" and item["actor_known"] and item["read_allowed"]
                        for item in observed.values()
                    ),
                    "source_withdrawn",
                    409,
                )
                key = identity(request["actor_id"], request["source"], request["perspective"])
                with self.store.transaction(write=True) as db:
                    require(self.store.version(db) == initial_version + 1, "source_changed", 409)
                    row = db.execute(
                        "SELECT id,revision,state,fact FROM actor_episodes WHERE identity=?", (key,)
                    ).fetchone()
                    if row is not None:
                        require(
                            row[2] == "active" and json.loads(row[3]) == fact,
                            "source_withdrawn",
                            409,
                        )
                        return self._save_receipt(
                            db,
                            caller,
                            request,
                            state="duplicate",
                            episode_id=row[0],
                            revision=row[1],
                        )
                    episode_id = "episode:" + uuid4().hex
                    db.execute(
                        "INSERT INTO actor_episodes VALUES (?,?,1,'active',?,?,?)",
                        (episode_id, request["actor_id"], key, canonical(fact), utc(self.clock())),
                    )
                    search.index(db, request["actor_id"], "episode", episode_id, fact["summary"])
                    for item in observed.values():
                        source = item["source"]
                        db.execute(
                            "INSERT INTO actor_dependencies VALUES (?,?,?,?,?,?,?,?)",
                            (
                                episode_id,
                                request["actor_id"],
                                source["producer"],
                                source["object_id"],
                                canonical(source),
                                item["source_generation"],
                                item["source_sequence"],
                                item["access_version"],
                            ),
                        )
                    return self._save_receipt(
                        db, caller, request, state="accepted", episode_id=episode_id
                    )
            if operation == "apply_source_revision":
                with self.store.transaction(write=True) as db:
                    require(self.store.version(db) == initial_version + 1, "source_changed", 409)
                    return self._save_receipt(
                        db,
                        caller,
                        request,
                        state="invalidated"
                        if invalidated
                        or any(item["state"] != "current" for item in observed.values())
                        else "accepted",
                    )
            with self.store.transaction() as db:
                require(self.store.version(db) == initial_version, "source_changed", 409)
                rows = self._rows(db, request)
                if operation == "get_episode":
                    row = rows[0][1]
                    require(row is not None, "not_found", 404)
                    require(
                        row["revision"] == request["expected_revision"], "version_conflict", 409
                    )
                    require(row["state"] == "active", "source_withdrawn", 409)
                    fact = json.loads(row["fact"])
                    require(
                        permitted(fact, context)
                        and all(
                            item["read_allowed"]
                            and (
                                context["audience"]["kind"] == "actor_only"
                                or item["disclosure_allowed"]
                            )
                            for item in observed.values()
                        ),
                        "forbidden",
                        403,
                    )
                    result = dict(
                        schema_version=1,
                        request_id=request["request_id"],
                        actor_id=request["actor_id"],
                        role_version=request["role_version"],
                        operation=operation,
                        context=context,
                        domain_version=self.store.version(db),
                        episode=dict(
                            episode_id=row["id"],
                            revision=row["revision"],
                            actor_id=row["actor_id"],
                            state="active",
                            fact=fact,
                            recorded_at=row["recorded_at"],
                        ),
                    )
                else:
                    items = []
                    for ref, row in rows:
                        state, disclosure = "not_found", False
                        if row is not None:
                            state = (
                                "withdrawn"
                                if row["state"] != "active"
                                else "changed"
                                if row["revision"] != ref["expected_revision"]
                                else "current"
                            )
                            if state == "current":
                                fact = json.loads(row["fact"])
                                current = [observed[canonical(ref)] for ref in source_refs(fact)]
                                disclosure = (
                                    context["audience"]["kind"] != "actor_only"
                                    and all(item["disclosure_allowed"] for item in current)
                                    and permitted(fact, context)
                                )
                                if (
                                    not all(item["read_allowed"] for item in current)
                                    or not permitted(fact, context)
                                    or context["audience"]["kind"] != "actor_only"
                                    and not disclosure
                                ):
                                    state, disclosure = "forbidden", False
                        items.append(
                            dict(
                                episode_id=ref["episode_id"],
                                expected_revision=ref["expected_revision"],
                                current_revision=row["revision"] if row is not None else None,
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
                        domain_version=self.store.version(db),
                        checked_at=utc(self.clock()),
                        items=items,
                    )
                self.contracts.validate(operation + "_result", result)
                return result

    def _prove(
        self, request, refs, context, actor, caller, recheck, version, context_before, *, fact=None
    ):
        """One current source/context checkpoint for episode and derived operations."""
        before = self._observe(request, refs, context) if refs else {}
        if fact is not None:
            require(
                version_tuple(fact) == version_tuple(before[canonical(fact["source"])]),
                "source_changed",
                409,
            )
        observed = self._refresh(request, before, refs, context, actor, recheck, caller)
        require(
            canonical(self.authority.context(request, context)) == canonical(context_before),
            "source_changed",
            409,
        )
        invalidated = False
        # Stable negatives commit even when the requested positive operation is rejected.
        if observed:
            with self.store.transaction() as db:
                require(self.store.version(db) == version, "source_changed", 409)
                changes = self._source_changes(db, request["actor_id"], observed)
                invalidated = bool(changes[1])
            if any(changes):
                with self.store.transaction(write=True) as db:
                    require(self.store.version(db) == version + 1, "source_changed", 409)
                    self._apply(db, changes)
                    version = self.store.version(db)
        return observed, version, invalidated

    def close(self):
        try:
            self.authority.close()
        finally:
            self.store.close()
