"""One sealed conversation job, one frozen model input, one atomic person receipt."""

import copy
import json

from . import consolidation_sources, segment_records, segment_sources, target_rotation
from .domain import Fault, canonical, fingerprint, new_id, parse_time, require, utc
from .retention import decide
from .segment_materials import SegmentMaterials, authorize, human_member
from .segment_migration import installed


def _materials(db, identities):
    rows = {}
    for start in range(0, len(identities), 256):
        page = identities[start : start + 256]
        rows.update(
            (row["id"], dict(row))
            for row in db.execute(
                "SELECT * FROM memory_materials WHERE id IN (" + ",".join("?" for _ in page) + ")",
                page,
            )
        )
    require(set(identities) <= set(rows), "not_found", 404)
    return [rows[identity] for identity in identities]


def _leaf_rows(service, db, event):
    materials = _materials(db, event["material_ids"])
    require(
        segment_sources.current_many(service, db, materials) == set(event["material_ids"]),
        "scope_changed",
        409,
    )
    keys = list({row["source_key"] for row in materials})
    sources = segment_sources.dependency_rows(db, keys)
    require(set(sources) == set(keys), "scope_changed", 409)
    for window in event["context_windows"]:
        for key, (row, _) in consolidation_sources.local_sources(
            service, db, window["manifest"]
        ).items():
            sources[key] = row
    require(len(sources) <= 256, "budget_exceeded", 413)
    return sources


def _invalid_material_snapshot(db, materials):
    """Original versions plus a durable negative, never a positive source proof."""
    if not segment_sources.invalidated_many(db, materials):
        return None
    return {
        material["source_key"]: [payload["source_revision"], payload["source_epoch"]]
        for material in materials
        for payload in [json.loads(material["payload"])]
    }


def _current(service, db, event, snapshot):
    rows = _leaf_rows(service, db, event)
    require(set(rows) <= set(snapshot), "scope_changed", 409)
    complete = segment_sources.dependency_rows(db, list(snapshot))
    require(set(complete) == set(snapshot), "scope_changed", 409)
    required = [
        dict(row, required_revision=snapshot[key][0], required_epoch=snapshot[key][1])
        for key, row in complete.items()
    ]
    require(
        "window" in service.source_authority.current_many(db, {"window": required}),
        "scope_changed",
        409,
    )
    rows.update(complete)
    new_materials = {row["id"]: row for row in _materials(db, event["new_material_ids"])}
    for target in event["targets"]:
        require(
            all(
                new_materials[identity]["scope"] == canonical(target["scope"])
                for identity in target["new_member_ids"]
            ),
            "scope_changed",
            409,
        )
    return rows


def group_projection_current(service, db, event, snapshot, scope):
    """The original sealed dependency predicate, restricted to one public group frame."""
    expected = {key: scope[key] for key in ("actor_id", "audience", "conversation_id")}
    if expected["audience"] != "group" or event["frame"] != expected:
        return False
    try:
        rows = _current(service, db, event, snapshot)
    except Fault as error:
        if (error.code, error.status) in {("scope_changed", 409), ("not_found", 404)}:
            return False
        raise
    if any(
        {key: json.loads(row["scope"])[key] for key in expected} != expected
        for row in rows.values()
    ):
        return False
    windows = [window["manifest"] for window in event["context_windows"]]
    turns = {member["turn_id"] for window in windows for member in window["members"]}
    current_turns = {}
    for start in range(0, len(turns), 256):
        page = sorted(turns)[start : start + 256]
        current_turns.update(
            (row["key"], json.loads(row["payload"]))
            for row in db.execute(
                "SELECT key,payload FROM source_observations WHERE kind='turn' AND key IN ("
                + ",".join("?" for _ in page)
                + ")",
                page,
            )
        )
    return all(consolidation_sources.turns_current(window, current_turns) for window in windows)


def _public(manifest):
    return {
        key: manifest[key]
        for key in ("segment_id", "frame", "window_id", "members", "targets", "coverage")
    }


