"""Contextual experience summaries and revisable portraits on the existing jobs/ledger."""

import json
from datetime import timedelta

from . import consolidation_sources as dependencies
from .consolidation_migration import installed
from .domain import canonical, fingerprint, new_id, parse_time, require, utc
from .memory_documents import index_group, remove_groups
from .workflow import _WorkflowBase

SOURCE_BUDGET = 256


def record_refs(db, group_id):
    return [
        dict(record_id=row["id"], record_version=row["version"])
        for row in db.execute(
            "SELECT id,version FROM records WHERE group_id=? ORDER BY id", (group_id,)
        )
    ]


def bound_portrait(batch, scope):
    """The existing JSON column binds new batches without rewriting old inputs."""
    value = json.loads(batch["portrait_input"])
    if not isinstance(value, dict) or "input" not in value:
        return False, None
    require(
        set(value) == {"subject_person_id", "input"}
        and value["subject_person_id"] == scope["person_id"],
        "scope_changed",
        409,
    )
    return True, value["input"]


def portrait_input(service, db, scope, window_sources):
    encoded = canonical(scope)
    pointer = db.execute("SELECT * FROM memory_portraits WHERE scope=?", (encoded,)).fetchone()
    inherited, refs, summaries, current_text = {}, {}, [], ""
    used = set(window_sources)

    def fits(group_id):
        keys = {
            row[0]
            for row in db.execute(
                "SELECT source_key FROM memory_summary_sources WHERE group_id=?", (group_id,)
            )
        }
        if len(used | keys) > SOURCE_BUDGET:
            return False
        used.update(keys)
        return True

    for row in db.execute(
        "SELECT s.group_id,s.manifest FROM memory_summaries s JOIN groups g ON g.id=s.group_id WHERE s.scope=? AND s.kind='summary' AND g.state='active' ORDER BY s.recorded_at DESC,s.group_id LIMIT 16",
        (encoded,),
    ):
        if not service._group_current(db, row["group_id"]):
            continue
        material = dependencies.subject_material(service, db, json.loads(row["manifest"]), scope)
        if material is None or not fits(row["group_id"]):
            continue
        for ref in record_refs(db, row["group_id"]):
            refs[canonical(ref)] = ref
            summaries.append(
                dict(ref, subject_person_id=scope["person_id"], subject_material=material)
            )
    if not refs:
        return None, []
    # The old portrait is an optional whole input. Never trim leaves from text
    # already supplied to the model; rebuild from recent whole summaries instead.
    prior_batch = (
        db.execute(
            "SELECT b.*,j.event AS batch_event FROM memory_summaries s JOIN memory_batches b ON b.window_id=s.window_id "
            "JOIN jobs j ON j.id=b.job_id WHERE s.group_id=? AND s.scope=?",
            (pointer["group_id"], encoded),
        ).fetchone()
        if pointer
        else None
    )
    if (
        pointer
        and prior_batch is not None
        and json.loads(prior_batch["batch_event"])["scope"] == scope
        and bound_portrait(prior_batch, scope)[0]
        and service._group_current(db, pointer["group_id"])
        and fits(pointer["group_id"])
    ):
        prior = db.execute(
            "SELECT summary_refs FROM memory_summaries WHERE group_id=?", (pointer["group_id"],)
        ).fetchone()
        inherited.update((canonical(ref), ref) for ref in json.loads(prior[0]))
        current_text = "\n".join(
            json.loads(row[0])["statement"]
            for row in db.execute(
                "SELECT payload FROM records WHERE group_id=? AND state='active'",
                (pointer["group_id"],),
            )
        )
    for ref in refs.values():
        row = db.execute(
            "SELECT payload,version,state FROM records WHERE id=?", (ref["record_id"],)
        ).fetchone()
        require(
            row is not None
            and row["state"] == "active"
            and row["version"] == ref["record_version"],
            "scope_changed",
            409,
        )
    inherited.update(refs)
    return dict(
        subject_person_id=scope["person_id"],
        expected_revision=pointer["revision"] if pointer else 0,
        summary_refs=list(refs.values()),
        summaries=summaries,
        current_text=current_text,
    ), list(inherited.values())


