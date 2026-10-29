"""One authorized evidence set for atomic facts and exact original episodes."""

import copy
import json
import time
from dataclasses import replace
from datetime import timedelta

from .. import automatic_facts, canonical_facts, consolidation_sources, memory_documents
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
from .authorization import authorized_scopes
from .history import history_version
from .migration import identity_version
from .partition import candidate_limit, documents_by_owner, interleave, partition
from .read_bundle import (
    contextual,
    operation,
    requirements,
    selection_proof,
    verify_legacy,
    verify_selection_proof,
)
from .scopes import checks, group_read, group_rows

MAX_BUNDLE_OWNERS = 17


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
    from ..memory_events import selection_links

    return dict(
        scope=scope,
        scopes=scopes,
        groups=groups,
        group_map={entry[0]["id"]: entry for entry in groups},
        snapshot=snapshot,
        documents=document_rows,
        summaries=consolidation_sources.metadata(db, group_ids),
        episodes=episodes,
        event_links=selection_links(db, groups),
        complete=complete,
    )


def query_domain(service, frame, subquery, *, context_mode=False):
    """Select within the existing authorized frame, with factual proofs optional.

    Factual resolution binds the complete projected owner domain. Context uses
    that frame directly without resolving a textual name or claiming exhaustion.
    Neither mode discovers conversations or grants a new scope.
    """
    if context_mode:
        resolution, witnesses = dict(mode="unresolved", targets=[]), []
    else:
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
            if context_mode or resolution["mode"] == "room" or entry[0]["person_id"] in people
        ),
        key=lambda entry: canonical(entry[0]),
    )
    owners = {canonical(entry[0]) for entry in scopes}
    groups = [entry for entry in frame["groups"] if entry[0]["scope"] in owners]
    # Context summaries already carry exact owners within collect's authorized
    # frame. Supplying them never resolves an unproven textual person reference.
    contextual = [entry for entry in frame["groups"] if entry[0]["id"] in frame["summaries"]]
    groups = list({entry[0]["id"]: entry for entry in [*groups, *contextual]}.values())
    contextual_owners = {entry[0]["scope"] for entry in contextual}
    scopes = [
        entry
        for entry in frame["scopes"]
        if entry in scopes or canonical(entry[0]) in contextual_owners
    ]
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
        complete=frame["complete"] and not context_mode,
        digest=digest,
        count=len(groups) + len(episodes),
    )


