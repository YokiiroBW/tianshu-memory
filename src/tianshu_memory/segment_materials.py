"""Accept genuine frontend/archived messages without creating singleton jobs."""

import hashlib
import json

from jsonschema import ValidationError

from . import consolidation_sources, segment_sources
from .domain import Fault, canonical, fingerprint, new_id, parse_time, require, source_selector, utc
from .memory_documents import capture, capture_sources
from .segment_migration import installed


def authorize(publisher, scope):
    require(publisher["authenticated_service"] == "companion")
    require(scope["actor_id"] not in publisher.get("blocked_role_actors", []))
    require(
        scope in publisher.get("allowed_scopes", [])
        or scope["actor_id"] in publisher.get("allowed_role_actors", [])
    )


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identity(actor, channel, message_id, revision, *, kind="core"):
    return "material:" + fingerprint([actor, channel, kind, message_id, revision])


def human_member(
    identity,
    kind,
    scope,
    account,
    binding_version,
    text,
    observed_at,
    *,
    reply_refs=(),
    mentioned_accounts=(),
    reality="real",
):
    return dict(
        member_id=identity,
        kind=kind,
        role="human",
        speaker=dict(kind="person", person_id=scope["person_id"], account=account),
        source_scope=scope,
        binding_version=binding_version,
        observed_at=observed_at,
        text=text,
        content_sha256=text_hash(text),
        reply_refs=list(reply_refs),
        mentioned_accounts=list(mentioned_accounts),
        reality=reality,
    )


