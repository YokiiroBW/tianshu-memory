"""Authenticated owner snapshots joined with Memory's durable authority transaction."""

import json
from contextlib import contextmanager

from .domain import (
    Fault,
    admission_key,
    canonical,
    fingerprint,
    new_id,
    require,
    source_selector,
    utc,
)


class LocalRevisionChanged(Exception):
    """Internal retry signal; never an HTTP error or a successful sync result."""


class SourceAuthority:
    def __init__(self, transport, contracts):
        require(contracts.source_version == "1.0.0", "dependency_unavailable", 503)
        self.transport, self.contracts, self.rules = transport, contracts, contracts.source_rules

    @staticmethod
    def revision(db):
        row = db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()
        ready = db.execute("SELECT value FROM metadata WHERE key='source_recovery'").fetchone()
        require(
            row is not None and ready is not None and ready[0] == "ready",
            "dependency_unavailable",
            503,
        )
        return int(row[0])

    def coverage(self, db, scope, sources, profile):
        self.revision(db)
        selected = {
            canonical(source_selector(s, scope)): source_selector(s, scope) for s in sources
        }
        # Exact text domain includes historical/tombstoned dependencies. Profile domain covers
        # every active public/current-group projection, including other authors and targets.
        if profile:
            rows = db.execute(
                "SELECT DISTINCT a.selector FROM source_admissions a JOIN lineage l ON l.source_key=a.key "
                "JOIN groups g ON g.id=l.group_id JOIN profile_shares p ON p.group_id=g.id "
                "WHERE g.state='active' AND p.actor_id=? AND (p.sharing='public_preference' OR "
                "(p.sharing='group_only' AND ?='group' AND p.conversation_id=?))",
                (scope["actor_id"], scope["audience"], scope["conversation_id"]),
            )
        else:
            rows = db.execute(
                "SELECT a.selector FROM source_admissions a JOIN sources s ON s.key=a.key WHERE s.scope=?",
                (canonical(scope),),
            )
        for row in rows:
            selected[row[0]] = json.loads(row[0])
        require(len(selected) <= 256, "dependency_unavailable", 503)
        return [selected[k] for k in sorted(selected)]

    def _relations(self, name, *args):
        try:
            return getattr(self.rules, name)(*args)
        except (ValueError, KeyError, TypeError):
            raise Fault("dependency_unavailable", 503) from None

    def barrier(
        self, service, scope, *, sources=(), context=None, profile=False, event=None, check=None
    ):
        for _ in range(3):
            with service.store.transaction() as db:
                m0 = self.revision(db)
                selectors = self.coverage(db, scope, sources, profile)
                if context is not None:
                    service._authorize(db, context, scope=scope)
            turn_ids = [event["aggregate_id"]] if event else [check["turn_id"]] if check else []
            request = dict(
                schema_version=1,
                request_id=new_id("facts"),
                mode="snapshot",
                selectors=selectors,
                turn_ids=turn_ids,
                include_content=False,
            )
            snapshot = self.transport.facts(request)
            self._relations("source_snapshot", request, snapshot)
            viewer = (
                None
                if context is None
                else {
                    "origin": {"assertion_ref": context["assertion_ref"]},
                    "scope": scope,
                }
            )
            access_request = dict(
                schema_version=1,
                request_id=new_id("access"),
                operation="current",
                admissions=snapshot["admissions"],
                viewer=viewer,
            )
            access = self.transport.current(access_request)
            self._relations(
                "current_access", access_request, access, snapshot, utc(service.clock())
            )
            final_request = dict(
                schema_version=1,
                request_id=new_id("head"),
                mode="head",
                selectors=[],
                turn_ids=[],
                include_content=False,
            )
            final = self.transport.facts(final_request)
            self._relations("source_snapshot", final_request, final)
            if snapshot["head"] != final["head"]:
                continue
            if context is not None:
                current = access["viewer_context"]
                require(
                    current["verified_account"] == context["verified_account"]
                    and current["verified_channel"] == context["verified_channel"],
                    "dependency_unavailable",
                    503,
                )
            observation = dict(
                request=request,
                snapshot=snapshot,
                access_request=access_request,
                access=access,
                final_head=final["head"],
            )
            try:
                result = self.sync(
                    service, observation, m0=m0, scope=scope, sources=sources, profile=profile
                )
            except LocalRevisionChanged:
                continue
            # Owner negatives are already committed. Bad/stale event or a later scope conflict
            # must never roll them back.
            if event:
                self._relations("actor_event", event, snapshot["turns"][0], snapshot)
            if check:
                turn = snapshot["turns"][0]
                require(
                    turn["scope"] == scope
                    and turn["turn_id"] == check["turn_id"]
                    and turn["input_revision"] == check["input_revision"]
                    and turn["input_sources"] == list(sources),
                    "dependency_unavailable",
                    503,
                )
                self._relations("current_actor_sources", scope, sources, snapshot)
            return result, access.get("viewer_context")
        raise Fault("dependency_unavailable", 503)

    def sync(self, service, observation, *, m0, scope, sources=(), profile=False):
        """memory.source_sync trusted application port; only the HTTPS barrier calls this.

        m0 and scope are local execution state captured before network reads, never wire
        authority. This method is not exposed by the HTTP app or the operator fixture CLI.
        """
        self.contracts.validate("sync-workflow#sync_input", observation)
        self._relations("sync_barrier", dict(observation, now=utc(service.clock())))
        with service.store.transaction() as db:
            if (
                self.revision(db) != m0
                or self.coverage(db, scope, sources, profile) != observation["request"]["selectors"]
            ):
                raise LocalRevisionChanged()
            viewer = observation["access"]["viewer_context"]
            if viewer is not None:
                service._authorize(db, viewer, scope=scope)
            result = self._apply(service, db, observation["snapshot"], observation["access"])
            self.contracts.validate("sync-workflow#sync_result", result)
        return result

    @contextmanager
    def operation(self, service, scope, **kwargs):
        for _ in range(3):
            result, viewer = self.barrier(service, scope, **kwargs)
            with service.store.transaction() as db:
                if self.revision(db) != result["local_revision"]:
                    continue
                if viewer is not None:
                    service._authorize(db, viewer, scope=scope)
                yield db
                return
        raise Fault("dependency_unavailable", 503)

    def _apply(self, service, db, snapshot, access):
        heads = {"core": snapshot["head"], "platform": access["head"]}
        for owner, head in heads.items():
            old = db.execute("SELECT * FROM owner_heads WHERE owner=?", (owner,)).fetchone()
            if old:
                require(
                    head["generation"] == old["generation"] and head["sequence"] >= old["sequence"],
                    "dependency_unavailable",
                    503,
                )
        physicals = {fingerprint(p["key"]): p for p in snapshot["physicals"]}
        grants = {fingerprint(g["selector"]): g for g in access["grants"]}
        # The last observed sequence is per object: another scope may already have advanced
        # the global head. At the same object sequence a changed owner fact is equivocation.
        for kind, owner, values in (
            ("physical", "core", physicals),
            ("admission", "core", {fingerprint(a["selector"]): a for a in snapshot["admissions"]}),
            ("turn", "core", {t["turn_id"]: t for t in snapshot["turns"]}),
            ("access", "platform", grants),
        ):
            for key, value in values.items():
                old = db.execute(
                    "SELECT * FROM source_observations WHERE kind=? AND key=?", (kind, key)
                ).fetchone()
                if old and old["sequence"] == heads[owner]["sequence"]:
                    require(old["payload"] == canonical(value), "dependency_unavailable", 503)
                db.execute(
                    "INSERT INTO source_observations VALUES (?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
                    "sequence=excluded.sequence,payload=excluded.payload "
                    "WHERE source_observations.sequence != excluded.sequence OR source_observations.payload != excluded.payload",
                    (kind, key, heads[owner]["sequence"], canonical(value)),
                )
        # Local binding is an independent authority. A remote allowed bit cannot mint a person.
        for admission in snapshot["admissions"]:
            physical = physicals[fingerprint(admission["selector"]["key"])]
            binding = service._binding(db, physical["author"])
            require(
                binding is not None
                and binding["person_id"] == admission["scope"]["person_id"]
                and binding["version"] == admission["binding_version"],
                "dependency_unavailable",
                503,
            )
            for retained in db.execute(
                "SELECT key,payload FROM source_admissions WHERE payload IS NOT NULL"
            ):
                if retained["key"] != fingerprint(admission["selector"]):
                    require(
                        json.loads(retained["payload"])["source"]["receipt_id"]
                        != admission["source"]["receipt_id"],
                        "dependency_unavailable",
                        503,
                    )
        invalid_keys = set()
        for key, physical in physicals.items():
            old = db.execute("SELECT * FROM physical_sources WHERE key=?", (key,)).fetchone()
            if old:
                require(physical["revision"] >= old["revision"], "dependency_unavailable", 503)
                require(
                    old["state"] != "withdrawn" or physical["state"] == "withdrawn",
                    "dependency_unavailable",
                    503,
                )
                if old["payload"] is not None:
                    saved = json.loads(old["payload"])
                    require(
                        all(
                            saved[k] == physical[k]
                            for k in ("key", "author", "audience", "conversation_id")
                        ),
                        "dependency_unavailable",
                        503,
                    )
                    if saved["revision"] == physical["revision"]:
                        require(
                            all(
                                saved[k] == physical[k]
                                for k in ("content_digest", "kind", "physical_receipt_id")
                            ),
                            "dependency_unavailable",
                            503,
                        )
                    if saved != physical:
                        invalid_keys.update(
                            r[0]
                            for r in db.execute(
                                "SELECT key FROM source_admissions WHERE physical_key=?", (key,)
                            )
                        )
            db.execute(
                "INSERT INTO physical_sources VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "payload=excluded.payload,revision=excluded.revision,state=excluded.state "
                "WHERE physical_sources.payload IS NOT excluded.payload OR physical_sources.revision != excluded.revision "
                "OR physical_sources.state != excluded.state",
                (key, canonical(physical), physical["revision"], physical["state"]),
            )
        # The physical negative broadcast includes all locally known actors, without obtaining
        # their positive grants or exposing their lineage in this actor's response.
        for key in invalid_keys:
            db.execute("UPDATE sources SET state='withdrawn',epoch=epoch+1 WHERE key=?", (key,))
        for admission in snapshot["admissions"]:
            key, pk = fingerprint(admission["selector"]), fingerprint(admission["selector"]["key"])
            physical, grant = physicals[pk], grants[key]
            source, scope = admission["source"], admission["scope"]
            old = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            suppression = db.execute(
                "SELECT 1 FROM suppression WHERE source_key=?", (key,)
            ).fetchone()
            active = (
                grant["state"] == "allowed"
                and physical["state"] == "active"
                and source["message_key"]["revision"] == physical["revision"]
                and admission["physical_receipt_id"] == physical["physical_receipt_id"]
                and physical["classification"]["value"] in {"real", "fictional"}
                and not suppression
            )
            state = "active" if active else "withdrawn"
            reality = physical["classification"]["value"]
            if old:
                require(old["scope"] == canonical(scope), "dependency_unavailable", 503)
                require(
                    source["message_key"]["revision"] >= old["revision"],
                    "dependency_unavailable",
                    503,
                )
                stored_admission = db.execute(
                    "SELECT payload FROM source_admissions WHERE key=?", (key,)
                ).fetchone()
                if (
                    stored_admission
                    and stored_admission[0]
                    and old["revision"] == source["message_key"]["revision"]
                ):
                    require(
                        json.loads(stored_admission[0]) == admission, "dependency_unavailable", 503
                    )
                changed = (
                    old["payload"] != canonical(source)
                    or old["reality"] != reality
                    or (old["state"] == "active" and state != "active")
                )
                epoch = old["epoch"]
                if changed and key not in invalid_keys:
                    invalid_keys.add(key)
                    epoch += 1
                values = (
                    source["message_key"]["revision"],
                    epoch,
                    state,
                    canonical(scope),
                    reality,
                    canonical(source),
                )
                if values != tuple(
                    old[k] for k in ("revision", "epoch", "state", "scope", "reality", "payload")
                ):
                    db.execute(
                        "UPDATE sources SET revision=?,epoch=?,state=?,scope=?,reality=?,payload=? WHERE key=?",
                        (*values, key),
                    )
            else:
                db.execute(
                    "INSERT INTO sources VALUES (?,?,1,?,?,?,?)",
                    (
                        key,
                        source["message_key"]["revision"],
                        state,
                        canonical(scope),
                        reality,
                        canonical(source),
                    ),
                )
            db.execute(
                "INSERT INTO source_admissions VALUES (?,?,?,?,?,1) ON CONFLICT(key) DO UPDATE SET "
                "payload=excluded.payload,verified=1 WHERE source_admissions.payload IS NOT excluded.payload OR source_admissions.verified != 1",
                (
                    key,
                    pk,
                    scope["actor_id"],
                    canonical(admission["selector"]),
                    canonical(admission),
                ),
            )
        groups = set()
        for key in invalid_keys:
            groups.update(
                r[0] for r in db.execute("SELECT group_id FROM lineage WHERE source_key=?", (key,))
            )
        service._invalidate(db, groups)
        service._invalidate_jobs(db, source_keys=invalid_keys)
        for owner, head in heads.items():
            db.execute(
                "INSERT INTO owner_heads VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                "generation=excluded.generation,sequence=excluded.sequence "
                "WHERE owner_heads.generation != excluded.generation OR owner_heads.sequence != excluded.sequence",
                (owner, head["generation"], head["sequence"]),
            )
        return {
            "local_revision": self.revision(db),
            "core_head": heads["core"],
            "platform_head": heads["platform"],
        }

    def verify(self, db, sources, scope):
        for source in sources:
            row = db.execute(
                "SELECT * FROM source_admissions WHERE key=?", (admission_key(source, scope),)
            ).fetchone()
            require(row is not None and row["verified"], "dependency_unavailable", 503)

    def current(self, db, rows):
        return bool(rows) and all(
            row["state"] == "active"
            and row["revision"] == row["required_revision"]
            and row["epoch"] == row["required_epoch"]
            and db.execute(
                "SELECT 1 FROM source_admissions WHERE key=? AND verified=1", (row["key"],)
            ).fetchone()
            and not db.execute(
                "SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)
            ).fetchone()
            for row in rows
        )