def prepare_query(service, db, frame, subquery, selection, *, context_mode=False):
    domain = query_domain(service, frame, subquery, context_mode=context_mode)
    candidates = [entry for entry in domain["groups"] if entry[0]["category"] in selection]
    contextual = [entry for entry in candidates if entry[0]["id"] in frame["summaries"]]
    candidates = [entry for entry in candidates if entry[0]["id"] not in frame["summaries"]]
    history = subquery["intent"] != "current"
    if not history:
        contextual = [
            entry
            for entry in contextual
            if parse_time(frame["summaries"][entry[0]["id"]]["expires_at"]) > service.clock()
        ]
    if not history:
        candidates = automatic_facts.filter_current(
            db,
            candidates,
            subquery["text"],
            service.clock,
            fact_snapshot=frame["snapshot"],
            include_history=False if context_mode else None,
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
        contextual = [entry for entry in contextual if applies(entry)]
    candidates.extend(contextual)
    per_owner_ranked = (
        {
            owner: service._rank_groups(
                db,
                entries,
                query_terms(subquery["text"], legacy_hints=not context_mode),
                subquery["text"],
                fact_snapshot=frame["snapshot"],
                authorized_group_ids=frozenset(frame["group_map"]),
                context_mode=context_mode,
            )
            for owner, entries in partition(candidates).items()
        }
        if "facts" in subquery["kinds"] and not context_mode
        else {}
    )
    if context_mode and "facts" in subquery["kinds"]:
        from ..partition_index import rank as rank_partitions

        per_owner_ranked = rank_partitions(
            db, partition(candidates), query_terms(subquery["text"], legacy_hints=False)
        )
    lexical = interleave(per_owner_ranked)
    episode_ids = {
        row["id"]
        for row in memory_documents.eligible_episodes(service, db, domain["episodes"], subquery)
    }
    lexical_episodes = (
        list(
            memory_documents.episode_metadata(
                service,
                db,
                [entry[0] for entry in domain["scopes"]],
                query_text=subquery["text"],
                legacy_hints=not context_mode,
            )
        )
        if "episodes" in subquery["kinds"]
        else []
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
        context_mode=context_mode,
        reference_incomplete=not context_mode
        and targets.unresolved_reference(subquery["text"], domain["resolution"]),
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
        summary = frame["summaries"].get(identity)
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
        if summary:
            evidence.update(
                kind=summary["kind"],
                importance=summary["importance"],
                expires_at=summary["expires_at"],
            )
            if summary["kind"] == "summary" and "memory_kind" in summary:
                evidence.update(
                    {key: summary[key] for key in ("memory_kind", "state", "valid_until")}
                )
        elif value:
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
            "SELECT l.group_id,l.source_key FROM lineage l WHERE l.group_id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ):
            lineage.setdefault(row["group_id"], []).append(row["source_key"])
        if consolidation_sources.installed(db):
            for row in db.execute(
                "SELECT d.group_id,d.source_key FROM memory_summary_sources d WHERE d.group_id IN ("
                + ",".join("?" for _ in batch)
                + ")",
                batch,
            ):
                lineage.setdefault(row["group_id"], []).append(row["source_key"])
    from ..segment_sources import dependency_rows

    required_keys = sorted({key for values in lineage.values() for key in values})
    source_rows.update(dependency_rows(db, required_keys))
    require_selection(set(source_rows) == set(required_keys))
    episode_dependencies = []
    for item in evidence:
        if item["kind"] != "episode":
            continue
        row = db.execute(
            "SELECT d.source_key FROM memory_documents d WHERE d.id=?",
            (item["episode_id"],),
        ).fetchone()
        require_selection(row is not None)
        original = dependency_rows(db, [row["source_key"]]).get(row["source_key"])
        require_selection(original is not None)
        row = original
        source_rows[row["key"]] = row
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
            content_hash=row["source_content_hash"],
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
                    "source-dependency:" + fingerprint(key)
                    for key in sorted(set(lineage[identity]))
                ],
            )
        )
    require(len(sources) <= 256, "budget_exceeded", 429)
    return groups, episode_dependencies, sources


def evidence_source_keys(db, evidence):
    """Keep the existing 256-source bundle budget atomic for derived groups too."""
    result = set()
    for item in evidence:
        if item["kind"] == "episode":
            row = db.execute(
                "SELECT source_key FROM memory_documents WHERE id=?", (item["episode_id"],)
            ).fetchone()
            require_selection(row is not None)
            result.add(row[0])
        else:
            identity = item["unit"]["semantic_group_id"]
            result.update(
                row[0]
                for row in db.execute(
                    "SELECT source_key FROM lineage WHERE group_id=?", (identity,)
                )
            )
            if consolidation_sources.installed(db):
                result.update(
                    row[0]
                    for row in db.execute(
                        "SELECT source_key FROM memory_summary_sources WHERE group_id=?",
                        (identity,),
                    )
                )
    return result


