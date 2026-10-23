"""Authenticated component verification within one complete source boundary."""

import hashlib
import hmac
from contextlib import contextmanager, nullcontext

from ..domain import (
    DependencyChanged,
    Fault,
    OriginDenied,
    OriginUnavailable,
    canonical,
    parse_time,
    require,
    require_dependency,
    require_selection,
)
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
    require_selection(hmac.compare_digest(bundle["bundle_id"], selection_proof(db, bundle)))


def authorize_check(service, db, context, scope):
    """Classify only this local scope decision, after HTTP origin integrity checks."""
    try:
        service._authorize(db, context, scope=scope)
    except Fault as error:
        if error.code == "forbidden" and error.status == 403:
            raise OriginDenied() from error
        raise


def requirements(service, request, context, legacy=(), *, rejected=None, db=None, covered=None):
    scope = request["requested_scope"]
    result = []
    with service.store.transaction() if db is None else nullcontext(db) as db:
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

        def add(target, resolved=None, profile=False, *, into=result):
            same_room = (
                scope["audience"] == target["audience"] == "group"
                and target["actor_id"] == scope["actor_id"]
                and target["conversation_id"] == scope["conversation_id"]
            )
            require_dependency(canonical(target) in permitted or same_room)
            into.append(
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
            pending = []
            try:
                if isinstance(resolved, (OriginDenied, OriginUnavailable)):
                    raise resolved
                payload = check["request"]
                target = payload.get("requested_scope", payload.get("requester_scope"))
                authorize_check(service, db, resolved, target)
                # A preparation rechecks each origin within its final transaction.
                # Its source coverage remains complete even if an optional lease
                # expires while waiting for the shared operation lock.
                add(
                    target,
                    resolved if rejected is None else None,
                    profile=check["kind"] == "profile",
                    into=pending,
                )
                if check["kind"] != "profile":
                    for other, _, _ in authorized_scopes(
                        service, db, target, payload["include_associated"]
                    )[1:]:
                        add(other, into=pending)
                if covered is not None:
                    require_dependency(
                        all(
                            (canonical(item["scope"]), item["profile"], item["group_read"])
                            in covered
                            for item in pending
                        )
                    )
            except (OriginDenied, OriginUnavailable, DependencyChanged) as error:
                if rejected is None:
                    raise
                rejected[check["check_id"]] = error
            else:
                result.extend(pending)
    if rejected is not None:
        # All check origins were evaluated above; coverage for the same domain
        # is still needed only once. Preserve the primary context, inserted first.
        unique = {}
        for item in result:
            key = (canonical(item["scope"]), item["profile"], item["group_read"])
            unique.setdefault(key, item)
        return list(unique.values())
    return result


@contextmanager
def operation(service, items):
    if service.synchronized:
        with service.source_authority.bundle_operation(service, items) as db:
            yield db
    else:
        with service.store.transaction() as db:
            yield db


def verify_component(application, check, context, *, db=None):
    from .. import profiles

    request, expected = check["request"], check["expected"]
    scope = request.get("requested_scope", request.get("requester_scope"))
    if db is not None:
        authorize_check(application.service, db, context, scope)
    try:
        if check["kind"] == "profile":
            actual = profiles.select(application.service, request, context)
        else:
            actual = getattr(application, "query" if check["kind"] == "select" else check["kind"])(
                request, context
            )
    except Fault as error:
        if db is not None and error.code == "forbidden":
            # Only a now-failed local origin check can classify a late expiry.
            # An unrelated selector/protocol Fault must still escape unchanged.
            authorize_check(application.service, db, context, scope)
        raise
    for key, value in expected.items():
        if value is not None:
            require_dependency(actual[key] == value)
    require_dependency(parse_time(actual["valid_until"]) > application.service.clock())
    return actual


def verify_legacy(application, checks, contexts):
    result = []
    for check, context in zip(checks, contexts, strict=True):
        verify_component(application, check, context)
        result.append(dict(check_id=check["check_id"], kind=check["kind"], status="valid"))
    return result
