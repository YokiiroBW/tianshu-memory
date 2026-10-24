"""Memory-owned candidate read/commit port; models never choose scope or authority."""

import json

from . import automatic_facts as facts
from .domain import Fault, canonical, fingerprint, new_id, parse_time, require, source_selector, utc
from .memory_context.migration import installed as context_installed
from .workflow import TrustedWorkflow


class CandidateExtraction:
    def __init__(self, service):
        self.service = service

    def execute(self, request, publisher):
        require(type(request) is dict, "invalid_input", 400)
        negotiated = set(request) != {"job_id", "output"}
        if negotiated:
            if self.service.contracts.extraction_version is None:
                self.service.contracts.load_memory_v2()
            self.service.contracts.validate("memory-extraction#process_request", request)
        require(
            type(request["job_id"]) is str and 0 < len(request["job_id"]) <= 256,
            "invalid_input",
            400,
        )
        service = self.service
        require(service.synchronized, "dependency_unavailable", 503)
        with service.store.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (request["job_id"],)).fetchone()
            require(job is not None, "not_found", 404)
            event = json.loads(job["event"])
            scope = event["scope"]
            require(publisher["authenticated_service"] == "companion")
            require(scope["actor_id"] not in publisher.get("blocked_role_actors", []))
            require(
                scope in publisher["allowed_scopes"]
                or scope["actor_id"] in publisher.get("allowed_role_actors", [])
            )
            from .memory_migration import installed as typed_installed

            protocol = 3
            if typed_installed(db):
                protocol = dict(job).get("extraction_schema")
                advertised = request.get(
                    "supported_extraction_schemas", [request.get("extraction_schema", 3)]
                )
                if protocol is None:
                    protocol = max(advertised)
                    db.execute(
                        "UPDATE jobs SET extraction_schema=? WHERE id=? AND extraction_schema IS NULL",
                        (protocol, job["id"]),
                    )
                require(protocol in advertised, "invalid_input", 400)
            else:
                require(not negotiated, "dependency_unavailable", 503)
            completed = self._completed(db, job["id"])
            if completed:
                return dict(completed, extraction_schema=protocol) if negotiated else completed
            if job["state"] != "pending":
                result = dict(state="invalidated", result=None, content=[])
                return dict(result, extraction_schema=protocol) if negotiated else result
        if request["output"] is None:
            result = self._content(event, pending_job=job["id"], protocol=protocol)
            return dict(result, extraction_schema=protocol) if negotiated else result
        content = self._content(event)
        try:
            drafts = (
                self._typed_drafts(event, content, request["output"])
                if protocol == 4
                else self._drafts(event, content, request["output"])
            )
            # The workflow rolls this proposal's entire transaction back before
            # raising. A rejected proposal can be extracted again; another older
            # same-job request may still be committing its own valid proposal.
            result = TrustedWorkflow(service).commit_extracted(
                dict(job_id=job["id"], drafts=drafts)
            )
        except Fault as error:
            if error.code != "invalid_input":
                raise
            # The current proposal rolled back, but an older same-job request
            # may already have committed after our initial receipt read. Reuse
            # that result before reporting a semantic rejection to the caller.
            with service.store.transaction() as db:
                completed = self._completed(db, job["id"])
            if completed:
                return dict(completed, extraction_schema=protocol) if negotiated else completed
            response = dict(state="rejected", result={"error_code": "invalid_input"}, content=[])
            return dict(response, extraction_schema=protocol) if negotiated else response
        response = dict(state="completed", result=result, content=[])
        return dict(response, extraction_schema=protocol) if negotiated else response

    @staticmethod
    def _completed(db, job_id):
        receipt = db.execute("SELECT result FROM write_ledger WHERE job_id=?", (job_id,)).fetchone()
        return (
            dict(state="completed", result=json.loads(receipt[0]), content=[]) if receipt else None
        )

    def _content(self, event, *, pending_job=None, protocol=3):
        service = self.service
        scope, sources = event["scope"], event["sources"]
        with service.operation(scope=scope, sources=sources, event=event) as db:
            service._source_rows(db, sources, scope, reality=event["reality"])
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
            result.append(dict(index=index, text=text, sent_at=data["sent_at"]))
        require(len(canonical(result).encode()) <= 65536, "budget_exceeded", 413)
        with service.operation(scope=scope, sources=sources, event=event) as db:
            rows = service._source_rows(db, sources, scope, reality=event["reality"])
            if context_installed(db):
                covered = {
                    row["key"]
                    for row in rows
                    if db.execute(
                        "SELECT 1 FROM context_applications WHERE source_key=? AND revision=? "
                        "AND scope=? LIMIT 1",
                        (row["key"], row["revision"], canonical(scope)),
                    ).fetchone()
                }
                # An explicit committed tool action owns its exact source revision. Never
                # create a second inferred copy, including after the explicit record is forgotten.
                # Other source messages in this aggregate remain eligible for extraction.
                for index, source in enumerate(sources):
                    if service.source_identity(source, scope) in covered:
                        result[index]["text"] = ""
            from .memory_documents import capture_sources

            capture_sources(service, db, event, result, rows)
            if pending_job is not None:
                # This fresh post-content transaction already owns the same
                # source/event boundary. Read the ledger and matching hints
                # here, rather than immediately repeating the whole barrier.
                completed = self._completed(db, pending_job)
                if completed:
                    return completed
                if protocol == 4:
                    from .canonical_facts import known

                    hints = known(
                        service,
                        db,
                        scope,
                        "\n".join(item["text"] for item in result),
                        reality=event["reality"],
                    )
                else:
                    hints = facts.known(service, db, scope)
                pending = dict(state="pending", result=None, content=result)
                if facts.installed(db):
                    pending.update(extraction_schema=protocol, known_facts=hints)
                return pending
        return result

    def _typed_drafts(self, event, content, output):
        from . import canonical_facts
        from .fact_names import candidate
        from .fact_provenance import DERIVED_UPDATES
        from .fact_semantics import sourced_modality
        from .temporal_facts import sourced_times, unit_time

        self.service.contracts.validate("memory-extraction#output", output)
        require(event["reality"] in {"real", "fictional"}, "invalid_input", 400)
        drafts = []
        consumed_targets = set()
        for memory in output["memories"]:
            evidence = memory["evidence"]
            indexes = set()
            for proof in evidence:
                index, quote = proof["source_index"], proof["quote"]
                require(
                    0 <= index < len(content) and quote.strip() and quote in content[index]["text"],
                    "invalid_input",
                    400,
                )
                indexes.add(index)
            fact = canonical_facts.prepare(memory["fact"])
            if fact["update"]["operation"] in canonical_facts.CONSUMING_UPDATES:
                for ref in fact["update"]["target_refs"]:
                    identity = (ref["group_id"], ref["record_id"])
                    # A batch names the original optimistic targets, not a
                    # model-invented sequence of future record versions. Detect
                    # this proposal's own conflict before any draft can write.
                    require(identity not in consumed_targets, "invalid_input", 400)
                    consumed_targets.add(identity)
            fact = sourced_modality(fact, evidence, content)
            # Derived updates prove inherited dates against their exact current
            # targets inside the write transaction. Independent dates must be
            # supported by this proposal's own original source.
            if fact["update"]["operation"] not in DERIVED_UPDATES:
                fact = sourced_times(fact, evidence, content)
            fact["reality"] = event["reality"]
            # A repaired observation-as-effective date must also update the one
            # canonical representation, never leave different dates in two fields.
            fact["canonical"].update(valid_from=fact["valid_from"], valid_to=fact["valid_to"])
            unit = dict(
                statement=canonical_facts.render(
                    fact["canonical"], operation=fact["update"]["operation"]
                ),
                conditions=memory["conditions"],
                negations=memory["negations"],
                valid_time=unit_time(fact, memory["valid_time"], evidence, content),
                uncertainty="inferred",
                reality=event["reality"],
                sources=[event["sources"][i] for i in sorted(indexes)],
            )
            drafts.append(
                dict(
                    scope=event["scope"],
                    category="evidence",
                    field_key="automatic." + fact["key"],
                    item_key=None,
                    relationship_delta=None,
                    units=[unit],
                    fact=fact,
                    observed_at=max((content[i]["sent_at"] for i in indexes), key=parse_time),
                    quotes=[
                        dict(
                            source_key=self.service.source_identity(
                                event["sources"][p["source_index"]], event["scope"]
                            ),
                            quote=p["quote"],
                            context_text=content[p["source_index"]]["text"],
                            **(
                                {"source_text": content[p["source_index"]]["text"]}
                                if candidate(fact)
                                else {}
                            ),
                        )
                        for p in evidence
                    ],
                )
            )
        return drafts

    def _drafts(self, event, content, output):
        require(type(output) is dict and set(output) == {"memories"}, "invalid_input", 400)
        memories = output["memories"]
        require(type(memories) is list and len(memories) <= 8, "invalid_input", 400)
        drafts = []
        for memory in memories:
            require(
                type(memory) is dict
                and set(memory)
                in (
                    {"statement", "conditions", "negations", "valid_time", "evidence"},
                    {"statement", "conditions", "negations", "valid_time", "evidence", "fact"},
                ),
                "invalid_input",
                400,
            )
            require(
                type(memory["statement"]) is str and 0 < len(memory["statement"].strip()) <= 1200,
                "invalid_input",
                400,
            )
            require(
                type(memory["valid_time"]) is str and len(memory["valid_time"]) <= 1200,
                "invalid_input",
                400,
            )
            for name in ("conditions", "negations"):
                require(
                    type(memory[name]) is list
                    and len(memory[name]) <= 16
                    and all(type(v) is str and 0 < len(v.strip()) <= 512 for v in memory[name]),
                    "invalid_input",
                    400,
                )
            evidence = memory["evidence"]
            require(type(evidence) is list and 0 < len(evidence) <= 16, "invalid_input", 400)
            indexes = set()
            for proof in evidence:
                require(
                    type(proof) is dict and set(proof) == {"source_index", "quote"},
                    "invalid_input",
                    400,
                )
                index, quote = proof["source_index"], proof["quote"]
                require(type(index) is int and 0 <= index < len(content), "invalid_input", 400)
                require(
                    type(quote) is str and bool(quote.strip()) and quote in content[index]["text"],
                    "invalid_input",
                    400,
                )
                indexes.add(index)
            require(event["reality"] in {"real", "fictional"}, "invalid_input", 400)
            unit = {k: memory[k] for k in ("statement", "conditions", "negations", "valid_time")}
            # Unknown timing is not an invalid fact or a claim of current validity.
            # Keep the published nonempty label without inventing an effective date.
            unit["valid_time"] = memory["valid_time"].strip() or "未说明有效时间"
            unit.update(
                uncertainty="inferred",
                reality=event["reality"],
                sources=[event["sources"][i] for i in sorted(indexes)],
            )
            drafts.append(
                dict(
                    scope=event["scope"],
                    category="evidence",
                    units=[unit],
                    field_key="automatic." + fingerprint(memory["statement"]),
                    item_key=None,
                    relationship_delta=None,
                )
            )
            if "fact" in memory:
                from .fact_labels import prepare as prepare_label
                from .fact_names import candidate, prepare
                from .temporal_facts import sourced_times

                fact = sourced_times(facts.validate(memory["fact"]), evidence, content)
                fact = prepare(fact, unit, evidence, content)
                fact = prepare_label(fact, evidence, content)
                # Scope/subject/reality come only from the authenticated source event.
                require(event["reality"] == "real", "invalid_input", 400)
                drafts[-1].update(
                    fact=fact,
                    field_key="automatic." + fact["key"],
                    item_key=None,
                    observed_at=max((content[i]["sent_at"] for i in indexes), key=parse_time),
                    quotes=[
                        dict(
                            source_key=self.service.source_identity(
                                event["sources"][proof["source_index"]], event["scope"]
                            ),
                            quote=proof["quote"],
                            **(
                                {"source_text": content[proof["source_index"]]["text"]}
                                if candidate(fact)
                                else {}
                            ),
                        )
                        for proof in evidence
                    ],
                )
                if fact["state"] == "historical":
                    unit["valid_time"] = "历史事实，不能据此认定仍然有效：" + unit["valid_time"]
                if fact["valid_to"]:
                    unit["valid_time"] += "；有效期截止 " + fact["valid_to"]
        return drafts