def source_owners(db, keys):
    owners = set()
    ordered = sorted(keys)
    for offset in range(0, len(ordered), 256):
        batch = ordered[offset : offset + 256]
        rows = db.execute(
            "SELECT key,scope FROM sources WHERE key IN (" + ",".join("?" for _ in batch) + ")",
            batch,
        ).fetchall()
        require_selection(len(rows) == len(batch))
        owners.update(row["scope"] for row in rows)
    return owners


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
    groups, episodes, sources = dependencies(db, frame, evidence)
    owners = {
        canonical(scope),
        *[canonical(item["owner_scope"]) for item in evidence],
        *[canonical(item["owner_scope"]) for item in sources],
    }
    require_selection(owners <= {canonical(entry[0]) for entry in frame["scopes"]})
    scopes = [entry for entry in frame["scopes"] if canonical(entry[0]) in owners]
    scopes.sort(key=lambda entry: (entry[0] != scope, canonical(entry[0])))
    require(len(scopes) <= MAX_BUNDLE_OWNERS, "budget_exceeded", 429)
    association = identity_version(db, scope)
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
    if "retrieval_mode" in request:
        result["retrieval_mode"] = request["retrieval_mode"]
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
        require_selection(constraint["query_id"] == subquery["query_id"])
        if contextual(bundle):
            continue
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
    allows_expired = any(query["intent"] != "current" for query in bundle["queries"])
    for group in bundle["group_dependencies"]:
        identity = group["group"]["semantic_group_id"]
        require_selection(identity in frame["group_map"])
        summary = frame["summaries"].get(identity)
        require_selection(
            allows_expired or summary is None or parse_time(summary["expires_at"]) > service.clock()
        )
        for item in fact_evidence(frame, identity):
            evidence[item["evidence_id"]] = item
    docs = {item["id"]: item for item in frame["episodes"]}
    expired_sources = (
        consolidation_sources.expired_sources(
            db, [item["source_key"] for item in docs.values()], service.clock
        )
        if not allows_expired
        else set()
    )
    for dependency in bundle["episode_dependencies"]:
        require_selection(dependency["episode_id"] in docs)
        require_selection(docs[dependency["episode_id"]]["source_key"] not in expired_sources)
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
    selected = (
        "evidence_digest",
        "group_dependencies",
        "episode_dependencies",
        "source_dependencies",
    )
    if not contextual(bundle):
        selected += (
            "scope_checks",
            "association_version",
            "history_snapshot",
            "input_history_snapshot",
        )
    for key in selected:
        require_selection(fresh[key] == bundle[key])
    require_selection(fresh["bundle_id"] == bundle["bundle_id"])
    return fresh


def verify_selected(service, db, bundle, context):
    """Rebuild and verify an existing selection within the caller's checkpoint."""
    return verify_snapshot(service, db, bundle, collect(service, db, bundle, context), context)


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
        fresh = verify_selected(service, db, bundle, context)
        checked = verify_legacy(
            application, request["checks"], legacy_contexts, db=db, verify_selected=verify_selected
        )
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
    if request["known_bundle"] is not None:
        require(contextual(request) == contextual(request["known_bundle"]), "invalid_input", 400)
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
    query_vectors, vectors_by_text = {}, {}
    if settings is not None:
        deadline = time.monotonic() + 8
        for query in request["queries"]:
            # Kinds, intent and time filters change document selection, not the
            # embedding of the exact text under this one captured revision.
            # Reuse only here; authorization and every query's proof stay live.
            text = query["text"]
            if text not in vectors_by_text:
                vectors_by_text[text] = prepare_query_vector(
                    service.retrieval, text, settings, deadline=deadline
                )
            query_vectors[query["query_id"]] = vectors_by_text[text]
    local_selection = (
        service.synchronized
        and contextual(request)
        and request["known_bundle"] is None
        and (
            settings is None
            or (
                not settings["rerank"]
                and (
                    settings["mode"] == "keyword"
                    or all(value is not None for value in query_vectors.values())
                )
            )
        )
    )
    # Discovery is private to this request. Its local eligibility is sufficient
    # for ranking, never for returning a body or sending one to a backend.
    boundary = service.store.transaction() if local_selection else operation(service, items)
    with boundary as db:
        frame = collect(service, db, request, context)
        if request["known_bundle"] is not None:
            verify_snapshot(service, db, request["known_bundle"], frame, context)
        prepared = [
            prepare_query(
                service, db, frame, query, request["selection"], context_mode=contextual(request)
            )
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
                    summaries=frame["summaries"],
                    now=service.clock(),
                    partitioned=True,
                )
                if settings
                else None
            )
            if plan["ranking"] is not None:
                discard_corrupt(db, plan["ranking"])
        if local_selection:
            rank_plans(service, prepared, backend_failed, frame=frame)
            evidence, results, _, chosen = _select_evidence(service, db, request, frame, prepared)
            groups, episodes, sources = dependencies(db, frame, evidence)
            selection = dict(
                scope=frame["scope"],
                group_ids=[item["group"]["semantic_group_id"] for item in groups],
                episode_ids=[item["episode_id"] for item in episodes],
                source_keys=[item["source_key"] for item in sources],
            )
            for plan, result, selected_ids in zip(prepared, results, chosen, strict=True):
                plan["ranked"] = [
                    document for document in plan["ranked"] if document["id"] in selected_ids
                ]
                # The final fit may remove stale rows, but must not fill their
                # space from candidates that this source boundary never proved.
                plan["selection_omissions"] = [
                    item
                    for item in result["omissions"]
                    if item in {"budget", "source_time_unavailable"}
                ]
            items = requirements(service, request, context, db=db, server_selection=selection)
        elif not any(plan["ranking"] is not None and plan["ranking"].external for plan in prepared):
            # The evidence and its negative/selection proof are produced from this
            # same fresh source boundary. Nothing outside the transaction ran.
            rank_plans(service, prepared, backend_failed, frame=frame)
            return assemble(service, db, request, context, frame, prepared)
    if not local_selection:
        rank_plans(service, prepared, backend_failed, frame=frame)
    with operation(service, items) as db:
        fresh = collect(service, db, request, context)
        _refresh_ranked(service, db, request, frame, fresh, prepared)
        return assemble(service, db, request, context, fresh, prepared)


