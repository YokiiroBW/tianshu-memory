"""Bounded candidate retrieval preserving whole groups and their authorized scopes."""

from datetime import timedelta

from ..domain import (
    DependencyChanged,
    canonical,
    parse_time,
    query_terms,
    require,
    terms,
    utc,
)
from ..fact_migration import installed
from ..recall_language import candidate_aliases, history_query, self_reference
from ..recall_relevance import named_owner
from .migration import identity_version
from .scopes import checks, group_candidates, group_read, verified_scopes
from .times import filter_candidates, prepare


def keyword_candidates(scope, query, search_terms):
    """One indexed candidate pass; never a correlated FTS scan per group."""
    hints = set(search_terms)
    for alias in candidate_aliases(query):
        hints.update(terms(alias))
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in sorted(hints))
    return (
        "SELECT r.group_id FROM search_index i JOIN records r "
        "ON r.id=i.record_id AND r.version=i.record_version "
        "WHERE i.scope=? AND i.text MATCH ?",
        [scope, expression],
    )


def execute(application, request, context):
    service = application.service
    settings = service.retrieval.settings_for(request) if service.retrieval else None
    scope = request["requested_scope"]
    shared = group_read(service, scope)
    period = request["time_range"]
    if period is not None:
        require(parse_time(period["from"]) < parse_time(period["to"]), "invalid_input", 400)
    with verified_scopes(service, request, context) as (db, scopes, approved):
        version = service._scope_version(db, scope)
        association_version = identity_version(db, scope)
        scope_checks = checks(service, db, scopes)
        if (
            request["known_scope_version"] not in (None, version)
            or request["known_association_version"] not in (None, association_version)
            or request["known_scope_checks"] not in (None, scope_checks)
        ):
            raise DependencyChanged(version)
        verified = service.clock()
        result = dict(
            schema_version=1,
            request_id=request["query"]["request_id"],
            effective_scope=scope,
            scope_version=version,
            verified_at=utc(verified),
            valid_until=utc(
                min(verified + timedelta(seconds=30), parse_time(context["expires_at"]))
            ),
            selected_units=[],
            dependency_groups=[],
            budget_used={"tokens": 0, "bytes": 0},
            omissions=[],
            version_domain="memory-context/v1",
            association_version=association_version,
            scope_checks=scope_checks,
            coverage=dict(
                matched_groups=0,
                returned_groups=0,
                complete=True,
                time_basis="source_sent_at",
                history_complete=False,
                missing_source_times=0,
            ),
        )
        if not all(request["budget"].values()):
            result["omissions"] = ["budget"]
            result["coverage"]["complete"] = False
            return result
        exact_kind, separator, exact_value = request["query_text"].partition(":")
        exact = separator and exact_kind in {"field", "item"}
        search_terms = [] if exact else query_terms(request["query_text"])
        if not exact and not search_terms and (not settings or settings["mode"] == "keyword"):
            result["omissions"] = ["no_match"]
            return result
        candidates, witnesses = [], []
        authorized_group_ids = None
        ranking_query = request["query_text"]
        if shared:
            owners = [scope] if self_reference(ranking_query) else None
            # Name uniqueness and target relevance share one window across categories;
            # an unselected identity category must not hide a same-name other owner.
            candidates, complete = group_candidates(
                service, db, scope, owners=owners, approved=approved
            )
            authorized_group_ids = frozenset(entry[0]["id"] for entry in candidates)
            result["coverage"]["complete"] &= complete
        for selected_scope, _, _ in [] if shared else scopes:
            parameters = [canonical(selected_scope), *request["selection"]]
            predicates = [
                "g.scope=?",
                "g.state='active'",
                "g.category IN (" + ",".join("?" for _ in request["selection"]) + ")",
            ]
            if exact:
                from ..fact_names import field_values

                values = field_values(exact_kind, exact_value)
                predicates.append(
                    f"g.{exact_kind}_key"
                    + ("=?" if len(values) == 1 else " IN (" + ",".join("?" for _ in values) + ")")
                )
                parameters.extend(values)
            elif settings is None or settings["mode"] == "keyword":
                query_sql, query_parameters = keyword_candidates(
                    canonical(selected_scope), request["query_text"], search_terms
                )
                if installed(db) and not history_query(request["query_text"]):
                    from ..fact_references import expand_candidates

                    query_sql, query_parameters = expand_candidates(
                        query_sql, query_parameters, canonical(selected_scope)
                    )
                if installed(db) and history_query(request["query_text"]):
                    from ..fact_entities import historical_candidates

                    history_sql, history_parameters = historical_candidates(
                        canonical(selected_scope), request["query_text"]
                    )
                    if history_sql:
                        query_sql += " UNION " + history_sql
                        query_parameters += history_parameters
                predicates.append("g.id IN (" + query_sql + ")")
                parameters += query_parameters
            # Rank a bounded surplus, preserving explicit incomplete coverage.
            # Alias/history expansion happens before this bound, not afterwards.
            keyword = settings is None or settings["mode"] == "keyword"
            candidate_limit = (
                request["limit"] if exact else min(4096, request["limit"] * 4) if keyword else 4096
            )
            parameters.append(candidate_limit + 1)
            rows = db.execute(
                "SELECT g.* FROM groups g WHERE "
                + " AND ".join(predicates)
                + " ORDER BY g.rowid DESC LIMIT ?",
                parameters,
            ).fetchall()
            if len(rows) > candidate_limit:
                result["coverage"]["complete"] = False
                rows = rows[:candidate_limit]
            from ..recall_index import eligible_groups

            candidates.extend(eligible_groups(service, db, rows, selected_scope))
        from ..automatic_facts import filter_current
        from ..fact_queries import snapshot_for_groups

        fact_snapshot = snapshot_for_groups(
            db,
            candidates,
            service=service,
            domain_complete=result["coverage"]["complete"] if shared else None,
        )
        if fact_snapshot is not None and fact_snapshot.names is not None:
            result["coverage"]["complete"] &= not fact_snapshot.names.incomplete
        if exact:
            candidates = [
                entry
                for entry in candidates
                if (
                    fact_snapshot.names.field_matches(entry[0], exact_kind, exact_value)
                    if fact_snapshot is not None and fact_snapshot.names is not None
                    else entry[0][exact_kind + "_key"] == exact_value
                )
            ]
        if shared and not exact and not self_reference(ranking_query):
            candidates, witnesses, ranking_query = named_owner(
                service,
                db,
                candidates,
                ranking_query,
                complete=result["coverage"]["complete"],
                fact_snapshot=fact_snapshot,
            )
            search_terms = query_terms(ranking_query)
        if shared:
            candidates = [
                entry for entry in candidates if entry[0]["category"] in request["selection"]
            ]
            if any(entry[0]["category"] not in request["selection"] for entry in witnesses):
                candidates = []
        candidates = filter_current(
            db, candidates, ranking_query, service.clock, fact_snapshot=fact_snapshot
        )
        lexical = (
            service._rank_groups(
                db,
                candidates,
                search_terms,
                ranking_query,
                fact_snapshot=fact_snapshot,
                authorized_group_ids=authorized_group_ids,
            )
            if not exact
            else candidates
        )
        if witnesses:
            witness_ids = {entry[0]["id"] for entry in witnesses}
            matched_ids = {entry[0]["id"] for entry in lexical}
            candidates = [
                entry
                for entry in candidates
                if entry[0]["id"] not in witness_ids or entry[0]["id"] in matched_ids
            ]
        remote_rank = settings is not None and not exact
        if not remote_rank:
            candidates = lexical
        if not remote_rank and (period is None or not candidates):
            return finish(service, db, request, context, candidates, result, witnesses=witnesses)
        prepared = prepare(db, together(witnesses, candidates))
    # Model and content I/O never hold the authority write transaction.
    missing = 0
    if period is not None and candidates:
        ranged, missing = filter_candidates(service, prepared, candidates, scope, context, period)
        allowed = {entry[0]["id"] for entry in ranged}
        candidates = [entry for entry in candidates if entry[0]["id"] in allowed]
        lexical = [entry for entry in lexical if entry[0]["id"] in allowed]
    if remote_rank:
        result["coverage"]["complete"] &= len(candidates) <= settings["candidates"]
        candidates = service.retrieval.rank(ranking_query, candidates, lexical, settings)
    with verified_scopes(service, request, context) as (db, current_scopes, approved):
        require(
            int(db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0])
            == prepared["revision"],
            "dependency_unavailable",
            503,
        )
        service._authorize(db, context, scope=scope)
        require(
            current_scopes == scopes and checks(service, db, scopes) == scope_checks,
            "scope_changed",
            409,
        )
        if witnesses:
            # A clock boundary can change current names without any DML/revision.
            # Reuse the same bounded authorized domain to reprove both this witness
            # and uniqueness against all other owners, not just the returned owner.
            fresh, complete = group_candidates(service, db, scope, approved=approved)
            fresh_snapshot = snapshot_for_groups(
                db, fresh, service=service, domain_complete=complete
            )
            _, current_witnesses, current_query = named_owner(
                service,
                db,
                fresh,
                request["query_text"],
                complete=complete,
                fact_snapshot=fresh_snapshot,
            )
            require(
                [(entry[0]["id"], entry[0]["scope"]) for entry in current_witnesses]
                == [(entry[0]["id"], entry[0]["scope"]) for entry in witnesses]
                and current_query == ranking_query,
                "scope_changed",
                409,
            )
        service._verify_retrieval(db, together(witnesses, candidates))
        result["coverage"]["missing_source_times"] = missing
        result["coverage"]["complete"] &= not missing
        return finish(service, db, request, context, candidates, result, witnesses=witnesses)


