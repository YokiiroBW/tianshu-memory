"""Bounded candidate retrieval preserving whole groups and their authorized scopes."""

import json
from datetime import timedelta

from ..domain import Fault, canonical, parse_time, query_terms, require, utc
from .migration import identity_version, ready
from .times import filter_candidates, prepare


def authorized_scopes(service, db, scope, include_associated):
    result = [(scope, None, 1)]
    if not include_associated or scope["audience"] != "self_private":
        return result
    rows = db.execute(
        "SELECT * FROM context_associations WHERE actor_id=? AND state='linked' AND (source_person=? OR target_person=?) "
        "AND (json_extract(scopes,'$[0]')=? OR json_extract(scopes,'$[1]')=?) ORDER BY id LIMIT 17",
        (
            scope["actor_id"],
            scope["person_id"],
            scope["person_id"],
            canonical(scope),
            canonical(scope),
        ),
    ).fetchall()
    if len(rows) > 16:
        raise Fault("budget_exceeded", 429)
    for row in rows:
        scopes = json.loads(row["scopes"])
        if scope not in scopes:
            continue
        bindings = [
            service._binding(db, json.loads(row[key]))
            for key in ("source_account", "target_account")
        ]
        if any(
            binding is None
            or binding["person_id"] != row[person]
            or binding["version"] != row[version]
            for binding, person, version in zip(
                bindings,
                ("source_person", "target_person"),
                ("source_binding", "target_binding"),
                strict=True,
            )
        ):
            continue
        other = scopes[1] if scopes[0] == scope else scopes[0]
        if other not in [entry[0] for entry in result]:
            result.append((other, row["id"], row["version"]))
    return result


def checks(service, db, scopes):
    return [
        dict(
            scope=scope,
            scope_version=service._scope_version(db, scope),
            association_id=association,
            association_version=version,
        )
        for scope, association, version in scopes
    ]


def execute(application, request, context):
    service = application.service
    scope = request["requested_scope"]
    period = request["time_range"]
    if period is not None:
        require(parse_time(period["from"]) < parse_time(period["to"]), "invalid_input", 400)
    # Establish the requester's source barrier before finding association-controlled scopes.
    with service.operation(scope=scope, context=context) as db:
        ready(db)
        service._authorize(db, context, scope=scope)
        scopes = authorized_scopes(service, db, scope, request["include_associated"])
    # Memory's persisted, purpose-bound consent supplies access to these exact other scopes.
    # The ordinary source-owner grants are still independently rechecked; no origin is forged.
    for other, _, _ in scopes[1:]:
        with service.operation(scope=other):
            pass
    with service.operation(scope=scope, context=context) as db:
        ready(db)
        service._authorize(db, context, scope=scope)
        current_scopes = authorized_scopes(service, db, scope, request["include_associated"])
        require(current_scopes == scopes, "scope_changed", 409)
        version = service._scope_version(db, scope)
        association_version = identity_version(db, scope)
        scope_checks = checks(service, db, scopes)
        if (
            request["known_scope_version"] not in (None, version)
            or request["known_association_version"] not in (None, association_version)
            or request["known_scope_checks"] not in (None, scope_checks)
        ):
            raise Fault("scope_changed", 409, version)
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
        if not exact and not search_terms:
            result["omissions"] = ["no_match"]
            return result
        candidates = []
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in search_terms)
        for selected_scope, _, _ in scopes:
            parameters = [canonical(selected_scope), *request["selection"]]
            predicates = [
                "g.scope=?",
                "g.state='active'",
                "g.category IN (" + ",".join("?" for _ in request["selection"]) + ")",
            ]
            if exact:
                predicates.append(f"g.{exact_kind}_key=?")
                parameters.append(exact_value)
            else:
                predicates.append(
                    "EXISTS(SELECT 1 FROM records r JOIN search_index i ON i.record_id=r.id AND i.record_version=r.version WHERE r.group_id=g.id AND i.scope=g.scope AND i.text MATCH ?)"
                )
                parameters.append(expression)
            parameters.append(request["limit"] + 1)
            rows = db.execute(
                "SELECT g.* FROM groups g WHERE "
                + " AND ".join(predicates)
                + " ORDER BY g.rowid DESC LIMIT ?",
                parameters,
            ).fetchall()
            if len(rows) > request["limit"]:
                result["coverage"]["complete"] = False
                rows = rows[: request["limit"]]
            for group in rows:
                if not service._group_current(db, group["id"]):
                    continue
                records = db.execute(
                    "SELECT * FROM records WHERE group_id=? ORDER BY id", (group["id"],)
                ).fetchall()
                if set(json.loads(group["members"])) != {r["id"] for r in records} or any(
                    r["state"] != "active" for r in records
                ):
                    continue
                units = [json.loads(r["payload"]) for r in records]
                if any(u["subject_person_id"] != selected_scope["person_id"] for u in units):
                    continue
                if selected_scope["audience"] == "group" and any(
                    u["visibility"] != "shared_projection"
                    or any(s["kind"] != "shareable_projection" for s in u["sources"])
                    for u in units
                ):
                    continue
                if service._projections_current(db, units):
                    candidates.append((group, records, units))
        if not exact:
            candidates = service._rank_groups(db, candidates, search_terms)
        if period is None or not candidates:
            return finish(service, db, request, context, candidates, result)
        prepared = prepare(db, candidates)
    # Remote content reads happen without the SQLite authority write lock. The
    # final transaction rejects any local authority change during these reads.
    candidates, missing = filter_candidates(service, prepared, candidates, scope, context, period)
    with service.store.transaction() as db:
        require(
            int(db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0])
            == prepared["revision"],
            "dependency_unavailable",
            503,
        )
        service._authorize(db, context, scope=scope)
        require(
            authorized_scopes(service, db, scope, request["include_associated"]) == scopes
            and checks(service, db, scopes) == scope_checks,
            "scope_changed",
            409,
        )
        result["coverage"]["missing_source_times"] = missing
        result["coverage"]["complete"] &= not missing
        return finish(service, db, request, context, candidates, result)


def finish(service, db, request, context, candidates, result):
    result["coverage"]["matched_groups"] = len(candidates)
    result.update(service._fit_groups(candidates[: request["limit"]], request["budget"]))
    if result["coverage"]["missing_source_times"]:
        result["omissions"].append("source_time_unavailable")
    result["coverage"]["returned_groups"] = len(result["dependency_groups"])
    result["coverage"]["complete"] &= (
        len(candidates) <= request["limit"] and "budget" not in result["omissions"]
    )
    service._authorize(db, context, scope=request["requested_scope"])
    verified = service.clock()
    result["verified_at"] = utc(verified)
    result["valid_until"] = utc(
        min(verified + timedelta(seconds=30), parse_time(context["expires_at"]))
    )
    return result