def _prepare_target(service, db, target, used_sources, history_budget):
    scope = target["scope"]
    summaries, refs, known = [], [], []
    for row in db.execute(
        "SELECT s.*,r.id AS record_id,r.version,r.payload FROM memory_summaries s "
        "JOIN groups g ON g.id=s.group_id JOIN records r ON r.group_id=g.id "
        "WHERE s.scope=? AND s.kind='summary' AND g.state='active' AND r.state='active' "
        "ORDER BY s.recorded_at DESC,s.group_id LIMIT 16",
        (canonical(scope),),
    ):
        if parse_time(row["expires_at"]) <= service.clock() or not service._group_current(
            db, row["group_id"]
        ):
            continue
        material = consolidation_sources.portrait_material(
            service, db, json.loads(row["manifest"]), scope
        )
        leaves = {
            value[0]
            for value in db.execute(
                "SELECT source_key FROM memory_summary_sources WHERE group_id=?", (row["group_id"],)
            )
        }
        cost = (
            (
                sum(len(item["text"].encode()) for item in material)
                + len(json.loads(row["payload"])["statement"].encode())
            )
            if material
            else 0
        )
        if material is None or len(used_sources | leaves) > 256 or cost > history_budget[0]:
            continue
        history_budget[0] -= cost
        used_sources.update(leaves)
        ref = dict(record_id=row["record_id"], record_version=row["version"])
        refs.append(ref)
        summaries.append(dict(ref, subject_person_id=scope["person_id"], subject_material=material))
        state = db.execute(
            "SELECT kind,state,valid_until FROM memory_segment_items WHERE group_id=?",
            (row["group_id"],),
        ).fetchone()
        known.append(
            dict(
                ref,
                text=json.loads(row["payload"])["statement"],
                reality=json.loads(row["payload"])["reality"],
                kind=state["kind"] if state else "experience",
                state=state["state"] if state else "described",
                valid_until=state["valid_until"] if state else None,
            )
        )
    if len(known) < 16:
        from .recall_index import eligible_groups

        candidates = db.execute(
            "SELECT g.* FROM groups g WHERE g.scope=? AND g.state='active' "
            "AND g.category IN ('identity','current_items','evidence') "
            "AND NOT EXISTS (SELECT 1 FROM memory_summaries s WHERE s.group_id=g.id) "
            "ORDER BY g.id DESC LIMIT 16",
            (canonical(scope),),
        ).fetchall()
        for group, _, units in eligible_groups(service, db, candidates, scope):
            leaves = {
                row[0]
                for row in db.execute(
                    "SELECT source_key FROM lineage WHERE group_id=?", (group["id"],)
                )
            }
            cost = sum(len(unit["statement"].encode()) for unit in units)
            if (
                len(known) + len(units) > 16
                or len(used_sources | leaves) > 256
                or cost > history_budget[0]
            ):
                continue
            history_budget[0] -= cost
            used_sources.update(leaves)
            known.extend(
                dict(
                    record_id=unit["record_id"],
                    record_version=unit["record_version"],
                    text=unit["statement"],
                    reality=unit["reality"],
                    kind="experience",
                    state="described",
                    valid_until=None,
                )
                for unit in units
            )
    pointer = db.execute(
        "SELECT revision FROM memory_portraits WHERE scope=?", (canonical(scope),)
    ).fetchone()
    return dict(
        target,
        known_memories=known,
        portrait_input=dict(
            subject_person_id=scope["person_id"],
            expected_revision=pointer[0] if pointer else 0,
            summary_refs=refs,
            summaries=summaries,
            current_text="",
        ),
    )


