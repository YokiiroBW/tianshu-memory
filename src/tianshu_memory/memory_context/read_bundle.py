"""Authenticated component verification within one complete source boundary."""

import hashlib
import hmac
from contextlib import contextmanager

from ..domain import canonical, parse_time, require
from .scopes import authorized_scopes, group_read


def selection_proof(db, bundle):
    """Authenticate server-selected semantics; freshness still requires the full source barrier."""
    key = db.execute("SELECT value FROM metadata WHERE key='memory_selection_proof_key'").fetchone()
    require(key is not None and len(key[0]) == 64, "dependency_unavailable", 503)
    # These fields are freshly recomputed by every verify. Every other field,
    # including future schema additions, belongs to the immutable selection.
    fresh = {"bundle_id", "verified_at", "valid_until", "owner_heads", "source_revision"}
    payload = {name: value for name, value in bundle.items() if name not in fresh}
    digest = hmac.new(
        bytes.fromhex(key[0]),
        b"memory-context/v2/selection\0" + canonical(payload).encode(),
        hashlib.sha256,
    ).hexdigest()
    return "bundle:" + digest


def verify_selection_proof(db, bundle):
    require(
        hmac.compare_digest(bundle["bundle_id"], selection_proof(db, bundle)), "scope_changed", 409
    )


def requirements(service, request, context, legacy=()):
    scope = request["requested_scope"]
    result = []
    with service.store.transaction() as db:
        service._authorize(db, context, scope=scope)
        allowed = authorized_scopes(
            service,
            db,
            scope,
            request.get(
                "include_associated", request.get("bundle", {}).get("include_associated", False)
            ),
        )
        permitted = {canonical(item[0]) for item in allowed}

        def add(target, resolved=None, profile=False):
            same_room = (
                scope["audience"] == target["audience"] == "group"
                and target["actor_id"] == scope["actor_id"]
                and target["conversation_id"] == scope["conversation_id"]
            )
            require(canonical(target) in permitted or same_room, "scope_changed", 409)
            result.append(
                dict(
                    scope=target,
                    context=resolved,
                    profile=profile,
                    group_read=not profile and group_read(service, target),
                )
            )

        add(scope, context)
        for other, _, _ in allowed[1:]:
            add(other)
        for check, resolved in legacy:
            payload = check["request"]
            target = payload.get("requested_scope", payload.get("requester_scope"))
            add(target, resolved, profile=check["kind"] == "profile")
            if check["kind"] != "profile":
                for other, _, _ in authorized_scopes(
                    service, db, target, payload["include_associated"]
                )[1:]:
                    add(other)
    return result


@contextmanager
def operation(service, items):
    if service.synchronized:
        with service.source_authority.bundle_operation(service, items) as db:
            yield db
    else:
        with service.store.transaction() as db:
            yield db


def verify_legacy(application, checks, contexts):
    from .. import profiles

    result = []
    for check, context in zip(checks, contexts, strict=True):
        request, expected = check["request"], check["expected"]
        if check["kind"] == "profile":
            actual = profiles.select(application.service, request, context)
        else:
            actual = getattr(application, "query" if check["kind"] == "select" else check["kind"])(
                request, context
            )
        for key, value in expected.items():
            if value is not None:
                require(actual[key] == value, "scope_changed", 409)
        require(
            parse_time(actual["valid_until"]) > application.service.clock(), "scope_changed", 409
        )
        result.append(dict(check_id=check["check_id"], kind=check["kind"], status="valid"))
    return result
