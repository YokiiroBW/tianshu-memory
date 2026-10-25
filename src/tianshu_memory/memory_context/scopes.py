"""Requester authorization, explicit private associations and original group owners."""

import json
from contextlib import contextmanager

from ..domain import canonical, require, require_dependency
from ..recall_index import eligible_groups
from .authorization import authorized_scopes
from .migration import ready

GROUP_WINDOW = 4096


def group_read(service, scope):
    # Fixture authority has no authenticated original group admissions. It retains
    # its exact-scope behavior; production never treats fixtures as sharing proof.
    return scope["audience"] == "group" and service.synchronized


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


def group_rows(db, requester, *, owners=None, selection=None, after=None, limit=None):
    """One bounded metadata window, with every lineage in its real original scope.

    No body is read until these origin checks and eligible_groups' current-source
    checks pass. An active projection alone cannot launder a private dependency.
    """
    limit = GROUP_WINDOW if limit is None else limit
    predicates = [
        "g.state='active'",
        "json_extract(g.scope,'$.actor_id')=?",
        "json_extract(g.scope,'$.audience')='group'",
        "json_extract(g.scope,'$.conversation_id')=?",
        "EXISTS (SELECT 1 FROM lineage l WHERE l.group_id=g.id)",
        "NOT EXISTS (SELECT 1 FROM lineage l LEFT JOIN sources s ON s.key=l.source_key "
        "LEFT JOIN source_admissions a ON a.key=s.key "
        "LEFT JOIN physical_sources p ON p.key=a.physical_key WHERE l.group_id=g.id "
        "AND (s.scope IS NULL OR s.scope!=g.scope OR a.payload IS NULL OR a.verified!=1 "
        "OR json_extract(a.payload,'$.scope.actor_id')!=json_extract(g.scope,'$.actor_id') "
        "OR json_extract(a.payload,'$.scope.person_id')!=json_extract(g.scope,'$.person_id') "
        "OR json_extract(a.payload,'$.scope.audience')!='group' "
        "OR json_extract(a.payload,'$.scope.conversation_id')!=json_extract(g.scope,'$.conversation_id') "
        "OR p.payload IS NULL "
        "OR json_extract(p.payload,'$.audience')!='group' "
        "OR json_extract(p.payload,'$.conversation_id')!=json_extract(g.scope,'$.conversation_id')))",
    ]
    parameters = [requester["actor_id"], requester["conversation_id"]]
    from ..segment_migration import installed as segments_installed

    if segments_installed(db):
        # Both branches are real owner proofs. In particular a private archive
        # can never acquire a group scope by producing a projection row.
        predicates[-1] = (
            "NOT EXISTS (SELECT 1 FROM lineage l LEFT JOIN sources s ON s.key=l.source_key "
            "WHERE l.group_id=g.id AND (s.scope IS NULL OR s.scope!=g.scope OR NOT ("
            "EXISTS (SELECT 1 FROM source_admissions a JOIN physical_sources p ON p.key=a.physical_key "
            "WHERE a.key=s.key AND a.verified=1 AND "
            "json_extract(a.payload,'$.scope.actor_id')=json_extract(g.scope,'$.actor_id') AND "
            "json_extract(a.payload,'$.scope.person_id')=json_extract(g.scope,'$.person_id') AND "
            "json_extract(a.payload,'$.scope.audience')='group' AND "
            "json_extract(a.payload,'$.scope.conversation_id')=json_extract(g.scope,'$.conversation_id') AND "
            "json_extract(p.payload,'$.audience')='group' AND "
            "json_extract(p.payload,'$.conversation_id')=json_extract(g.scope,'$.conversation_id')) OR "
            "EXISTS (SELECT 1 FROM memory_materials m WHERE m.source_key=s.key AND m.scope=g.scope "
            "AND m.kind='observation' AND m.verified=1 AND "
            "json_extract(m.frame,'$.audience')='group' AND "
            "json_extract(m.frame,'$.actor_id')=json_extract(g.scope,'$.actor_id') AND "
            "json_extract(m.frame,'$.conversation_id')=json_extract(g.scope,'$.conversation_id')))))"
        )
    if after is not None:
        predicates.append("g.id>?")
        parameters.append(after)
    if owners is not None:
        predicates.append("g.scope IN (" + ",".join("?" for _ in owners) + ")")
        parameters.extend(canonical(owner) for owner in owners)
    if selection is not None:
        predicates.append("g.category IN (" + ",".join("?" for _ in selection) + ")")
        parameters.extend(selection)
    rows = db.execute(
        "SELECT g.* FROM groups g WHERE "
        + " AND ".join(predicates)
        + (" ORDER BY g.id LIMIT ?" if after is not None else " ORDER BY g.rowid DESC LIMIT ?"),
        [*parameters, limit + 1],
    ).fetchall()
    return rows[:limit], len(rows) <= limit


