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
        require(
            type(request) is dict and set(request) == {"job_id", "output"}, "invalid_input", 400
        )
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
            completed = self._completed(db, job["id"])
            if completed:
                return completed
            if job["state"] != "pending":
                return dict(state="invalidated", result=None, content=[])
        content = self._content(event)
        if request["output"] is None:
            with service.operation(scope=scope, sources=event["sources"], event=event) as db:
                # Another same-job request can finish while _content reads its
                # sources. Its committed receipt outranks a new pending snapshot.
                completed = self._completed(db, job["id"])
                if completed:
                    return completed
                hints = facts.known(service, db, scope)
                version = 3 if facts.installed(db) else 1
            result = dict(state="pending", result=None, content=content)
            if version == 3:
                result.update(extraction_schema=3, known_facts=hints)
            return result
        try:
            drafts = self._drafts(event, content, request["output"])
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
                return completed
            return dict(state="rejected", result={"error_code": "invalid_input"}, content=[])
        return dict(state="completed", result=result, content=[])

    @staticmethod
    def _completed(db, job_id):
        receipt = db.execute("SELECT result FROM write_ledger WHERE job_id=?", (job_id,)).fetchone()
        return (
            dict(state="completed", result=json.loads(receipt[0]), content=[]) if receipt else None
        )

    def _content(self, event):
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
        return result

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
                from .fact_names import candidate, prepare
                from .temporal_facts import sourced_times

                fact = sourced_times(facts.validate(memory["fact"]), evidence, content)
                fact = prepare(fact, unit, evidence, content)
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