def _refresh_ranked(service, db, request, frame, fresh, prepared):
    """Reuse eligibility after a boundary; never add or rerank a document."""
    for plan in prepared:
        _selection_units(frame, plan, context_mode=contextual(request))
        eligible = prepare_query(
            service,
            db,
            fresh,
            plan["subquery"],
            request["selection"],
            context_mode=contextual(request),
        )
        domain = eligible["domain"]
        if not contextual(request):
            require(
                domain["resolution"] == plan["domain"]["resolution"]
                and domain["digest"] == plan["domain"]["digest"],
                "scope_changed",
                409,
            )
        # Time/retention can change without a domain/version change. A new
        # record version or an episode's changed metadata cannot reuse old rank.
        documents = {item["id"]: item for item in eligible["documents"]}
        ranked = [
            documents[document["id"]]
            for document in plan["ranked"]
            if document["id"] in documents
            and document_binding(frame, document)
            == document_binding(fresh, documents[document["id"]])
        ]
        if "selection_units" in plan:
            current_ids = {document["id"] for document in ranked}
            complete_ids = {
                identity
                for unit in plan["selection_units"]
                if set(unit) <= current_ids
                for identity in unit
            }
            ranked = [document for document in ranked if document["id"] in complete_ids]
        plan["stale_ranked"] = len(ranked) != len(plan["ranked"])
        plan["ranked"] = ranked
        plan["domain"] = domain


def document_binding(frame, document):
    """The exact document and whole record versions seen before backend work."""
    records = (
        sorted((row["id"], row["version"]) for row in frame["group_map"][document["group_id"]][1])
        if document["kind"] != "episode"
        else []
    )
    # Reranking may hydrate episode text after the authoritative metadata read.
    # Its content hash still binds that body; the temporary text field is not
    # a source identity field and fresh evidence is rebuilt from the current DB.
    metadata = (
        {key: value for key, value in document.items() if key != "text"}
        if document["kind"] == "episode"
        else document
    )
    return metadata, records


