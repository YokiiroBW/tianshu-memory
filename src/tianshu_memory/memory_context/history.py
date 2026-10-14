"""History validity is independent of additive memory content revisions."""

import json
from datetime import timedelta

from ..domain import canonical, fingerprint, parse_time, require, utc
from .migration import identity_version, ready
from .query import authorized_scopes, checks


def metadata_key(scope):
    return "context_history:" + fingerprint(scope)


def history_version(service, db, scope):
    current = service._scope_version(db, scope)
    row = db.execute("SELECT value FROM metadata WHERE key=?", (metadata_key(scope),)).fetchone()
    if row is None:
        return current
    value = json.loads(row[0])
    # An unclassified writer, or an old database, must not preserve stale history.
    if value["scope_version"] != current:
        return current
    return value["history_version"]


def record_version(db, scope, content_version, retained_version):
    db.execute(
        "INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            metadata_key(scope),
            canonical(dict(scope_version=content_version, history_version=retained_version)),
        ),
    )


def execute(application, request, context):
    service = application.service
    scope = request["requested_scope"]
    with service.operation(scope=scope, context=context) as db:
        ready(db)
        service._authorize(db, context, scope=scope)
        scopes = authorized_scopes(service, db, scope, request["include_associated"])
    for other, _, _ in scopes[1:]:
        with service.operation(scope=other):
            pass
    with service.operation(scope=scope, context=context) as db:
        service._authorize(db, context, scope=scope)
        require(
            scopes == authorized_scopes(service, db, scope, request["include_associated"]),
            "scope_changed",
            409,
        )
        version = service._scope_version(db, scope)
        association = identity_version(db, scope)
        expected = request["expected_snapshot"]
        if expected is not None:
            require(
                expected
                == dict(
                    scope_version=version,
                    association_version=association,
                    scope_checks=checks(service, db, scopes),
                ),
                "scope_changed",
                409,
            )
        snapshot = dict(
            association_version=association,
            history_checks=[
                dict(
                    scope=other,
                    history_version=history_version(service, db, other),
                    association_id=link,
                    association_version=epoch,
                )
                for other, link, epoch in scopes
            ],
        )
        require(request["known_snapshot"] in (None, snapshot), "scope_changed", 409)
        verified = service.clock()
        return dict(
            schema_version=1,
            request_id=request["query"]["request_id"],
            effective_scope=scope,
            scope_version=version,
            snapshot=snapshot,
            verified_at=utc(verified),
            valid_until=utc(
                min(verified + timedelta(seconds=30), parse_time(context["expires_at"]))
            ),
        )