class Consolidation:
    def __init__(self, extraction):
        self.extraction, self.service = extraction, extraction.service
        self.writer = _WorkflowBase(self.service)

    def _job(self, db, request, publisher):
        require(installed(db), "dependency_unavailable", 503)
        require(request["job_ids"] == [request["job_id"]], "invalid_input", 400)
        row = db.execute("SELECT * FROM jobs WHERE id=?", (request["job_id"],)).fetchone()
        require(row is not None, "not_found", 404)
        event = json.loads(row["event"])
        scope = event["scope"]
        require(publisher["authenticated_service"] == "companion")
        require(scope["actor_id"] not in publisher.get("blocked_role_actors", []))
        require(
            scope in publisher["allowed_scopes"]
            or scope["actor_id"] in publisher.get("allowed_role_actors", [])
        )
        require(dict(row).get("extraction_schema") in (None, 5), "invalid_input", 400)
        return dict(row), event

    @staticmethod
    def _completed(db, batch, scope):
        old = db.execute(
            "SELECT result FROM write_ledger WHERE job_id=?", (batch["job_id"],)
        ).fetchone()
        if old:
            return dict(
                state="completed",
                extraction_schema=5,
                batch_ref=batch["id"],
                job_ids=[batch["job_id"]],
                result=dict(json.loads(old[0]), subject_person_id=scope["person_id"]),
            )

    def _retire_unbound(self, db, job, batch, scope):
        if bound_portrait(batch, scope)[0]:
            return False
        if job["state"] == "pending":
            db.execute("UPDATE jobs SET state='scope_changed' WHERE id=?", (job["id"],))
            self.service._emit(
                db,
                "memory.candidate_invalidated",
                dict(candidate_job_ref=job["id"], state="scope_changed"),
            )
        return True

    @staticmethod
    def _invalidated(job_id):
        return dict(state="invalidated", extraction_schema=5, job_ids=[job_id], result=None)

    def execute(self, request, publisher):
        service = self.service
        service.contracts.validate("memory-extraction#process_request", request)
        with service.local_commit() as db:
            job, event = self._job(db, request, publisher)
            batch = db.execute(
                "SELECT * FROM memory_batches WHERE job_id=?", (job["id"],)
            ).fetchone()
            if batch is not None:
                if request["output"] is not None:
                    require(request["batch_ref"] == batch["id"], "idempotency_conflict", 409)
                old = self._completed(db, batch, event["scope"])
                if old is not None:
                    if request["output"] is not None:
                        saved = db.execute(
                            "SELECT digest FROM write_ledger WHERE job_id=?",
                            (job["id"],),
                        ).fetchone()
                        require(
                            saved[0] == fingerprint(request["output"]), "idempotency_conflict", 409
                        )
                    return old
                if self._retire_unbound(db, job, batch, event["scope"]):
                    return self._invalidated(job["id"])
            if job["state"] != "pending":
                return self._invalidated(job["id"])
            if job.get("extraction_schema") is None:
                db.execute("UPDATE jobs SET extraction_schema=5 WHERE id=?", (job["id"],))
            if batch is None and request["output"] is None and request["window"] is None:
                # This is a receipt lookup, not a new window or a source read.
                # A competing request for this same job reuses its frozen window
                # and actual receipt when the original window is resent.
                return dict(state="unclaimed", extraction_schema=5, job_ids=request["job_ids"])
        if request["output"] is None:
            try:
                return self._read(
                    request, publisher, event, dict(batch) if batch is not None else None
                )
            except dependencies.WindowChanged as error:
                if batch is not None or not error.new_inputs_valid or not error.member_ids:
                    raise
                manifest = request["window"]["manifest"]
                optional = {
                    member["member_id"]
                    for member in manifest["members"]
                    if member["purpose"] == "context"
                }
                if (
                    error.window_id != manifest["window_id"]
                    or not set(error.member_ids) <= optional
                ):
                    raise
                with service.local_commit() as db:
                    fresh, current_event = self._job(db, request, publisher)
                    if fresh["state"] != "pending" or not dependencies.job_current(
                        service, db, current_event, json.loads(fresh["source_snapshot"])
                    ):
                        raise error
                    if db.execute(
                        "SELECT 1 FROM memory_batches WHERE job_id=?", (job["id"],)
                    ).fetchone():
                        raise error
                return dict(
                    state="context_changed",
                    extraction_schema=5,
                    job_ids=request["job_ids"],
                    window_id=error.window_id,
                    invalid_context_member_ids=error.member_ids,
                )
        require(batch is not None, "not_found", 404)
        return self._submit(request, publisher, event, dict(batch))

    def _read(self, request, publisher, event, batch):
        service, scope = self.service, event["scope"]
        if batch is None:
            require(request["window"] is not None, "invalid_input", 400)
            service.contracts.validate("memory-extraction#summary_window", request["window"])
            manifest = dependencies.validate_window(request["window"], scope, service.contracts)
            turns = [dict(turn_id=event["aggregate_id"], input_revision=event["input_revision"])]
            require(manifest["new_turns"] == turns, "invalid_input", 400)
            expected = {canonical(source) for source in event["sources"]}
            actual = {
                canonical(member["reference"]["source"])
                for member in manifest["members"]
                if member["purpose"] == "new" and member["role"] == "user"
            }
            require(
                actual == expected
                and len(actual)
                == len([member for member in manifest["members"] if member["purpose"] == "new"]),
                "invalid_input",
                400,
            )
            members = [
                member
                for member in manifest["members"]
                if member["purpose"] == "new" and member["turn_id"] == event["aggregate_id"]
            ]
            require(
                [member["reference"]["source"] for member in members] == event["sources"]
                and all(
                    member["reference"]["input_revision"] == event["input_revision"]
                    and member["source_scope"] == scope
                    for member in members
                ),
                "invalid_input",
                400,
            )
            # This commits exact originals before any summary/model work. A later
            # invalid proposal cannot roll back or hide the captured experience.
            self.extraction._content(event, retain_originals=True)
        else:
            manifest = json.loads(batch["manifest"])
            if request["window"] is not None:
                require(
                    dependencies.validate_window(request["window"], scope, service.contracts)
                    == manifest,
                    "idempotency_conflict",
                    409,
                )
        with service.operation(scope=scope, windows=[manifest]) as db:
            job, _ = self._job(db, request, publisher)
            # A concurrent request for this job may have saved its window while
            # originals were captured. Reuse only that job's actual receipt.
            current = db.execute(
                "SELECT * FROM memory_batches WHERE job_id=?", (job["id"],)
            ).fetchone()
            if current is not None:
                old = self._completed(db, current, scope)
                if old is not None:
                    return old
                if self._retire_unbound(db, job, current, scope):
                    return self._invalidated(job["id"])
                require(current["manifest"] == canonical(manifest), "idempotency_conflict", 409)
                batch = dict(current)
            require(job["state"] == "pending", "scope_changed", 409)
            window_sources = dependencies.local_sources(service, db, manifest)
            require(len(window_sources) <= SOURCE_BUDGET, "budget_exceeded", 413)
            if batch is None:
                prepared, inherited = portrait_input(service, db, scope, window_sources)
                batch = dict(
                    id=new_id("memory-batch"),
                    job_id=job["id"],
                    window_id=manifest["window_id"],
                    manifest=canonical(manifest),
                    portrait_input=canonical(
                        dict(subject_person_id=scope["person_id"], input=prepared)
                    ),
                    portrait_basis=canonical(inherited),
                    recorded_at=utc(service.clock()),
                )
                db.execute(
                    "INSERT INTO memory_batches VALUES (?,?,?,?,?,?,?)", tuple(batch.values())
                )
            else:
                _, prepared = bound_portrait(batch, scope)
        return dict(
            state="pending",
            extraction_schema=5,
            batch_ref=batch["id"],
            job_ids=request["job_ids"],
            window_id=manifest["window_id"],
            subject_person_id=scope["person_id"],
            portrait_input=prepared,
        )

    def _write(
        self, db, text, kind, batch, event, source_map, dependencies_map, importance, *, refs=()
    ):
        service, scope = self.service, event["scope"]
        all_sources = [json.loads(row["payload"]) for row in source_map.values()]
        unit = dict(
            statement=text,
            conditions=[],
            negations=[],
            valid_time="未说明有效时间",
            uncertainty="inferred",
            reality=event["reality"],
            sources=all_sources[:16],
        )
        group_id, ids = self.writer._write_raw_group(
            db,
            dict(
                scope=scope,
                category="evidence",
                field_key="memory." + kind,
                item_key=None,
                relationship_delta=None,
                units=[unit],
            ),
            event,
            source_map,
        )
        expires = utc(
            parse_time(batch["recorded_at"])
            + timedelta(days=service.consolidation_ttl_days[importance])
        )
        db.execute(
            "INSERT INTO memory_summaries VALUES (?,?,?,?,?,?,?,?,?)",
            (
                group_id,
                kind,
                canonical(scope),
                batch["window_id"],
                batch["manifest"],
                canonical(list(refs)),
                importance,
                batch["recorded_at"],
                expires,
            ),
        )
        for key, (row, purpose) in dependencies_map.items():
            db.execute(
                "INSERT INTO memory_summary_sources VALUES (?,?,?,?,?,?)",
                (group_id, key, row["revision"], row["epoch"], row["scope"], purpose),
            )
        for ref in refs:
            prior = db.execute(
                "SELECT group_id FROM records WHERE id=?", (ref["record_id"],)
            ).fetchone()
            require(prior is not None, "scope_changed", 409)
            for row in db.execute(
                "SELECT * FROM memory_summary_sources WHERE group_id=?", (prior[0],)
            ):
                db.execute(
                    "INSERT OR IGNORE INTO memory_summary_sources VALUES (?,?,?,?,?,?)",
                    (
                        group_id,
                        row["source_key"],
                        row["revision"],
                        row["epoch"],
                        row["source_scope"],
                        "context",
                    ),
                )
        index_group(db, group_id)
        return group_id, ids

    def _submit(self, request, publisher, event, batch):
        service = self.service
        scope, output = event["scope"], request["output"]
        manifest = json.loads(batch["manifest"])
        importance = output.get("importance", "normal")
        with service.operation(scope=scope, windows=[manifest]) as db:
            job, current_event = self._job(db, request, publisher)
            old = self._completed(db, batch, scope)
            if old:
                saved = db.execute(
                    "SELECT digest FROM write_ledger WHERE job_id=?", (job["id"],)
                ).fetchone()
                require(saved[0] == fingerprint(output), "idempotency_conflict", 409)
                return old
            if self._retire_unbound(db, job, batch, scope):
                return self._invalidated(job["id"])
            service.contracts.validate("memory-extraction#summary_output", output)
            require(output["subject_person_id"] == scope["person_id"], "invalid_input", 400)
            require(job["state"] == "pending", "scope_changed", 409)
            dependencies_map = dependencies.local_sources(service, db, manifest)
            source_rows = service._source_rows(
                db, current_event["sources"], scope, reality=current_event["reality"]
            )
            require(
                json.loads(job["source_snapshot"])
                == {value["key"]: [value["revision"], value["epoch"]] for value in source_rows},
                "scope_changed",
                409,
            )
            source_map = {value["key"]: value for value in source_rows}
            _, prepared = bound_portrait(batch, scope)
            basis_refs = json.loads(batch["portrait_basis"])
            for ref in basis_refs:
                prior = db.execute(
                    "SELECT r.*,g.scope FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                    (ref["record_id"],),
                ).fetchone()
                require(
                    prior is not None
                    and prior["scope"] == canonical(scope)
                    and prior["state"] == "active"
                    and prior["version"] == ref["record_version"]
                    and service._group_current(db, prior["group_id"]),
                    "scope_changed",
                    409,
                )
            groups, ids, summaries = [], [], []
            if output["summary"].strip():
                group, new_ids = self._write(
                    db,
                    output["summary"].strip(),
                    "summary",
                    batch,
                    event,
                    source_map,
                    dependencies_map,
                    importance,
                    refs=basis_refs,
                )
                groups.append(group)
                ids.extend(new_ids)
                summaries = record_refs(db, group)
            portrait = dict(state="not_requested", record_refs=[])
            if output["portrait"] is not None:
                portrait["state"] = "pending"
                pointer = db.execute(
                    "SELECT revision,group_id FROM memory_portraits WHERE scope=?",
                    (canonical(scope),),
                ).fetchone()
                revision = pointer["revision"] if pointer else 0
                if (
                    prepared is not None
                    and summaries
                    and output["portrait"].strip()
                    and revision == prepared["expected_revision"]
                ):
                    refs = [*basis_refs, *summaries]
                    group, new_ids = self._write(
                        db,
                        output["portrait"].strip(),
                        "portrait",
                        batch,
                        event,
                        source_map,
                        dependencies_map,
                        importance,
                        refs=refs,
                    )
                    if pointer:
                        # A new portrait supersedes a projection, not its source
                        # or other pending jobs. Historical BOT context can keep
                        # using its exact record while the leaf sources remain valid.
                        prior = pointer["group_id"]
                        db.execute("UPDATE groups SET state='superseded' WHERE id=?", (prior,))
                        remove_groups(db, [prior])
                        db.execute(
                            "DELETE FROM search_index WHERE record_id IN (SELECT id FROM records WHERE group_id=?)",
                            (prior,),
                        )
                        db.execute(
                            "UPDATE projections SET state='invalidated',version=version+1 WHERE record_id IN (SELECT id FROM records WHERE group_id=?)",
                            (prior,),
                        )
                    db.execute(
                        "INSERT INTO memory_portraits VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET revision=excluded.revision,group_id=excluded.group_id",
                        (canonical(scope), revision + 1, group),
                    )
                    groups.append(group)
                    ids.extend(new_ids)
                    portrait = dict(state="committed", record_refs=record_refs(db, group))
            result = dict(
                subject_person_id=scope["person_id"],
                state="committed" if groups else "skipped",
                record_ids=ids,
                group_ids=groups,
                experience_state="saved",
                summary_state="committed" if summaries else "skipped",
                summary_record_refs=summaries,
                portrait=portrait,
                covered_job_ids=request["job_ids"],
                cursor=dict(window_id=batch["window_id"], job_ids=request["job_ids"]),
            )
            db.execute(
                "INSERT INTO write_ledger VALUES (?,?,?)",
                (job["id"], fingerprint(output), canonical(result)),
            )
            db.execute("UPDATE jobs SET state=? WHERE id=?", (result["state"], job["id"]))
            service._emit(
                db, "memory.candidate_completed", dict(candidate_job_ref=job["id"], **result)
            )
            for row in source_map.values():
                db.execute(
                    "INSERT OR IGNORE INTO source_writes VALUES (?,?,?,?)",
                    (row["key"], row["revision"], canonical(scope), job["id"]),
                )
                db.execute(
                    "INSERT OR IGNORE INTO memory_source_lifecycle VALUES (?,?,?,?,?)",
                    (
                        row["key"],
                        row["revision"],
                        importance,
                        batch["recorded_at"],
                        utc(
                            parse_time(batch["recorded_at"])
                            + timedelta(days=service.consolidation_ttl_days[importance])
                        ),
                    ),
                )
            if groups:
                service._bump(db, scope, history_changed=False)
        return dict(
            state="completed",
            extraction_schema=5,
            batch_ref=batch["id"],
            job_ids=request["job_ids"],
            result=result,
        )