class SegmentMaterials:
    def __init__(self, service, *, read_content=None):
        self.service = service
        self.read_content = read_content or self.read_originals

    def read_originals(self, event, *, with_metadata=False):
        """Read authenticated bodies; the caller retains its final source boundary."""
        service = self.service
        scope, sources = event["scope"], event["sources"]
        authority = service.source_authority
        request = dict(
            schema_version=1,
            request_id=new_id("extract-source"),
            mode="snapshot",
            selectors=[source_selector(source, scope) for source in sources],
            turn_ids=[event["aggregate_id"]],
            include_content=True,
        )
        snapshot = authority.transport.facts(request)
        authority._relations("source_snapshot", request, snapshot)
        access_request, access = authority._read_access(snapshot, None)
        authority._relations(
            "current_access", access_request, access, snapshot, utc(service.clock())
        )
        authority._relations("actor_event", event, snapshot["turns"][0], snapshot)
        authority._relations("current_actor_sources", scope, sources, snapshot)
        physicals = {canonical(p["key"]): p for p in snapshot["physicals"]}
        result = []
        for index, source in enumerate(sources):
            data = physicals[canonical(source_selector(source, scope)["key"])]["content"]
            text = "\n".join(p["text"] for p in data["parts"] if p["kind"] == "text")
            item = dict(index=index, text=text, sent_at=data["sent_at"])
            if with_metadata:
                item.update(
                    author=data["author"],
                    reply_refs=data["reply_refs"],
                    mentioned_accounts=data["mentioned_accounts"],
                )
            result.append(item)
        require(
            len(canonical(result).encode()) <= (262144 if with_metadata else 65536),
            "budget_exceeded",
            413,
        )
        return result

    def execute(self, request, publisher):
        service = self.service
        require(service.synchronized, "dependency_unavailable", 503)
        service.contracts.validate("memory-extraction#segment_material_request", request)
        with service.local_commit() as db:
            require(installed(db), "dependency_unavailable", 503)
        if "event" in request:
            receipt = service.consume(request["event"], publisher)
            require(
                receipt["state"] in {"accepted", "duplicate"},
                "scope_changed" if receipt["state"] == "scope_changed" else "version_conflict",
                409,
            )
        rows = (
            self._front(request, publisher)
            if "event" in request
            else self._observations(request, publisher)
        )
        return dict(
            schema_version=1,
            candidate_job_ref=None,
            materials=[
                dict(
                    material_ref=row["id"],
                    frame=json.loads(row["frame"]),
                    person_id=json.loads(row["scope"])["person_id"],
                    text_bytes=len(json.loads(row["payload"])["member"]["text"].encode()),
                    member_count=1,
                    source_key=row["source_key"],
                )
                for row in rows
            ],
        )

    def _save(self, db, member, source_key, kind, payload):
        service = self.service
        encoded = canonical(member["source_scope"])
        payload = dict(payload, member=member)
        digest = fingerprint(
            dict(
                scope=member["source_scope"],
                account=member["speaker"]["account"],
                text=member["text"],
                observed_at=member["observed_at"],
            )
        )
        previous = db.execute(
            "SELECT * FROM memory_materials WHERE id=?", (member["member_id"],)
        ).fetchone()
        if previous:
            require(
                previous["digest"] == digest and previous["scope"] == encoded,
                "idempotency_conflict",
                409,
            )
            return dict(previous)
        row = dict(
            id=member["member_id"],
            digest=digest,
            frame=canonical(segment_sources.frame(member["source_scope"])),
            scope=encoded,
            source_key=source_key,
            kind=kind,
            binding_version=member["binding_version"],
            payload=canonical(payload),
            verified=int(kind == "core"),
            created_at=utc(service.clock()),
        )
        db.execute("INSERT INTO memory_materials VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))
        return row

    def _front(self, request, publisher):
        service, event = self.service, request["event"]
        service.contracts.validate("conversation#committed_event", event)
        scope = event["scope"]
        authorize(publisher, scope)
        windows = request.get("context_windows", [])
        for window in windows:
            consolidation_sources.validate_window(window, scope, service.contracts)
        require(self.read_content is not None, "dependency_unavailable", 503)
        content = self.read_content(event, with_metadata=True)
        archive_materials = self._canonical_materials(event, content, publisher)
        conflict = False
        with service.operation(
            scope=scope,
            sources=event["sources"],
            event=event,
            material_ids=[row["id"] for row in archive_materials.values()],
        ) as db:
            sources = service._source_rows(db, event["sources"], scope, reality=event["reality"])
            capture_sources(service, db, event, content, sources)
            result = []
            for index, (source, row, body) in enumerate(
                zip(event["sources"], sources, content, strict=True)
            ):
                if index in archive_materials:
                    material = archive_materials[index]
                    fresh = db.execute(
                        "SELECT * FROM memory_materials WHERE id=?", (material["id"],)
                    ).fetchone()
                    require(
                        fresh is not None and segment_sources.current(service, db, fresh),
                        "scope_changed",
                        409,
                    )
                    member = json.loads(fresh["payload"])["member"]
                    if member["reality"] != row["reality"]:
                        service._invalidate_sources(
                            db, [material["source_key"]], suppress="classification_conflict"
                        )
                        conflict = True
                        continue
                    prior = db.execute(
                        "SELECT material_id FROM memory_material_aliases WHERE source_key=?",
                        (row["key"],),
                    ).fetchone()
                    require(prior is None or prior[0] == material["id"], "scope_changed", 409)
                    db.execute(
                        "INSERT OR IGNORE INTO memory_material_aliases VALUES (?,?)",
                        (row["key"], material["id"]),
                    )
                    result.append(self._capture_observation(db, fresh))
                    continue
                account = body["author"]
                binding = service._binding(db, account)
                require(
                    binding is not None and binding["person_id"] == scope["person_id"],
                    "scope_changed",
                    409,
                )
                key = source["message_key"]
                material_id = identity(
                    scope["actor_id"], key["channel"], key["message_id"], key["revision"]
                )
                member = human_member(
                    material_id,
                    "core",
                    scope,
                    account,
                    binding["version"],
                    body["text"],
                    body["sent_at"],
                    reply_refs=body["reply_refs"],
                    mentioned_accounts=body["mentioned_accounts"],
                    reality=row["reality"],
                )
                result.append(
                    self._save(
                        db,
                        member,
                        row["key"],
                        "core",
                        dict(
                            event=event,
                            source=source,
                            source_revision=row["revision"],
                            source_epoch=row["epoch"],
                        ),
                    )
                )
        if conflict:
            raise Fault("scope_changed", 409)
        return result

    def _canonical_materials(self, event, content, publisher):
        """Resolve all sources once; an existing proved alias cannot silently fork."""
        service, scope = self.service, event["scope"]
        resolved = self._resolve(event)
        references = list(
            dict.fromkeys(
                item["observation"]["source_ref"]
                for item in resolved
                if item["observation"] is not None
            )
        )
        result = {}
        with service.local_commit() as db:
            for index, item in enumerate(resolved):
                observation = item["observation"]
                if observation is None:
                    alias = db.execute(
                        "SELECT m.* FROM memory_material_aliases a JOIN memory_materials m ON m.id=a.material_id WHERE a.source_key=?",
                        (service.source_identity(item["source"], scope),),
                    ).fetchone()
                    if alias:
                        result[index] = dict(alias)
                    continue
                original = db.execute(
                    "SELECT * FROM observation_sources WHERE source_ref=?",
                    (observation["source_ref"],),
                ).fetchone()
                require(
                    original is not None
                    and original["digest"] == observation["source_digest"]
                    and original["scope_version"] == observation["scope_version"]
                    and original["archive_epoch"] == observation["archive_epoch"]
                    and original["instance_id"] == observation["instance_id"]
                    and original["self_id"] == observation["self_id"]
                    and original["conversation"] == observation["conversation_id"],
                    "dependency_unavailable",
                    503,
                )
        if references:
            materials = self._stage_observations(
                dict(observation_refs=references), publisher, expected_actor=scope["actor_id"]
            )
            by_ref = {
                json.loads(material["payload"])["archive"]["source_ref"]: material
                for material in materials
            }
            require(set(by_ref) == set(references), "dependency_unavailable", 503)
            for index, item in enumerate(resolved):
                if item["observation"] is not None:
                    result[index] = by_ref[item["observation"]["source_ref"]]
        for index, material in result.items():
            member = json.loads(material["payload"])["member"]
            require(
                material["scope"] == canonical(scope)
                and member["text"] == content[index]["text"]
                and member["speaker"]["account"] == content[index]["author"]
                and parse_time(member["observed_at"]) == parse_time(content[index]["sent_at"]),
                "scope_changed",
                409,
            )
        return result

    def _resolve(self, event):
        service = self.service
        originals = [dict(source=source, scope=event["scope"]) for source in event["sources"]]
        if getattr(service, "observations", None) is None:
            # Deployments without passive ingestion retain genuine Core sources.
            return [dict(item, observation=None) for item in originals]
        request = dict(
            operation="segment_resolve", actor_id=event["scope"]["actor_id"], sources=originals
        )
        try:
            service.contracts.validate(
                "memory-extraction#segment_observation_resolve_request", request
            )
            response = service.observations.verifier(request)
            service.contracts.validate(
                "memory-extraction#segment_observation_resolve_response", response
            )
        except ValidationError:
            raise Fault("dependency_unavailable", 503) from None
        require(
            response["valid"] is True
            and response["actor_id"] == request["actor_id"]
            and len(response["sources"]) == len(originals),
            "dependency_unavailable",
            503,
        )
        for original, actual in zip(originals, response["sources"], strict=True):
            require(
                all(actual[key] == value for key, value in original.items()),
                "dependency_unavailable",
                503,
            )
        return response["sources"]

    def context(self, request, publisher):
        """A few exact replies/preceding archive rows before a new window freezes.

        This is material selection, not another scheduler. Missing or already
        withdrawn optional raw is omitted here; every selected member later
        participates in the segment's complete ordinary source checkpoint.
        """
        service = self.service
        with service.local_commit() as db:
            identities = request["material_refs"] + request["context_refs"]
            materials = [
                db.execute("SELECT * FROM memory_materials WHERE id=?", (identity,)).fetchone()
                for identity in identities
            ]
            require(
                all(
                    row is not None and row["frame"] == canonical(request["frame"])
                    for row in materials
                ),
                "forbidden",
                403,
            )
            for row in materials:
                authorize(publisher, json.loads(row["scope"]))
            used_bytes = sum(
                len(json.loads(row["payload"])["member"]["text"].encode()) for row in materials
            )
            used_bytes += sum(
                len(member["text"].encode())
                for window in request["context_windows"]
                for member in window["members"]
            )
            capacity = max(
                0,
                min(
                    8,
                    32
                    - len(materials)
                    - sum(len(window["members"]) for window in request["context_windows"]),
                ),
            )
            groups, known_refs = {}, set()
            for row in materials:
                if row["kind"] != "observation":
                    continue
                value = json.loads(row["payload"])
                original = value["archive"]
                known_refs.add(original["source_ref"])
                if row["id"] not in request["material_refs"]:
                    continue
                raw = db.execute(
                    "SELECT received_at FROM observation_sources WHERE source_ref=?",
                    (original["source_ref"],),
                ).fetchone()
                event = original["event"]
                key = (
                    event["platform_id"],
                    event["self_id"],
                    event["conversation_id"],
                    value["archive_epoch"],
                )
                group = groups.setdefault(key, dict(before=raw[0], replies=[]))
                group["before"] = min(group["before"], raw[0])
                group["replies"].extend(event.get("message_context", {}).get("reply_ids", []))
            selected, omitted = [], False
            for (instance, bot, conversation, epoch), group in groups.items():
                replies = list(dict.fromkeys(group["replies"]))
                if capacity == 0:
                    omitted = True
                    continue
                wanted = replies[:capacity]
                omitted |= len(wanted) < len(replies)
                base = "o.instance_id=? AND o.self_id=? AND o.conversation=? AND o.archive_epoch=? AND o.state='active'"
                parameters = [instance, bot, conversation, epoch]
                candidates = []
                if wanted:
                    candidates.extend(
                        db.execute(
                            "SELECT o.* FROM observation_sources o WHERE "
                            + base
                            + " AND o.event_id IN ("
                            + ",".join("?" for _ in wanted)
                            + ") ORDER BY o.received_at DESC,o.source_ref LIMIT ?",
                            [*parameters, *wanted, capacity + 1],
                        ).fetchall()
                    )
                    omitted |= set(wanted) != {row["event_id"] for row in candidates}
                candidates.extend(
                    db.execute(
                        "SELECT o.* FROM observation_sources o WHERE "
                        + base
                        + " AND o.received_at<? ORDER BY o.received_at DESC,o.source_ref LIMIT ?",
                        [*parameters, group["before"], capacity + 1],
                    ).fetchall()
                )
                native_counts = {}
                for row in candidates:
                    native_counts.setdefault(row["event_id"], set()).add(row["source_ref"])
                for row in candidates:
                    if row["source_ref"] in known_refs or row["source_ref"] in selected:
                        continue
                    if len(native_counts[row["event_id"]]) != 1:
                        omitted = True
                        continue
                    payload = db.execute(
                        "SELECT payload FROM observation_payloads WHERE source_ref=?",
                        (row["source_ref"],),
                    ).fetchone()
                    binding = service._binding(
                        db, dict(namespace="qq", immutable_account_id=row["author"])
                    )
                    if payload is None or binding is None:
                        omitted = True
                        continue
                    archived = json.loads(payload[0])
                    physical = archived["event"]
                    scope = dict(request["frame"], person_id=binding["person_id"])
                    key = "observation:" + fingerprint(
                        [
                            request["frame"],
                            {
                                key: physical[key]
                                for key in (
                                    "platform_id",
                                    "self_id",
                                    "conversation_id",
                                    "account_id",
                                    "event_id",
                                    "revision",
                                )
                            },
                        ]
                    )
                    local = db.execute("SELECT state FROM sources WHERE key=?", (key,)).fetchone()
                    if (local is not None and local[0] != "active") or db.execute(
                        "SELECT 1 FROM suppression WHERE source_key=?", (key,)
                    ).fetchone():
                        omitted = True
                        continue
                    authorize(publisher, scope)
                    if len(selected) >= capacity or used_bytes + len(row["text"].encode()) > 49152:
                        omitted = True
                        continue
                    used_bytes += len(row["text"].encode())
                    selected.append(row["source_ref"])
        if not selected:
            return [], omitted
        try:
            rows = self._observations(
                dict(observation_refs=selected),
                publisher,
                expected_actor=request["frame"]["actor_id"],
            )
        except Fault as error:
            if error.code == "scope_changed" and error.status == 409:
                return [], True
            raise
        return [row["id"] for row in rows], omitted

    def context_windows(self, request, publisher):
        """Choose whole optional Core windows before the sealed input exists."""
        selected, omitted = [], False
        for window in request["context_windows"]:
            scope = window["manifest"]["scope"]
            authorize(publisher, scope)
            require(segment_sources.frame(scope) == request["frame"], "forbidden", 403)
            manifest = consolidation_sources.validate_window(window, scope, self.service.contracts)
            try:
                with self.service.operation(scope=scope, windows=[manifest]) as db:
                    consolidation_sources.local_sources(self.service, db, manifest)
            except Fault as error:
                if error.code == "scope_changed" and error.status == 409:
                    omitted = True
                    continue
                raise
            selected.append(window)
        return selected, omitted

    def _stage_observations(self, request, publisher, *, expected_actor=None):
        """Persist archive candidates; discovery never accepts source authority."""
        service = self.service
        with service.local_commit() as db:
            archives = []
            for ref in request["observation_refs"]:
                row = db.execute(
                    "SELECT o.*,p.payload FROM observation_sources o JOIN observation_payloads p ON p.source_ref=o.source_ref WHERE o.source_ref=?",
                    (ref,),
                ).fetchone()
                require(row is not None and row["state"] == "active", "not_found", 404)
                value = dict(row)
                original = json.loads(row["payload"])
                require(
                    fingerprint(original["event"]) == row["digest"], "dependency_unavailable", 503
                )
                value["payload"] = canonical(
                    dict(archive=original, archive_epoch=row["archive_epoch"])
                )
                archives.append(value)
        groups = {}
        for row in archives:
            groups.setdefault((row["instance_id"], row["self_id"], row["conversation"]), []).append(
                row
            )
        result = []
        for rows in groups.values():
            discovery = segment_sources.observation_request(rows, expected_actor)
            proof = segment_sources.check_platform(service, discovery)
            frame = segment_sources.check_frame(
                service, proof["actor_id"], proof["channel"], None, proof["delivery_channel"]
            )
            staged = []
            with service.local_commit() as db:
                for row, entry in zip(rows, proof["sources"], strict=True):
                    value = json.loads(row["payload"])
                    event = value["archive"]["event"]
                    account = dict(
                        namespace=event["namespace"], immutable_account_id=event["account_id"]
                    )
                    binding = service._binding(db, account)
                    require(binding is not None, "dependency_unavailable", 503)
                    scope = dict(frame, person_id=binding["person_id"])
                    authorize(publisher, scope)
                    material_id = identity(
                        frame["actor_id"],
                        proof["channel"],
                        canonical(entry["physical_key"]),
                        event["revision"],
                        kind="observation",
                    )
                    context = event.get("message_context", {})
                    member = human_member(
                        material_id,
                        "observation",
                        scope,
                        account,
                        binding["version"],
                        event["text"],
                        event["sent_at"],
                        reply_refs=context.get("reply_ids", []),
                        mentioned_accounts=[
                            dict(namespace="qq", immutable_account_id=item)
                            for item in context.get("mentioned_accounts", [])
                        ],
                    )
                    source_key = "observation:" + fingerprint([frame, entry["physical_key"]])
                    staged.append(
                        self._save(
                            db,
                            member,
                            source_key,
                            "observation",
                            dict(
                                value,
                                source_revision=event["revision"],
                                source_epoch=1,
                                channel=proof["channel"],
                                authority_owner=proof["authority"]["owner_id"],
                            ),
                        )
                    )
            result.extend(staged)
        return result

    def _capture_observation(self, db, current):
        source = db.execute(
            "SELECT * FROM sources WHERE key=?", (current["source_key"],)
        ).fetchone()
        member = json.loads(current["payload"])["member"]
        capture(db, source, member["text"], member["observed_at"])
        return dict(current)

    def _observations(self, request, publisher, *, expected_actor=None):
        service = self.service
        staged = self._stage_observations(request, publisher, expected_actor=expected_actor)
        scopes = list(dict.fromkeys(item["scope"] for item in staged))
        requirements = [
            dict(
                scope=json.loads(scope),
                material_ids=[item["id"] for item in staged if item["scope"] == scope],
            )
            for scope in scopes
        ]
        result = []
        # Keep every owner's complete historical coverage, not just the first
        # scope or summary-scopes collection. The same checkpoint proves the
        # entire staged union before one atomic episode projection transaction.
        with service.source_authority.bundle_operation(service, requirements) as db:
            for scope in scopes:
                for item in staged:
                    if item["scope"] != scope:
                        continue
                    current = db.execute(
                        "SELECT * FROM memory_materials WHERE id=?", (item["id"],)
                    ).fetchone()
                    require(
                        current is not None and segment_sources.current(service, db, current),
                        "scope_changed",
                        409,
                    )
                    result.append(self._capture_observation(db, current))
        return result
