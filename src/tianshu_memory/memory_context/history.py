"""History validity is independent of additive memory content revisions."""

import json
from datetime import timedelta

from ..domain import canonical, fingerprint, parse_time, require, utc
from .migration import identity_version
from .scopes import checks, verified_scopes


def metadata_key(scope):
    return "context_history:" + fingerprint(scope)


def history_version(service, db, scope, *, input_only=False):
    current = service._scope_version(db, scope)
    row = db.execute("SELECT value FROM metadata WHERE key=?", (metadata_key(scope),)).fetchone()
    if row is None:
        return current
    return _retained_versions(current, json.loads(row[0]))[bool(input_only)]


def postgres_history_versions(db, scope):
    """Read both prior epochs together, immediately before a PG scope bump."""
    row = db.native(
        "SELECT COALESCE((SELECT version FROM scopes WHERE key=?),1),"
        "(SELECT value FROM metadata WHERE key=?)",
        (canonical(scope), metadata_key(scope)),
    ).fetchone()
    if row[1] is None:
        return row[0], row[0]
    return _retained_versions(row[0], json.loads(row[1]))


def _retained_versions(current, value):
    # An unclassified writer, or an old database, must not preserve stale history.
    if value["scope_version"] != current:
        return current, current
    # Older databases inherit the stricter epoch; no prior withdrawal is undone.
    return value["history_version"], value.get("source_history_version", value["history_version"])


def record_version(db, scope, content_version, retained_version, source_version):
    db.execute(
        "INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            metadata_key(scope),
            canonical(
                dict(
                    scope_version=content_version,
                    history_version=retained_version,
                    source_history_version=source_version,
                )
            ),
        ),
    )


def source_version_retained(service, db, scope, version):
    """Sourced work survives fact updates, never a future or withdrawn source epoch."""
    current = service._scope_version(db, scope)
    return version == current or (
        version < current and history_version(service, db, scope, input_only=True) <= version
    )


def execute(application, request, context, *, input_only=False):
    service = application.service
    scope = request["requested_scope"]
    with verified_scopes(service, request, context) as (db, scopes):
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
                    history_version=history_version(service, db, other, input_only=input_only),
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
