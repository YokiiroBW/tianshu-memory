"""Requester authorization, explicit private associations and original group owners."""

import json
from contextlib import contextmanager

from ..domain import Fault, canonical, require
from ..recall_index import eligible_groups
from .migration import ready

GROUP_WINDOW = 4096


def group_read(service, scope):
    # Fixture authority has no authenticated original group admissions. It retains
    # its exact-scope behavior; production never treats fixtures as sharing proof.
    return scope["audience"] == "group" and service.synchronized


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


def group_rows(db, requester, *, owners=None, selection=None):
    """One bounded metadata window, with every lineage in its real original scope.

    No body is read until these origin checks and eligible_groups' current-source
    checks pass. An active projection alone cannot launder a private dependency.
    """
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
    if owners is not None:
        predicates.append("g.scope IN (" + ",".join("?" for _ in owners) + ")")
        parameters.extend(canonical(owner) for owner in owners)
    if selection is not None:
        predicates.append("g.category IN (" + ",".join("?" for _ in selection) + ")")
        parameters.extend(selection)
    rows = db.execute(
        "SELECT g.* FROM groups g WHERE "
        + " AND ".join(predicates)
        + " ORDER BY g.rowid DESC LIMIT ?",
        [*parameters, GROUP_WINDOW + 1],
    ).fetchall()
    return rows[:GROUP_WINDOW], len(rows) <= GROUP_WINDOW


def group_candidates(service, db, requester, *, owners=None, selection=None):
    rows, complete = group_rows(db, requester, owners=owners, selection=selection)
    owner_scopes = {row["id"]: json.loads(row["scope"]) for row in rows}
    return eligible_groups(service, db, rows, requester, scopes_by_group=owner_scopes), complete


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
    if len(scopes) > 1:
        # The bound applies to this actual owner set, not a roster of the room.
        candidates, _ = group_candidates(
            service, db, requester, owners=[entry[0] for entry in scopes[1:]]
        )
        readable = {entry[0]["scope"] for entry in candidates}
        require(
            all(canonical(owner) in readable for owner, _, _ in scopes[1:]),
            "scope_changed",
            409,
        )
    return scopes


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
            scopes = (
                [(scope, None, 1)]
                if observed is None
                else observed_group_scopes(service, db, scope, observed)
            )
            yield db, scopes
            return
        scopes = authorized_scopes(service, db, scope, request["include_associated"])
        if len(scopes) == 1:
            yield db, scopes
            return
    # Explicit persisted private consent; never forge an origin for the other owner.
    for other, _, _ in scopes[1:]:
        with service.operation(scope=other):
            pass
    with service.operation(scope=scope, context=context) as db:
        ready(db)
        service._authorize(db, context, scope=scope)
        require(
            authorized_scopes(service, db, scope, request["include_associated"]) == scopes,
            "scope_changed",
            409,
        )
        yield db, scopes