def group_candidates(service, db, requester, *, owners=None, selection=None, approved=()):
    rows, complete = group_rows(db, requester, owners=owners, selection=selection)
    owner_scopes = {row["id"]: json.loads(row["scope"]) for row in rows}
    retained = {entry[0]["id"]: entry for entry in approved}
    reused = {
        row["id"]: retained[row["id"]]
        for row in rows
        if row["id"] in retained and dict(row) == dict(retained[row["id"]][0])
    }
    remaining = [row for row in rows if row["id"] not in reused]
    reused.update(
        (entry[0]["id"], entry)
        for entry in eligible_groups(
            service, db, remaining, requester, scopes_by_group=owner_scopes
        )
    )
    return [reused[row["id"]] for row in rows if row["id"] in reused], complete


def observed_group_scopes(service, db, requester, observed):
    """Reprove only previously used owners; a supplied owner list is not authority."""
    require(1 <= len(observed) <= 17, "scope_changed", 409)
    scopes = []
    for entry in observed:
        owner = entry["scope"]
        require(
            owner == dict(requester, person_id=owner["person_id"])
            and entry["association_id"] is None
            and entry["association_version"] == 1
            and owner not in [item[0] for item in scopes],
            "scope_changed",
            409,
        )
        scopes.append((owner, None, 1))
    require(scopes[0][0] == requester, "scope_changed", 409)
    candidates = []
    if len(scopes) > 1:
        # The bound applies to this actual owner set, not a roster of the room.
        candidates, _ = group_candidates(
            service, db, requester, owners=[entry[0] for entry in scopes[1:]]
        )
        readable = {entry[0]["scope"] for entry in candidates}
        require_dependency(all(canonical(owner) in readable for owner, _, _ in scopes[1:]))
    return scopes, candidates


def prior_checks(request):
    if "known_scope_checks" in request:
        return request["known_scope_checks"]
    expected = request["expected_snapshot"]
    known = request["known_snapshot"]
    if expected is not None:
        if known is not None:
            require(
                [
                    (e["scope"], e["association_id"], e["association_version"])
                    for e in expected["scope_checks"]
                ]
                == [
                    (e["scope"], e["association_id"], e["association_version"])
                    for e in known["history_checks"]
                ],
                "scope_changed",
                409,
            )
        return expected["scope_checks"]
    return None if known is None else known["history_checks"]


@contextmanager
def verified_scopes(service, request, context):
    scope = request["requested_scope"]
    shared = group_read(service, scope)
    with service.operation(scope=scope, context=context, group_read=shared) as db:
        ready(db)
        service._authorize(db, context, scope=scope)
        if shared:
            observed = prior_checks(request)
            scopes, approved = (
                ([(scope, None, 1)], [])
                if observed is None
                else observed_group_scopes(service, db, scope, observed)
            )
            # The read transaction still owns the authority lock. Reuse these
            # exact complete groups only within this operation; later HTTP or
            # external-ranking phases create a fresh proof and candidate set.
            yield db, scopes, approved
            return
        scopes = authorized_scopes(service, db, scope, request["include_associated"])
        if len(scopes) == 1:
            yield db, scopes, []
            return
    # Explicit persisted private consent; never forge an origin for the other owner.
    for other, _, _ in scopes[1:]:
        with service.operation(scope=other):
            pass
    with service.operation(scope=scope, context=context) as db:
        ready(db)
        service._authorize(db, context, scope=scope)
        require_dependency(
            authorized_scopes(service, db, scope, request["include_associated"]) == scopes
        )
        yield db, scopes, []