def rank_plans(service, prepared, backend_failed, *, frame=None):
    from ..persistent_retrieval import (
        PreparedQueryVector,
        embed_query,
        rank_prepared,
        rerank_documents,
    )
    from .merge import by_owner, mmr_diversify, quota_merge, unit_items

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
            ranking = plan["ranking"]
            partitions = documents_by_owner(plan["documents"])
            limit = candidate_limit(ranking.settings["candidates"], len(partitions))
            if len(partitions) > 1 and ranking.available and ranking.query_vector is None:
                try:
                    value = embed_query(
                        service.retrieval,
                        plan["subquery"]["text"],
                        ranking.settings,
                        retrieval_deadline,
                    )
                except (Fault, OSError, TimeoutError, ValueError, TypeError, KeyError):
                    value = None
                ranking.query_vector = PreparedQueryVector(
                    plan["subquery"]["text"], canonical(ranking.settings), value
                )
            ranked, complete = {}, True
            ranking.channel_hits = dict(lexical=0, vector=0)
            ranking.rrf_count = 0
            for owner, documents in partitions.items():
                identities = {document["id"] for document in documents}
                owned = replace(
                    ranking,
                    by_id={identity: by_id[identity] for identity in identities},
                    document_count=len(documents),
                    lexical_ids=[
                        identity for identity in ranking.lexical_ids if identity in identities
                    ],
                    available={
                        identity: value
                        for identity, value in ranking.available.items()
                        if identity in identities
                    },
                    corrupted=[row for row in ranking.corrupted if row[0] in identities],
                    trace=None,
                    channel_hits=None,
                    rrf_count=None,
                    degradation=None,
                )
                ranked[owner], current = rank_prepared(
                    service.retrieval,
                    service.store,
                    plan["subquery"]["text"],
                    owned,
                    deadline=retrieval_deadline,
                    query_id=plan["subquery"]["query_id"],
                    limit=limit,
                    rerank=False,
                )
                complete &= current
                for name, count in owned.channel_hits.items():
                    ranking.channel_hits[name] += count
                ranking.rrf_count += owned.rrf_count
                if owned.degradation is not None:
                    ranking.degradation = owned.degradation
            plan["per_owner_ranked"] = ranked
            plan["ranked"] = interleave(ranked)
            plan["index_complete"] = complete and not backend_failed
            if frame is not None:
                units = _selection_units(frame, plan, context_mode=plan["context_mode"])
                positions = {
                    document["id"]: i for rows in ranked.values() for i, document in enumerate(rows)
                }
                items = unit_items(units, positions)
                vectors = {
                    item["id"]: ranking.available[item["id"]]
                    for item in items
                    if item["id"] in ranking.available
                }
                diverse = mmr_diversify(items, vectors)
                shortlist = quota_merge(by_owner(diverse), min(80, ranking.settings["candidates"]))
                plan["ranked"] = [member for item in shortlist for member in item["members"]]
                if "selection_units" in plan:
                    plan["selection_units"] = [
                        tuple(member["id"] for member in item["members"]) for item in shortlist
                    ]
            if ranking.settings["rerank"] and plan["ranked"]:
                plan["ranked"], reranked = rerank_documents(
                    service.retrieval,
                    service.store,
                    plan["subquery"]["text"],
                    plan["ranked"],
                    ranking.settings,
                    deadline=retrieval_deadline,
                )
                plan["index_complete"] &= reranked
                if not reranked:
                    ranking.degradation = "rerank_failed"
                if "selection_units" in plan:
                    positions = {document["id"]: i for i, document in enumerate(plan["ranked"])}
                    plan["selection_units"].sort(
                        key=lambda unit: min(positions[identity] for identity in unit)
                    )
            if ranking.settings.get("trace", False):
                from ..diagnostics import record_recall_trace
                from ..recall_trace import build

                ranking.trace = build(
                    plan["subquery"]["query_id"], ranking, plan["ranked"], plan["index_complete"]
                )
                ranking.trace["owners"] = len(partitions)
                record_recall_trace(ranking.trace)
        else:
            plan["per_owner_ranked"] = documents_by_owner(plan["ranked"])
            plan["ranked"] = interleave(plan["per_owner_ranked"])


def _summary_selection_key(frame, document):
    if document["kind"] == "episode":
        return None
    identity = document["group_id"]
    summary = frame["summaries"].get(identity)
    if summary is None or summary["kind"] != "summary":
        return None
    group, _, units = frame["group_map"][identity]
    return (
        summary["window_id"],
        canonical(json.loads(group["scope"])),
        tuple(sorted({unit["subject_person_id"] for unit in units})),
    )


def _selection_units(frame, plan, *, context_mode):
    """Fit one owner's eligible summaries from a frozen window together.

    Ranking still chooses which unit to try first. Companion summaries keep
    their own records and states; sharing a budget unit never merges facts.
    The first membership is retained across the final source boundary.
    """
    if not context_mode or plan["subquery"]["intent"] != "current":
        return [[document] for document in plan["ranked"]]
    if "selection_units" not in plan:
        companions = {}
        parents = {document["id"]: document["id"] for document in plan["documents"]}

        def root(identity):
            while parents[identity] != identity:
                parents[identity] = parents[parents[identity]]
                identity = parents[identity]
            return identity

        def connect(identities):
            for identity in identities[1:]:
                parents[root(identity)] = root(identities[0])

        for document in plan["documents"]:
            key = _summary_selection_key(frame, document)
            if key is not None:
                companions.setdefault(key, []).append(document)
        for members in companions.values():
            connect([member["id"] for member in members])
        by_group = {
            document["group_id"]: document["id"]
            for document in plan["documents"]
            if document["kind"] != "episode"
        }
        for groups in frame.get("event_links", []):
            if all(group in by_group for group in groups):
                connect([by_group[group] for group in groups])
        components = {}
        for document in plan["documents"]:
            components.setdefault(root(document["id"]), []).append(document)
        units, seen = [], set()
        positions = {document["id"]: index for index, document in enumerate(plan["ranked"])}
        for document in plan["ranked"]:
            if document["id"] in seen:
                continue
            members = components[root(document["id"])]
            members = sorted(
                members,
                key=lambda member: (positions.get(member["id"], len(positions)), member["id"]),
            )
            units.append(members)
            seen.update(member["id"] for member in members)
        plan["selection_units"] = [tuple(member["id"] for member in unit) for unit in units]
        plan["ranked"] = [member for unit in units for member in unit]
    documents = {document["id"]: document for document in plan["ranked"]}
    return [
        [documents[identity] for identity in unit]
        for unit in plan["selection_units"]
        if all(identity in documents for identity in unit)
    ]