def together(first, second):
    identities = {entry[0]["id"] for entry in first}
    return first + [entry for entry in second if entry[0]["id"] not in identities]


def finish(service, db, request, context, candidates, result, *, witnesses=()):
    scope = request["requested_scope"]
    shared = group_read(service, scope)
    required = list(witnesses) if candidates else []
    ordered = together(required, candidates)
    result["coverage"]["matched_groups"] = len(ordered)
    fitted = service._fit_groups(
        ordered[: request["limit"]],
        request["budget"],
        requester_scope=scope if shared else None,
    )
    returned = {entry["semantic_group_id"] for entry in fitted["dependency_groups"]}
    if required and (
        not all(entry[0]["id"] in returned for entry in required)
        or not any(entry[0]["id"] in returned for entry in candidates)
    ):
        # No target-only precision when the name's original full witness did not fit.
        fitted = dict(
            selected_units=[],
            dependency_groups=[],
            budget_used={"tokens": 0, "bytes": 0},
            omissions=["budget"],
        )
    result.update(fitted)
    if shared:
        selected = [scope]
        for unit in result["selected_units"]:
            owner = dict(scope, person_id=unit["subject_person_id"])
            if owner not in selected:
                selected.append(owner)
        result["scope_checks"] = checks(service, db, [(owner, None, 1) for owner in selected])
    if result["coverage"]["missing_source_times"]:
        result["omissions"].append("source_time_unavailable")
    result["coverage"]["returned_groups"] = len(result["dependency_groups"])
    result["coverage"]["complete"] &= len(ordered) <= request["limit"] and not {
        "budget",
        "scope_filtered",
    }.intersection(result["omissions"])
    service._authorize(db, context, scope=request["requested_scope"])
    verified = service.clock()
    result["verified_at"] = utc(verified)
    result["valid_until"] = utc(
        min(verified + timedelta(seconds=30), parse_time(context["expires_at"]))
    )
    return result