class Segments:
    def __init__(self, service):
        self.service = service

    def _job(self, db, job_id, publisher):
        require(installed(db), "dependency_unavailable", 503)
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        require(row is not None, "not_found", 404)
        require(dict(row).get("extraction_schema") == 6, "invalid_input", 400)
        event = json.loads(row["event"])
        for target in event["targets"]:
            authorize(publisher, target["scope"])
        return dict(row), event

    def _operation(self, event, job_id=None):
        with self.service.store.transaction() as db:
            row = (
                db.execute(
                    "SELECT source_snapshot FROM jobs WHERE id=? AND extraction_schema=6", (job_id,)
                ).fetchone()
                if job_id
                else None
            )
            keys = list(json.loads(row[0])) if row else []
        return self.service.operation(
            scope=event["scope"],
            material_ids=event["material_ids"],
            dependency_keys=keys,
            summary_scopes=[(target["scope"], False) for target in event["targets"]],
            windows=[window["manifest"] for window in event["context_windows"]],
        )

    def _prior_seal(self, db, request, publisher, digest):
        prior = db.execute(
            "SELECT * FROM memory_segments WHERE segment_id=?", (request["segment_id"],)
        ).fetchone()
        if prior:
            job, _ = self._job(db, prior["job_id"], publisher)
            require(prior["digest"] == digest, "idempotency_conflict", 409)
            return self._sealed(request["segment_id"], job)

    @staticmethod
    def _save_segment(db, request, digest, event, state, snapshot):
        job = dict(id=new_id("job"), state=state)
        db.execute(
            "INSERT INTO jobs(id,state,event,source_snapshot,extraction_schema) VALUES (?,?,?,?,6)",
            (job["id"], state, canonical(event), canonical(snapshot)),
        )
        db.execute(
            "INSERT INTO memory_segments VALUES (?,?,?,?)",
            (request["segment_id"], job["id"], digest, canonical(request["frame"])),
        )
        return job

    def _seal_invalidated(self, request, publisher, digest):
        with self.service.local_commit() as db:
            require(installed(db), "dependency_unavailable", 503)
            prior = self._prior_seal(db, request, publisher, digest)
            if prior:
                return prior
            materials = _materials(db, request["material_refs"] + request["context_refs"])
            snapshot = _invalid_material_snapshot(db, materials)
            if snapshot is None:
                return None
            event, _ = self._event(db, request, publisher)
            job = self._save_segment(db, request, digest, event, "stale_source", snapshot)
            self.service._emit(
                db,
                "memory.candidate_invalidated",
                dict(candidate_job_ref=job["id"], state=job["state"]),
            )
            return self._sealed(request["segment_id"], job)

    def seal(self, request, publisher):
        service = self.service
        require(service.synchronized, "dependency_unavailable", 503)
        service.contracts.validate("memory-extraction#segment_seal_request", request)
        digest = fingerprint(request)
        invalidated = self._seal_invalidated(request, publisher, digest)
        if invalidated:
            return invalidated
        # Automatic passive context is chosen only before this segment has a
        # claim. A replay always uses the original frozen input above.
        request = copy.deepcopy(request)
        selector = SegmentMaterials(service)
        request["context_windows"], omitted = selector.context_windows(request, publisher)
        context, archive_omitted = selector.context(request, publisher)
        request["context_refs"].extend(
            identity
            for identity in context
            if identity not in request["material_refs"] + request["context_refs"]
        )
        request["omitted_context"] |= omitted or archive_omitted
        with service.local_commit() as db:
            event, materials = self._event(db, request, publisher)
        try:
            return self._seal_current(request, publisher, digest, event, materials)
        except Fault as error:
            if error.code != "scope_changed" or error.status != 409:
                raise
            invalidated = self._seal_invalidated(request, publisher, digest)
            if invalidated is None:
                raise
            return invalidated

    def _event(self, db, request, publisher):
        service = self.service
        identities = request["material_refs"] + request["context_refs"]
        require(len(identities) == len(set(identities)), "invalid_input", 400)
        materials = _materials(db, identities)
        require(
            all(row["frame"] == canonical(request["frame"]) for row in materials),
            "forbidden",
            403,
        )
        targets, members, member_sources = {}, {}, {}
        context_materials = []
        for row in materials:
            member = json.loads(row["payload"])["member"]
            require(member["source_scope"] == json.loads(row["scope"]), "scope_changed", 409)
            authorize(publisher, member["source_scope"])
            members[member["member_id"]] = member
            member_sources[member["member_id"]] = row["source_key"]
            if row["id"] in request["material_refs"]:
                target = targets.setdefault(
                    row["scope"], dict(scope=member["source_scope"], new_member_ids=[])
                )
                target["new_member_ids"].append(row["id"])
        require(1 <= len(targets) <= 8, "budget_exceeded", 413)
        require(
            len({target["scope"]["person_id"] for target in targets.values()}) == len(targets),
            "invalid_input",
            400,
        )
        # Preserve the complete supplied Core window, including every BOT
        # dependency. A model's later citations never trim this closure.
        for window in request["context_windows"]:
            scope = window["manifest"]["scope"]
            authorize(publisher, scope)
            require(segment_sources.frame(scope) == request["frame"], "forbidden", 403)
            manifest = consolidation_sources.validate_window(window, scope, service.contracts)
            bodies = {member["member_id"]: member["text"] for member in window["members"]}
            for original in manifest["members"]:
                if original["role"] == "user":
                    from .segment_materials import identity

                    key = original["reference"]["source"]["message_key"]
                    identity_value = identity(
                        scope["actor_id"], key["channel"], key["message_id"], key["revision"]
                    )
                    original_key = service.source_identity(
                        original["reference"]["source"], original["source_scope"]
                    )
                    alias = db.execute(
                        "SELECT material_id FROM memory_material_aliases WHERE source_key=?",
                        (original_key,),
                    ).fetchone()
                    if alias:
                        identity_value = alias[0]
                        context_materials.append(identity_value)
                    member_sources[identity_value] = original_key
                    member = human_member(
                        identity_value,
                        "core",
                        original["source_scope"],
                        original["speaker"]["account"],
                        original["binding_version"],
                        bodies[original["member_id"]],
                        original["observed_at"],
                        reality=None,
                    )
                else:
                    member = dict(
                        member_id=original["member_id"],
                        kind="core",
                        role="bot",
                        speaker=original["speaker"],
                        source_scope=original["source_scope"],
                        binding_version=original["binding_version"],
                        observed_at=original["observed_at"],
                        text=bodies[original["member_id"]],
                        content_sha256=original["content_sha256"],
                        reply_refs=[],
                        mentioned_accounts=[],
                        reality="real",
                    )
                old = members.get(member["member_id"])
                require(
                    old is None
                    or all(
                        old[key] == member[key]
                        for key in (
                            "role",
                            "speaker",
                            "source_scope",
                            "binding_version",
                            "observed_at",
                            "text",
                            "content_sha256",
                        )
                    ),
                    "invalid_input",
                    400,
                )
                members.setdefault(member["member_id"], member)
        ordered = sorted(
            members.values(),
            key=lambda item: (parse_time(item["observed_at"]), item["member_id"]),
        )
        require(
            len(ordered) <= 32 and sum(len(item["text"].encode()) for item in ordered) <= 49152,
            "budget_exceeded",
            413,
        )
        event = dict(
            kind="conversation_segment",
            segment_id=request["segment_id"],
            frame=request["frame"],
            scope=next(iter(targets.values()))["scope"],
            material_ids=list(dict.fromkeys([*identities, *context_materials])),
            new_material_ids=request["material_refs"],
            members=ordered,
            targets=list(targets.values()),
            member_sources=member_sources,
            context_windows=request["context_windows"],
            omitted_context=request["omitted_context"],
        )
        return event, materials

    def _seal_current(self, request, publisher, digest, event, materials):
        service = self.service
        with self._operation(event) as db:
            prior = self._prior_seal(db, request, publisher, digest)
            if prior:
                return prior
            rows = _leaf_rows(service, db, event)
            for member in event["members"]:
                if member["role"] == "human":
                    member["reality"] = rows[event["member_sources"][member["member_id"]]][
                        "reality"
                    ]
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
            require(pending < service.max_pending_jobs, "budget_exceeded", 429)
            for identity_value in request["material_refs"]:
                claimed = db.execute(
                    "SELECT 1 FROM memory_segment_claims c JOIN jobs j ON j.id=c.job_id "
                    "WHERE c.material_id=? AND (j.state='pending' OR EXISTS (SELECT 1 FROM write_ledger w WHERE w.job_id=j.id))",
                    (identity_value,),
                ).fetchone()
                require(claimed is None, "idempotency_conflict", 409)
            new_keys = {
                row["source_key"] for row in materials if row["id"] in request["material_refs"]
            }
            aliases = sorted(segment_sources.equivalent_keys(db, new_keys))
            if aliases:
                marks = ",".join("?" for _ in aliases)
                current_aliases = {
                    row["key"]: row["revision"]
                    for row in db.execute(
                        "SELECT key,revision FROM sources WHERE key IN ("
                        + marks
                        + ") AND state='active'",
                        aliases,
                    )
                }
                require(
                    not any(
                        current_aliases.get(row["source_key"]) == row["revision"]
                        for row in db.execute(
                            "SELECT source_key,revision FROM source_writes WHERE source_key IN ("
                            + marks
                            + ")",
                            aliases,
                        )
                    ),
                    "idempotency_conflict",
                    409,
                )
                for prior_job in db.execute(
                    "SELECT source_snapshot FROM jobs WHERE state='pending' AND (extraction_schema IS NULL OR extraction_schema!=6)"
                ):
                    require(
                        not any(
                            current_aliases.get(key) == value[0]
                            for key, value in json.loads(prior_job[0]).items()
                        ),
                        "idempotency_conflict",
                        409,
                    )
            job = self._save_segment(
                db,
                request,
                digest,
                event,
                "pending",
                {key: [row["revision"], row["epoch"]] for key, row in rows.items()},
            )
            for identity_value in request["material_refs"]:
                db.execute(
                    "INSERT INTO memory_segment_claims VALUES (?,?)", (identity_value, job["id"])
                )
            service._emit(
                db,
                "memory.candidate_accepted",
                dict(candidate_job_ref=job["id"], extraction_schema=6),
            )
            return self._sealed(request["segment_id"], job)

    @staticmethod
    def _sealed(segment_id, job):
        return dict(
            schema_version=1,
            segment_id=segment_id,
            job_id=job["id"],
            state="completed"
            if job["state"] in {"committed", "skipped"}
            else "pending"
            if job["state"] == "pending"
            else "invalidated",
            candidate_job_ref=job["id"],
        )

    @staticmethod
    def _receipt(db, job_id, request):
        row = db.execute("SELECT * FROM write_ledger WHERE job_id=?", (job_id,)).fetchone()
        if row:
            if request["output"] is not None:
                require(
                    row["digest"] == fingerprint(request["output"]), "idempotency_conflict", 409
                )
            result = json.loads(row["result"])
            require(
                request["batch_ref"] in (None, result["batch_ref"]), "idempotency_conflict", 409
            )
            return result

    @staticmethod
    def _invalidated(job_id, batch):
        return dict(
            state="invalidated",
            extraction_schema=6,
            job_id=job_id,
            batch_ref=batch["id"] if batch else None,
            result=None,
        )

    def execute(self, request, publisher):
        service = self.service
        service.contracts.validate("memory-extraction#segment_process_request", request)
        require(service.synchronized, "dependency_unavailable", 503)
        with service.local_commit() as db:
            job, event = self._job(db, request["job_id"], publisher)
            receipt = self._receipt(db, job["id"], request)
            if receipt:
                return receipt
            batch = db.execute(
                "SELECT * FROM memory_batches WHERE job_id=?", (job["id"],)
            ).fetchone()
            if job["state"] != "pending":
                return self._invalidated(job["id"], batch)
            if request["batch_ref"] is not None:
                require(
                    batch is not None and batch["id"] == request["batch_ref"],
                    "idempotency_conflict",
                    409,
                )
        try:
            return self._process(request, publisher, event)
        except Fault as error:
            if error.code != "scope_changed" or error.status != 409:
                raise
            with service.local_commit() as db:
                job, _ = self._job(db, request["job_id"], publisher)
                receipt = self._receipt(db, job["id"], request)
                if receipt:
                    return receipt
                batch = db.execute(
                    "SELECT * FROM memory_batches WHERE job_id=?", (job["id"],)
                ).fetchone()
                if job["state"] == "pending":
                    db.execute("UPDATE jobs SET state='scope_changed' WHERE id=?", (job["id"],))
                    service._emit(
                        db,
                        "memory.candidate_invalidated",
                        dict(candidate_job_ref=job["id"], state="scope_changed"),
                    )
                return self._invalidated(job["id"], batch)

    def _process(self, request, publisher, event):
        service = self.service
        with self._operation(event, request["job_id"]) as db:
            job, event = self._job(db, request["job_id"], publisher)
            receipt = self._receipt(db, job["id"], request)
            if receipt:
                return receipt
            batch = db.execute(
                "SELECT * FROM memory_batches WHERE job_id=?", (job["id"],)
            ).fetchone()
            if job["state"] != "pending":
                return self._invalidated(job["id"], batch)
            source_rows = _current(service, db, event, json.loads(job["source_snapshot"]))
            if batch is None:
                require(
                    request["output"] is None and request["batch_ref"] is None, "not_found", 404
                )
                used = set(source_rows)
                history_budget = [49152]
                targets = [
                    _prepare_target(service, db, target, used, history_budget)
                    for target in target_rotation.order(db, event["targets"])
                ]
                # Earlier seals could include empty optional archive context.
                # No valid model window exists yet: omit only those members
                # before freezing the first batch, retaining the original job,
                # new-member coverage and complete source dependency checks.
                new_members = {
                    identity for target in targets for identity in target["new_member_ids"]
                }
                members = [
                    member
                    for member in event["members"]
                    if member["text"] or member["member_id"] in new_members
                ]
                omitted_empty_context = len(members) != len(event["members"])
                # All historical raw supplied to any target is part of this
                # one model input, even if no output later cites that history.
                complete = {
                    key: db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
                    for key in used
                }
                require(all(row is not None for row in complete.values()), "scope_changed", 409)
                frozen_sources = {
                    key: [row["revision"], row["epoch"]] for key, row in complete.items()
                }
                db.execute(
                    "UPDATE jobs SET source_snapshot=? WHERE id=?",
                    (canonical(frozen_sources), job["id"]),
                )
                manifest = dict(
                    schema_version=6,
                    segment_id=event["segment_id"],
                    frame=event["frame"],
                    members=members,
                    targets=targets,
                    coverage=dict(
                        complete_new=True,
                        omitted_context=event["omitted_context"] or omitted_empty_context,
                    ),
                )
                manifest["window_id"] = fingerprint(
                    {key: value for key, value in manifest.items() if key != "schema_version"}
                )
                manifest["context_windows"] = event["context_windows"]
                require(
                    len(canonical(_public(manifest)).encode()) <= 262144, "budget_exceeded", 413
                )
                service.contracts.validate("memory-extraction#segment_window", _public(manifest))
                batch = dict(
                    id=new_id("memory-batch"),
                    job_id=job["id"],
                    window_id=manifest["window_id"],
                    manifest=canonical(manifest),
                    portrait_input=canonical(dict(extraction_schema=6, targets=targets)),
                    portrait_basis=canonical(
                        [
                            ref
                            for target in targets
                            for ref in target["portrait_input"]["summary_refs"]
                        ]
                    ),
                    recorded_at=utc(service.clock()),
                )
                db.execute(
                    "INSERT INTO memory_batches VALUES (?,?,?,?,?,?,?)", tuple(batch.values())
                )
            else:
                batch = dict(batch)
            if request["output"] is None:
                return dict(
                    state="pending",
                    extraction_schema=6,
                    job_id=job["id"],
                    batch_ref=batch["id"],
                    segment_window=_public(json.loads(batch["manifest"])),
                )
            require(request["batch_ref"] == batch["id"], "idempotency_conflict", 409)
            return self._submit(db, job, event, batch, request, source_rows)

    def _submit(self, db, job, event, batch, request, source_rows):
        service, output = self.service, request["output"]
        service.contracts.validate("memory-extraction#segment_output", output)
        manifest = json.loads(batch["manifest"])
        targets = {target["scope"]["person_id"]: target for target in manifest["targets"]}
        results = output["results"]
        require(
            len(results) == len(targets)
            and {item["subject_person_id"] for item in results} == set(targets),
            "invalid_input",
            400,
        )
        require(sum(len(item["memories"]) for item in results) <= 32, "budget_exceeded", 413)
        members = {member["member_id"]: member for member in manifest["members"]}
        prior_groups, all_refs = {}, set()
        # Validate the whole model proposal before the first derived write.
        for result in results:
            target = targets[result["subject_person_id"]]
            require(result["covered_member_ids"] == target["new_member_ids"], "invalid_input", 400)
            allowed_refs = {
                canonical({key: value[key] for key in ("record_id", "record_version")})
                for value in target["known_memories"]
            }
            for memory in result["memories"]:
                require(
                    memory["text"].strip()
                    and set(memory["source_member_ids"]) <= set(members)
                    and set(memory["source_member_ids"]) & set(target["new_member_ids"]),
                    "invalid_input",
                    400,
                )
                require(
                    memory["operation"] != "replace" or memory["prior_refs"], "invalid_input", 400
                )
                own_new = set(memory["source_member_ids"]) & set(target["new_member_ids"])
                require(
                    len({members[item]["reality"] for item in own_new}) == 1, "invalid_input", 400
                )
                for ref in memory["prior_refs"]:
                    require(canonical(ref) in allowed_refs, "invalid_input", 400)
                    prior = db.execute(
                        "SELECT r.*,g.scope,g.state AS group_state FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                        (ref["record_id"],),
                    ).fetchone()
                    require(
                        prior is not None
                        and prior["scope"] == canonical(target["scope"])
                        and prior["version"] == ref["record_version"]
                        and prior["state"] == "active"
                        and prior["group_state"] == "active"
                        and service._group_current(db, prior["group_id"]),
                        "scope_changed",
                        409,
                    )
                    if memory["operation"] == "replace":
                        require(ref["record_id"] not in all_refs, "invalid_input", 400)
                        all_refs.add(ref["record_id"])
                        prior_groups.setdefault(prior["group_id"], set()).add(ref["record_id"])
        for identity, replaced in prior_groups.items():
            original = db.execute("SELECT members FROM groups WHERE id=?", (identity,)).fetchone()
            require(
                original is not None and replaced == set(json.loads(original[0])),
                "invalid_input",
                400,
            )
        for topic in output["topics"]:
            require(
                set(topic["person_ids"])
                <= {
                    member["speaker"]["person_id"]
                    for member in members.values()
                    if member["role"] == "human"
                }
                and set(topic["member_ids"]) <= set(members),
                "invalid_input",
                400,
            )
        receipts = []
        for result in results:
            target = targets[result["subject_person_id"]]
            scope = target["scope"]
            owned_rows = [
                source_rows[row["source_key"]] for row in _materials(db, target["new_member_ids"])
            ]
            memory_refs = []
            retained_keys = set()
            for memory in result["memories"]:
                # Replacing a prior derived experience does not make its old
                # prose the provenance of the new one. The exact old version
                # remains in history; only the provided new owner source wins.
                refs = memory["prior_refs"] if memory["operation"] == "append" else []
                actual_rows = [
                    source_rows[row["source_key"]]
                    for row in _materials(
                        db,
                        [
                            identity
                            for identity in target["new_member_ids"]
                            if identity in memory["source_member_ids"]
                        ],
                    )
                ]
                _, created, expiry = segment_records.write(
                    service,
                    db,
                    batch,
                    target,
                    memory,
                    actual_rows,
                    list(source_rows.values()),
                    refs=refs,
                )
                memory_refs.extend(created)
                for row in actual_rows:
                    retained_keys.add(row["key"])
                    segment_records.lifecycle(
                        db,
                        row,
                        decide(memory)[1],
                        max(
                            (members[item]["observed_at"] for item in target["new_member_ids"]),
                            key=parse_time,
                        ),
                        expiry,
                    )
            portrait_state, portrait_refs = "not_requested", []
            if result["portrait"] is not None:
                prepared = target["portrait_input"]
                pointer = db.execute(
                    "SELECT * FROM memory_portraits WHERE scope=?", (canonical(scope),)
                ).fetchone()
                revision = pointer["revision"] if pointer else 0
                portrait_state = "conflict"
                if (
                    prepared is not None
                    and revision == prepared["expected_revision"]
                    and result["portrait"].strip()
                    and len({row["reality"] for row in owned_rows}) == 1
                ):
                    # A stale optional portrait never discards legal memories.
                    basis = prepared["summary_refs"]
                    valid_basis = True
                    for ref in basis:
                        prior = db.execute(
                            "SELECT r.*,g.scope,g.state AS group_state FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                            (ref["record_id"],),
                        ).fetchone()
                        if (
                            prior is None
                            or prior["scope"] != canonical(scope)
                            or prior["version"] != ref["record_version"]
                            or prior["state"] != "active"
                            or prior["group_state"] != "active"
                            or not service._group_current(db, prior["group_id"])
                        ):
                            valid_basis = False
                            break
                    if valid_basis:
                        portrait = dict(
                            text=result["portrait"],
                            kind="experience",
                            state="described",
                            valid_until=None,
                            source_member_ids=target["new_member_ids"],
                            importance="normal",
                            retention="medium",
                        )
                        group, portrait_refs, _ = segment_records.write(
                            service,
                            db,
                            batch,
                            target,
                            portrait,
                            owned_rows,
                            list(source_rows.values()),
                            kind="portrait",
                            refs=basis,
                        )
                        if pointer:
                            segment_records.supersede(db, pointer["group_id"])
                        db.execute(
                            "INSERT INTO memory_portraits VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,group_id=excluded.group_id",
                            (canonical(scope), revision + 1, group),
                        )
                        portrait_state = "committed"
            for row in owned_rows:
                db.execute(
                    "INSERT OR IGNORE INTO source_writes VALUES (?,?,?,?)",
                    (row["key"], row["revision"], canonical(scope), job["id"]),
                )
            unreferenced_rows = [row for row in owned_rows if row["key"] not in retained_keys]
            if unreferenced_rows:
                effective = max(
                    (members[item]["observed_at"] for item in target["new_member_ids"]),
                    key=parse_time,
                )
                expiry = segment_records.expires(
                    service,
                    effective,
                    dict(
                        kind="experience", state="described", retention="medium", valid_until=None
                    ),
                )
                for row in unreferenced_rows:
                    segment_records.lifecycle(db, row, "normal", effective, expiry)
            receipts.append(
                dict(
                    subject_person_id=scope["person_id"],
                    scope=scope,
                    covered_member_ids=target["new_member_ids"],
                    memory_refs=memory_refs,
                    portrait_state=portrait_state,
                    portrait_refs=portrait_refs,
                )
            )
            if memory_refs or portrait_refs:
                service._bump(db, scope, history_changed=False)
            target_rotation.covered(db, scope, utc(service.clock()))
        for group in prior_groups:
            segment_records.supersede(db, group)
        service._emit(
            db, "memory.candidate_completed", dict(candidate_job_ref=job["id"], extraction_schema=6)
        )
        cursor = db.execute("SELECT MAX(position) FROM outbox").fetchone()[0]
        receipt = dict(
            state="completed",
            extraction_schema=6,
            job_id=job["id"],
            batch_ref=batch["id"],
            segment_id=event["segment_id"],
            results=receipts,
            topics=output["topics"],
            receipt_cursor=cursor,
        )
        service.contracts.validate("memory-extraction#segment_receipt", receipt)
        db.execute(
            "INSERT INTO write_ledger VALUES (?,?,?)",
            (job["id"], fingerprint(output), canonical(receipt)),
        )
        db.execute("UPDATE jobs SET state='committed' WHERE id=?", (job["id"],))
        return receipt