def _select_evidence(service, db, request, frame, prepared):
    """Fit complete evidence groups once, for discovery and final assembly."""
    evidence, results, constraints = [], [], []
    chosen = []
    included = set()
    included_sources = set()
    included_owners = {canonical(frame["scope"])}
    authorized_owners = {canonical(entry[0]) for entry in frame["scopes"]}
    facts_count = episodes_count = 0
    for plan in prepared:
        query, domain = plan["subquery"], plan["domain"]
        resolution = domain["resolution"]
        result_ids, omissions, missing_time = [], list(plan.get("selection_omissions", ())), 0
        chosen_documents = set()
        witness_ids = [entry[0]["id"] for entry in domain["witnesses"]]
        from .merge import fit_order

        for documents in fit_order(
            _selection_units(frame, plan, context_mode=contextual(request)), request["limits"]
        ):
            batches = [
                fact_evidence(frame, document["group_id"])
                if document["kind"] != "episode"
                else [episode_evidence(db, document)]
                for document in documents
            ]
            if any(not batch for batch in batches):
                omissions.append("source_time_unavailable")
                continue
            batch = [item for members in batches for item in members]
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
            added_sources = evidence_source_keys(db, added)
            added_owners = {
                *[canonical(item["owner_scope"]) for item in added],
                *source_owners(db, added_sources),
            }
            require_selection(added_owners <= authorized_owners)
            if (
                facts_count + new_facts > request["limits"]["facts"]
                or episodes_count + new_episodes > request["limits"]["episodes"]
                or total > request["budget"]["bytes"]
                or total > request["budget"]["tokens"]
                or len(included_owners | added_owners) > MAX_BUNDLE_OWNERS
                or len(included_sources | added_sources) > 256
            ):
                omissions.append("budget")
                continue
            evidence.extend(added)
            chosen_documents.update(document["id"] for document in documents)
            included.update(item["evidence_id"] for item in added)
            included_sources.update(added_sources)
            included_owners.update(added_owners)
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
        if plan.get("stale_ranked"):
            omissions.append("stale_source")
        incomplete = bool(omissions) or not domain["complete"]
        status = "incomplete" if incomplete else "matched" if result_ids else "no_match"
        if unresolved and not result_ids:
            status = "unresolved"
        proofs = ["positive_sources"]
        if contextual(request):
            status = "incomplete"
            omissions.append("context_only")
        elif any(
            target["kind"] == "named" and target["status"] == "resolved"
            for target in resolution["targets"]
        ):
            proofs.append("unique_owner")
        if not contextual(request) and query["intent"] == "current":
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
                    matched_facts=sum(item["kind"] != "episode" for item in plan["ranked"]),
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
        chosen.append(chosen_documents)
    return evidence, results, constraints, chosen


def assemble(service, db, request, context, frame, prepared):
    evidence, results, constraints, chosen = _select_evidence(service, db, request, frame, prepared)
    for plan, result, selected in zip(prepared, results, chosen, strict=True):
        ranking = plan["ranking"]
        if ranking is not None and ranking.trace is not None:
            ranking.trace.update(
                selected_ids=sorted(selected), truncation_reasons=result["omissions"]
            )
            ranking.trace["distinct_subjects"] = len(
                {document["scope"] for document in plan["documents"] if document["id"] in selected}
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
