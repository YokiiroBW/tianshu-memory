"""One authorized evidence set for atomic facts and exact original episodes."""

import copy
import json
import time
from datetime import timedelta

from .. import automatic_facts, canonical_facts, memory_documents
from ..domain import (
    Fault,
    canonical,
    fingerprint,
    parse_time,
    query_terms,
    require,
    require_selection,
    utc,
)
from ..fact_queries import snapshot_for_groups
from ..memory_migration import installed
from ..recall_index import eligible_groups
from . import targets
from .history import history_version
from .migration import identity_version
from .read_bundle import (
    operation,
    requirements,
    selection_proof,
    verify_legacy,
    verify_selection_proof,
)
from .scopes import authorized_scopes, checks, group_read, group_rows


def collect(service, db, request, context):
    scope = request.get("requested_scope", request.get("effective_scope"))
    require(installed(db), "dependency_unavailable", 503)
    service._authorize(db, context, scope=scope)
    groups, complete = [], True
    if group_read(service, scope):
        after = ""
        while True:
            page, last = group_rows(db, scope, after=after, limit=512)
            groups.extend(
                eligible_groups(
                    service,
                    db,
                    page,
                    scope,
                    scopes_by_group={row["id"]: json.loads(row["scope"]) for row in page},
                )
            )
            if last:
                break
            after = page[-1]["id"]
        source_scopes = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT DISTINCT scope FROM sources WHERE state='active' AND json_extract(scope,'$.actor_id')=? AND json_extract(scope,'$.audience')='group' AND json_extract(scope,'$.conversation_id')=?",
                (scope["actor_id"], scope["conversation_id"]),
            )
        ]
        owner_scopes = {
            canonical(scope): scope,
            **{canonical(owner): owner for owner in source_scopes},
        }
        scopes = [(owner, None, 1) for owner in owner_scopes.values()]
    else:
        scopes = authorized_scopes(service, db, scope, request["include_associated"])
        for owner, _, _ in scopes:
            after = ""
            while True:
                page = db.execute(
                    "SELECT * FROM groups WHERE scope=? AND state='active' AND id>? ORDER BY id LIMIT 512",
                    (canonical(owner), after),
                ).fetchall()
                groups.extend(eligible_groups(service, db, page, owner))
                if len(page) < 512:
                    break
                after = page[-1]["id"]
    snapshot = snapshot_for_groups(db, groups, service=service, domain_complete=complete)
    document_rows = {}
    group_ids = [entry[0]["id"] for entry in groups]
    for offset in range(0, len(group_ids), 256):
        batch = group_ids[offset : offset + 256]
        for row in db.execute(
            "SELECT * FROM memory_documents WHERE group_id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ):
            document_rows[row["group_id"]] = dict(row)
    for group, _, units in groups:
        identity = group["id"]
        if identity not in document_rows and identity in snapshot.rows:
            # Legacy atomic facts are readable immediately after migration.
            # Explicit source backfill owns durable indexing; reads reuse the
            # batched fact snapshot without a per-group migration query.
            document_rows[identity] = memory_documents.fact_document(
                group, units, snapshot.decoded[identity], snapshot.rows[identity]["observed_at"]
            )
    episodes = list(memory_documents.episode_metadata(service, db, [item[0] for item in scopes]))
    return dict(
        scope=scope,
        scopes=scopes,
        groups=groups,
        group_map={entry[0]["id"]: entry for entry in groups},
        snapshot=snapshot,
        documents=document_rows,
        episodes=episodes,
        complete=complete,
    )


def query_domain(service, frame, subquery):
    """Resolve names globally, then bind the complete authorized owner scopes.

    Person resolution only projects scopes already authorized by collect,
    including explicit current private associations. It never discovers other
    conversations for that person. Body selection and its freshness proof share
    this projection; relevance, category and time filters cannot shrink it.
    """
    resolution, witnesses = targets.resolve(
        service,
        frame["snapshot"],
        frame["groups"],
        subquery["text"],
        frame["scope"],
        complete=frame["complete"],
    )
    people = {
        target["person_id"] for target in resolution["targets"] if target["status"] == "resolved"
    }
    scopes = sorted(
        (
            entry
            for entry in frame["scopes"]
            if resolution["mode"] == "room" or entry[0]["person_id"] in people
        ),
        key=lambda entry: canonical(entry[0]),
    )
    owners = {canonical(entry[0]) for entry in scopes}
    groups = [entry for entry in frame["groups"] if entry[0]["scope"] in owners]
    episodes = [row for row in frame["episodes"] if row["scope"] in owners]
    represented = {entry[0]["scope"] for entry in groups} | {row["scope"] for row in episodes}
    anchors = {
        canonical(frame["scope"]),
        *(canonical(entry[0]) for entry in frame["scopes"] if entry[1] is not None),
    }
    # Registered source owners are discovery metadata, not a room membership
    # roster or queryable content. Keep authorization anchors and every owner
    # with candidates, before any category, relevance, time or budget filtering.
    proof_scopes = sorted(
        (entry for entry in frame["scopes"] if canonical(entry[0]) in anchors | represented),
        key=lambda entry: canonical(entry[0]),
    )
    digest = fingerprint(
        dict(
            scopes=proof_scopes,
            groups=sorted(
                (entry[0]["id"], sorted((row["id"], row["version"]) for row in entry[1]))
                for entry in groups
            ),
            episodes=sorted(
                (row["id"], row["source_revision"], row["source_epoch"], row["content_hash"])
                for row in episodes
            ),
            complete=frame["complete"],
        )
    )
    return dict(
        resolution=resolution,
        witnesses=witnesses,
        scopes=scopes,
        groups=groups,
        episodes=episodes,
        complete=frame["complete"],
        digest=digest,
        count=len(groups) + len(episodes),
    )


def prepare_query(service, db, frame, subquery, selection):
    domain = query_domain(service, frame, subquery)
    candidates = [entry for entry in domain["groups"] if entry[0]["category"] in selection]
    history = subquery["intent"] != "current"
    if not history:
        candidates = automatic_facts.filter_current(
            db, candidates, subquery["text"], service.clock, fact_snapshot=frame["snapshot"]
        )
    period = subquery["time_filter"]
    if period is not None:

        def applies(entry):
            identity = entry[0]["id"]
            fact = frame["snapshot"].decoded.get(identity, {})
            document = frame["documents"].get(identity, {})
            metadata = dict(
                observed_at=document.get("observed_at"),
                valid_from=fact.get("valid_from"),
                valid_to=fact.get("valid_to"),
            )
            return temporal(metadata, period)[0]

        candidates = [entry for entry in candidates if applies(entry)]
    lexical = service._rank_groups(
        db,
        candidates,
        query_terms(subquery["text"]),
        subquery["text"],
        fact_snapshot=frame["snapshot"],
        authorized_group_ids=frozenset(frame["group_map"]),
    )
    episode_ids = {
        row["id"]
        for row in domain["episodes"]
        if temporal(dict(row, valid_from=None, valid_to=None), period)[0]
    }
    lexical_episodes = list(
        memory_documents.episode_metadata(
            service, db, [entry[0] for entry in domain["scopes"]], query_text=subquery["text"]
        )
    )
    documents = (
        [
            frame["documents"][entry[0]["id"]]
            for entry in candidates
            if entry[0]["id"] in frame["documents"]
        ]
        if "facts" in subquery["kinds"]
        else []
    )
    if "episodes" in subquery["kinds"]:
        documents += [row for row in domain["episodes"] if row["id"] in episode_ids]
    lexical_ids = (
        ["fact:" + entry[0]["id"] for entry in lexical] if "facts" in subquery["kinds"] else []
    )
    lexical_ids += (
        [row["id"] for row in lexical_episodes if row["id"] in episode_ids]
        if "episodes" in subquery["kinds"]
        else []
    )
    return dict(
        subquery=subquery,
        domain=domain,
        documents=documents,
        lexical_ids=lexical_ids,
        reference_incomplete=targets.unresolved_reference(subquery["text"], domain["resolution"]),
    )


def temporal(evidence, period):
    if period is None:
        return True, False
    if period["basis"] == "observed_at":
        value = evidence["observed_at"]
        if value is None:
            return True, True
        return parse_time(period["from"]) <= parse_time(value) < parse_time(period["to"]), False
    start, end = evidence.get("valid_from"), evidence.get("valid_to")
    if start is None and end is None:
        return True, True
    return (end is None or parse_time(end) > parse_time(period["from"])) and (
        start is None or parse_time(start) < parse_time(period["to"])
    ), False


def fact_evidence(frame, identity):
    entry = frame["group_map"][identity]
    document = frame["documents"].get(identity)
    if document is None or document["observed_at"] is None:
        return []
    source = frame["snapshot"].rows.get(identity)
    fact = frame["snapshot"].decoded.get(identity)
    result = []
    for original in entry[2]:
        unit = copy.deepcopy(original)
        owner = json.loads(entry[0]["scope"])
        value = fact.get("canonical") if fact else None
        historical = bool(source and source["superseded_by"]) or bool(
            fact and fact["state"] == "historical"
        )
        if value:
            value = dict(value, state="historical") if historical else value
            unit["statement"] = canonical_facts.render(
                value, operation=fact.get("update", {}).get("operation")
            )
        elif fact:
            # Keep old full statements in history/episodes, not as a second
            # current value contaminating a different object's property.
            unit["statement"] = fact["key"] + "：" + fact["value"]
        evidence = dict(
            evidence_id="evidence:" + unit["record_id"],
            subject_person_id=unit["subject_person_id"],
            owner_scope=owner,
            source_refs=unit["sources"],
            observed_at=document["observed_at"],
            valid_from=fact["valid_from"] if fact else None,
            valid_to=fact["valid_to"] if fact else None,
            unit=unit,
        )
        if value:
            evidence.update(kind="historical_fact" if historical else "current_fact", fact=value)
        else:
            evidence.update(kind="legacy_fact", representation="legacy_unstructured")
        result.append(evidence)
    return result


def episode_evidence(db, document):
    row = db.execute(
        "SELECT d.*,s.reality FROM memory_documents d JOIN sources s ON s.key=d.source_key WHERE d.id=? AND d.content_hash=?",
        (document["id"], document["content_hash"]),
    ).fetchone()
    require_selection(row is not None and row["observed_at"] is not None)
    owner = json.loads(row["scope"])
    return dict(
        evidence_id="evidence:" + fingerprint(row["id"]),
        subject_person_id=owner["person_id"],
        owner_scope=owner,
        source_refs=[
            dict(
                kind="source_episode",
                owner="memory",
                episode_ref=row["id"],
                episode_version=row["source_revision"],
            )
        ],
        observed_at=row["observed_at"],
        valid_from=None,
        valid_to=None,
        kind="episode",
        reality=row["reality"],
        episode_id=row["id"],
        episode_version=row["source_revision"],
        text=row["text"],
        span=dict(start_utf8=row["span_start"], end_utf8=row["span_end"]),
        event_time=None,
    )


def dependencies(db, frame, evidence):
    group_ids = list(
        dict.fromkeys(item["unit"]["semantic_group_id"] for item in evidence if "unit" in item)
    )
    source_rows, lineage = {}, {}
    for offset in range(0, len(group_ids), 256):
        batch = group_ids[offset : offset + 256]
        for row in db.execute(
            "SELECT l.group_id,s.*,p.payload AS physical FROM lineage l JOIN sources s ON s.key=l.source_key JOIN source_admissions a ON a.key=s.key JOIN physical_sources p ON p.key=a.physical_key WHERE l.group_id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ):
            source_rows[row["key"]] = dict(row)
            lineage.setdefault(row["group_id"], []).append(row["key"])
    episode_dependencies = []
    for item in evidence:
        if item["kind"] != "episode":
            continue
        row = db.execute(
            "SELECT d.source_key,s.*,p.payload AS physical FROM memory_documents d JOIN sources s ON s.key=d.source_key JOIN source_admissions a ON a.key=s.key JOIN physical_sources p ON p.key=a.physical_key WHERE d.id=?",
            (item["episode_id"],),
        ).fetchone()
        require_selection(row is not None)
        source_rows[row["key"]] = dict(row)
        episode_dependencies.append(
            dict(
                episode_id=item["episode_id"],
                episode_version=item["episode_version"],
                source_dependency_id="source-dependency:" + fingerprint(row["key"]),
                span=item["span"],
                text_hash=memory_documents.text_hash(item["text"]),
            )
        )
    sources = [
        dict(
            dependency_id="source-dependency:" + fingerprint(key),
            owner_scope=json.loads(row["scope"]),
            source_key=key,
            revision=row["revision"],
            epoch=row["epoch"],
            content_hash=json.loads(row["physical"])["content_digest"],
            state="active",
        )
        for key, row in sorted(source_rows.items())
    ]
    groups = []
    for identity in group_ids:
        group, rows, _ = frame["group_map"][identity]
        groups.append(
            dict(
                group=dict(
                    semantic_group_id=identity,
                    record_ids=[row["id"] for row in rows],
                    complete=True,
                ),
                owner_scope=json.loads(group["scope"]),
                records=[
                    dict(group_id=identity, record_id=row["id"], record_version=row["version"])
                    for row in rows
                ],
                source_dependency_ids=[
                    "source-dependency:" + fingerprint(key) for key in lineage[identity]
                ],
            )
        )
    require(len(sources) <= 256, "budget_exceeded", 429)
    return groups, episode_dependencies, sources


def history_snapshot(service, db, scopes, association, *, input_only=False):
    return dict(
        association_version=association,
        history_checks=[
            dict(
                scope=scope,
                history_version=history_version(service, db, scope, input_only=input_only),
                association_id=link,
                association_version=version,
            )
            for scope, link, version in scopes
        ],
    )


def make_bundle(service, db, request, context, frame, evidence, constraints):
    scope = frame["scope"]
    owners = {canonical(scope), *[canonical(item["owner_scope"]) for item in evidence]}
    scopes = [entry for entry in frame["scopes"] if canonical(entry[0]) in owners]
    scopes.sort(key=lambda entry: (entry[0] != scope, canonical(entry[0])))
    require(len(scopes) <= 17, "budget_exceeded", 429)
    association = identity_version(db, scope)
    groups, episodes, sources = dependencies(db, frame, evidence)
    now = service.clock()
    result = dict(
        bundle_id="",
        bundle_version=1,
        effective_scope=scope,
        selection=request["selection"],
        include_associated=request["include_associated"],
        queries=request["queries"],
        budget=request["budget"],
        limits=request["limits"],
        scope_version=service._scope_version(db, scope),
        association_version=association,
        scope_checks=checks(service, db, scopes),
        history_snapshot=history_snapshot(service, db, scopes, association),
        input_history_snapshot=history_snapshot(service, db, scopes, association, input_only=True),
        group_dependencies=groups,
        episode_dependencies=episodes,
        source_dependencies=sources,
        evidence_ids=[item["evidence_id"] for item in evidence],
        evidence_digest=fingerprint(evidence),
        selection_constraints=constraints,
        owner_heads={
            row["owner"]: dict(generation=row["generation"], sequence=row["sequence"])
            for row in db.execute("SELECT * FROM owner_heads")
        },
        source_revision=int(
            db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
        ),
        validation_mode="full_barrier",
        verified_at=utc(now),
        valid_until=utc(min(now + timedelta(seconds=30), parse_time(context["expires_at"]))),
    )
    result["bundle_id"] = selection_proof(db, result)
    return result


def verify_snapshot(service, db, bundle, frame, context):
    verify_selection_proof(db, bundle)
    require_selection(
        bundle["effective_scope"] == frame["scope"]
        and len(bundle["queries"]) == len(bundle["selection_constraints"]),
    )
    for subquery, constraint in zip(
        bundle["queries"], bundle["selection_constraints"], strict=True
    ):
        domain = query_domain(service, frame, subquery)
        require_selection(
            constraint["query_id"] == subquery["query_id"]
            and constraint["owner_resolution"] == domain["resolution"]
            and constraint["candidate_domain_hash"] == domain["digest"]
            and constraint["candidate_count"] == domain["count"]
            and constraint["candidate_domain_complete"] == domain["complete"],
        )
        if constraint["result_status"] == "no_match":
            require_selection("no_match" in constraint["proof_kinds"] and domain["complete"])
    evidence = {}
    for group in bundle["group_dependencies"]:
        identity = group["group"]["semantic_group_id"]
        require_selection(identity in frame["group_map"])
        for item in fact_evidence(frame, identity):
            evidence[item["evidence_id"]] = item
    docs = {item["id"]: item for item in frame["episodes"]}
    for dependency in bundle["episode_dependencies"]:
        require_selection(dependency["episode_id"] in docs)
        item = episode_evidence(db, docs[dependency["episode_id"]])
        evidence[item["evidence_id"]] = item
    require_selection(
        set(evidence) == set(bundle["evidence_ids"])
        and len(bundle["evidence_ids"]) == len(evidence),
    )
    ordered = [evidence[identity] for identity in bundle["evidence_ids"]]
    fresh = make_bundle(
        service, db, bundle, context, frame, ordered, bundle["selection_constraints"]
    )
    for key in (
        "evidence_digest",
        "group_dependencies",
        "episode_dependencies",
        "source_dependencies",
        "scope_checks",
        "association_version",
        "history_snapshot",
        "input_history_snapshot",
    ):
        require_selection(fresh[key] == bundle[key])
    require_selection(fresh["bundle_id"] == bundle["bundle_id"])
    return fresh


def verify(application, request, context, legacy_contexts):
    service = application.service
    bundle = request["bundle"]
    require(bundle["effective_scope"] == request["requested_scope"], "scope_changed", 409)
    require(
        len({check["check_id"] for check in request["checks"]}) == len(request["checks"]),
        "invalid_input",
        400,
    )
    items = requirements(
        service, request, context, list(zip(request["checks"], legacy_contexts, strict=True))
    )
    with operation(service, items) as db:
        frame = collect(service, db, bundle, context)
        fresh = verify_snapshot(service, db, bundle, frame, context)
        checked = verify_legacy(application, request["checks"], legacy_contexts)
        # Request-local issuer reuse cannot borrow a later lease or return a
        # valid verification after any adopted origin expired during the work.
        service._authorize(db, context, scope=request["requested_scope"])
        deadline = parse_time(fresh["valid_until"])
        for check, resolved in zip(request["checks"], legacy_contexts, strict=True):
            payload = check["request"]
            service._authorize(
                db, resolved, scope=payload.get("requested_scope", payload.get("requester_scope"))
            )
            deadline = min(deadline, parse_time(resolved["expires_at"]))
        now = service.clock()
        require(deadline > now, "timeout", 408)
    return dict(
        schema_version=2,
        version_domain="memory-context/v2",
        request_id=request["query"]["request_id"],
        effective_scope=request["requested_scope"],
        state="valid",
        bundle=fresh,
        checks=checked,
        checked_at=utc(now),
        valid_until=utc(deadline),
    )


def execute(application, request, context):
    from ..persistent_retrieval import discard_corrupt, prepare, prepare_query_vector

    service = application.service
    require(
        len({query["query_id"] for query in request["queries"]}) == len(request["queries"]),
        "invalid_input",
        400,
    )
    for query in request["queries"]:
        if query["time_filter"]:
            require(
                parse_time(query["time_filter"]["from"]) < parse_time(query["time_filter"]["to"]),
                "invalid_input",
                400,
            )
    items = requirements(service, request, context)
    settings, backend_failed = None, False
    if service.retrieval is not None:
        try:
            settings = dict(service.retrieval.settings())
        except (Fault, OSError, TimeoutError):
            backend_failed = True
    # A bounded query embedding depends only on the caller's text and captured
    # retrieval revision. Do it before acquiring a source boundary, so current
    # authorized document vectors can later be ranked without any external work.
    query_vectors = {}
    if settings is not None:
        deadline = time.monotonic() + 8
        for query in request["queries"]:
            query_vectors[query["query_id"]] = prepare_query_vector(
                service.retrieval, query["text"], settings, deadline=deadline
            )
    with operation(service, items) as db:
        frame = collect(service, db, request, context)
        if request["known_bundle"] is not None:
            verify_snapshot(service, db, request["known_bundle"], frame, context)
        prepared = [
            prepare_query(service, db, frame, query, request["selection"])
            for query in request["queries"]
        ]
        for plan in prepared:
            plan["ranking"] = (
                prepare(
                    db,
                    plan["documents"],
                    plan["lexical_ids"],
                    settings,
                    query_vector=query_vectors[plan["subquery"]["query_id"]],
                )
                if settings
                else None
            )
            if plan["ranking"] is not None:
                discard_corrupt(db, plan["ranking"])
        if not any(plan["ranking"] is not None and plan["ranking"].external for plan in prepared):
            # The evidence and its negative/selection proof are produced from this
            # same fresh source boundary. Nothing outside the transaction ran.
            rank_plans(service, prepared, backend_failed)
            return assemble(service, db, request, context, frame, prepared)
    rank_plans(service, prepared, backend_failed)
    with operation(service, items) as db:
        fresh = collect(service, db, request, context)
        for plan in prepared:
            domain = query_domain(service, fresh, plan["subquery"])
            require(
                domain["resolution"] == plan["domain"]["resolution"]
                and domain["digest"] == plan["domain"]["digest"],
                "scope_changed",
                409,
            )
            plan["domain"] = domain
        return assemble(service, db, request, context, fresh, prepared)


def rank_plans(service, prepared, backend_failed):
    from ..persistent_retrieval import rank_prepared

    retrieval_deadline = (
        time.monotonic() + 8
        if any(plan["ranking"] is not None and plan["ranking"].external for plan in prepared)
        else None
    )
    for plan in prepared:
        by_id = {document["id"]: document for document in plan["documents"]}
        plan["ranked"] = [by_id[identity] for identity in plan["lexical_ids"] if identity in by_id]
        plan["index_complete"] = not backend_failed
        if plan["ranking"] is not None:
            plan["ranked"], plan["index_complete"] = rank_prepared(
                service.retrieval,
                service.store,
                plan["subquery"]["text"],
                plan["ranking"],
                deadline=retrieval_deadline,
            )


def assemble(service, db, request, context, frame, prepared):
    evidence, results, constraints = [], [], []
    included = set()
    facts_count = episodes_count = 0
    for plan in prepared:
        query, domain = plan["subquery"], plan["domain"]
        resolution = domain["resolution"]
        result_ids, omissions, missing_time = [], [], 0
        witness_ids = [entry[0]["id"] for entry in domain["witnesses"]]
        for document in plan["ranked"]:
            batch = (
                fact_evidence(frame, document["group_id"])
                if document["kind"] == "fact"
                else [episode_evidence(db, document)]
            )
            if not batch:
                omissions.append("source_time_unavailable")
                continue
            accepted = []
            for item in batch:
                keep, missing = temporal(item, query["time_filter"])
                missing_time += missing
                if keep:
                    accepted.append(item)
            if len(accepted) != len(batch):
                continue
            witnesses = [
                item for identity in witness_ids for item in fact_evidence(frame, identity)
            ]
            complete_batch = list(
                {item["evidence_id"]: item for item in [*witnesses, *accepted]}.values()
            )
            added = [item for item in complete_batch if item["evidence_id"] not in included]
            new_facts = sum(item["kind"] != "episode" for item in added)
            new_episodes = len(added) - new_facts
            total = len(canonical([*evidence, *added]).encode())
            owner_count = len(
                {
                    canonical(frame["scope"]),
                    *[canonical(item["owner_scope"]) for item in [*evidence, *added]],
                }
            )
            if (
                facts_count + new_facts > request["limits"]["facts"]
                or episodes_count + new_episodes > request["limits"]["episodes"]
                or total > request["budget"]["bytes"]
                or total > request["budget"]["tokens"]
                or owner_count > 17
            ):
                omissions.append("budget")
                continue
            evidence.extend(added)
            included.update(item["evidence_id"] for item in added)
            result_ids.extend(item["evidence_id"] for item in complete_batch)
            facts_count += new_facts
            episodes_count += new_episodes
        unresolved = plan["reference_incomplete"] or any(
            target["status"] != "resolved" for target in resolution["targets"]
        )
        if unresolved:
            omissions.append("unresolved_reference")
        if not plan["index_complete"]:
            omissions.append("index_incomplete")
        if missing_time:
            omissions.append("source_time_unavailable")
        incomplete = bool(omissions) or not domain["complete"]
        status = "incomplete" if incomplete else "matched" if result_ids else "no_match"
        if unresolved and not result_ids:
            status = "unresolved"
        proofs = ["positive_sources"]
        if any(
            target["kind"] == "named" and target["status"] == "resolved"
            for target in resolution["targets"]
        ):
            proofs.append("unique_owner")
        if query["intent"] == "current":
            proofs.append("latest_values")
        if status == "no_match":
            omissions.append("no_match")
            proofs.append("no_match")
        selected = [item for item in evidence if item["evidence_id"] in result_ids]
        results.append(
            dict(
                query_id=query["query_id"],
                status=status,
                evidence_ids=list(dict.fromkeys(result_ids)),
                omissions=list(dict.fromkeys(omissions)),
                coverage=dict(
                    matched_facts=sum(item["kind"] == "fact" for item in plan["ranked"]),
                    returned_facts=sum(item["kind"] != "episode" for item in selected),
                    matched_episodes=sum(item["kind"] == "episode" for item in plan["ranked"]),
                    returned_episodes=sum(item["kind"] == "episode" for item in selected),
                    candidate_complete=domain["complete"],
                    index_complete=plan["index_complete"],
                    history_complete=False,
                    missing_source_times=missing_time,
                ),
            )
        )
        constraints.append(
            dict(
                query_id=query["query_id"],
                owner_resolution=resolution,
                proof_kinds=list(dict.fromkeys(proofs)),
                candidate_domain_hash=domain["digest"],
                candidate_count=domain["count"],
                candidate_domain_complete=domain["complete"],
                result_status=status,
            )
        )
    bundle = make_bundle(service, db, request, context, frame, evidence, constraints)
    size = len(canonical(evidence).encode()) if evidence else 0
    return dict(
        schema_version=2,
        version_domain="memory-context/v2",
        request_id=request["query"]["request_id"],
        effective_scope=frame["scope"],
        evidence=evidence,
        query_results=results,
        bundle=bundle,
        budget_used=dict(tokens=size, bytes=size),
        omissions=list(dict.fromkeys(item for result in results for item in result["omissions"])),
    )
