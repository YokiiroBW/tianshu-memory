"""One fresh authority boundary for a bounded preparation check or profile catalog."""

from .. import profiles
from ..domain import (
    DependencyChanged,
    OriginDenied,
    OriginUnavailable,
    SelectionChanged,
    canonical,
    parse_time,
    require,
    require_dependency,
    require_selection,
    utc,
)
from . import read_v2
from .read_bundle import authorize_check, operation, requirements, verify_component


def catalog(request):
    identities = [check["check_id"] for check in request["checks"]]
    required = request["required_check_ids"]
    require(
        len(set(identities)) == len(identities)
        and len(set(required)) == len(required)
        and set(required) <= set(identities),
        "invalid_input",
        400,
    )
    return set(required)


def envelope(request):
    return dict(
        schema_version=2,
        version_domain="memory-context/v2",
        request_id=request["query"]["request_id"],
        effective_scope=request["requested_scope"],
    )


def invalid(request, error, check_id=None):
    return dict(
        envelope(request),
        state="invalid",
        component="current_selection" if check_id is None else "required_check",
        check_id=check_id,
        failure=error.wire(request["query"]["request_id"]),
    )


def domains(items):
    return {
        (canonical(item["scope"]), bool(item.get("profile")), bool(item.get("group_read")))
        for item in items
    }


def component_key(check):
    request = dict(check["request"])
    request["query"] = {
        key: value for key, value in request["query"].items() if key != "request_id"
    }
    return canonical(dict(kind=check["kind"], request=request, expected=check["expected"]))


def execute(application, request, context, contexts):
    required = catalog(request)
    service, rejected = application.service, {}
    pairs = list(zip(request["checks"], contexts, strict=True))
    items = requirements(service, request, context, pairs, rejected=rejected)
    planned = domains(items)
    with operation(service, items) as db:
        try:
            require_selection(request["bundle"]["effective_scope"] == request["requested_scope"])
            frame = read_v2.collect(service, db, request["bundle"], context)
            # Authenticate semantics and compare the current selection exactly once.
            fresh = read_v2.verify_snapshot(service, db, request["bundle"], frame, context)
        except SelectionChanged as error:
            return invalid(request, error)
        # Re-evaluate the catalog against this one final transaction. A changed
        # association cannot add an unobserved domain after the union barrier.
        requirements(service, request, context, pairs, rejected=rejected, db=db, covered=planned)
        outcomes, cache = [], {}
        for check, resolved in pairs:
            identity = check["check_id"]
            error = rejected.get(identity)
            actual = None
            if error is None:
                try:
                    key = component_key(check)
                    if key not in cache:
                        try:
                            cache[key] = verify_component(application, check, resolved, db=db)
                        except (OriginDenied, DependencyChanged) as failure:
                            cache[key] = failure
                    actual = cache[key]
                    if isinstance(actual, (OriginDenied, DependencyChanged)):
                        raise actual
                except (OriginDenied, DependencyChanged) as failure:
                    error = failure
            outcomes.append((check, resolved, actual, error))

        # Do not borrow a lease renewed elsewhere or publish an already-expired
        # earlier component proof after finishing a long catalog.
        service._authorize(db, context, scope=request["requested_scope"])
        now = service.clock()
        deadline = parse_time(fresh["valid_until"])
        require(deadline > now, "timeout", 408)
        results = []
        for check, resolved, actual, error in outcomes:
            if error is None:
                try:
                    authorize_check(
                        service,
                        db,
                        resolved,
                        check["request"].get(
                            "requested_scope", check["request"].get("requester_scope")
                        ),
                    )
                    require_dependency(parse_time(actual["valid_until"]) > now)
                except (OriginDenied, DependencyChanged) as failure:
                    error = failure
            if error is not None:
                if isinstance(error, OriginUnavailable):
                    # Necessary origins were excluded from optional degradation
                    # at the HTTP boundary; keep this invariant for direct calls.
                    if check["check_id"] in required:
                        raise error
                    results.append(
                        dict(
                            check_id=check["check_id"],
                            kind=check["kind"],
                            status="unavailable",
                            reason="origin_unavailable",
                        )
                    )
                    continue
                if check["check_id"] in required:
                    return invalid(request, error, check["check_id"])
                results.append(
                    dict(
                        check_id=check["check_id"],
                        kind=check["kind"],
                        status="rejected",
                        reason="origin_denied"
                        if isinstance(error, OriginDenied)
                        else "dependency_changed",
                    )
                )
            else:
                deadline = min(
                    deadline, parse_time(actual["valid_until"]), parse_time(resolved["expires_at"])
                )
                results.append(dict(check_id=check["check_id"], kind=check["kind"], status="valid"))
        return dict(
            envelope(request),
            state="valid",
            bundle=fresh,
            checks=results,
            checked_at=utc(now),
            valid_until=utc(deadline),
        )


def profile_batch(application, request, context):
    service = application.service
    scope, header = request["requested_scope"], request["query"]
    require(
        all(
            item["requester_scope"] == scope and item["query"]["origin"] == header["origin"]
            for item in request["requests"]
        ),
        "invalid_input",
        400,
    )
    items = [dict(scope=scope, context=context, profile=True, group_read=False)]
    with operation(service, items) as db:
        responses = [profiles.select(service, item, context) for item in request["requests"]]
        service._authorize(db, context, scope=scope)
        now = service.clock()
        deadline = min(parse_time(item["valid_until"]) for item in responses)
        require(deadline > now, "timeout", 408)
        return dict(
            envelope(request),
            state="valid",
            responses=responses,
            checked_at=utc(now),
            valid_until=utc(deadline),
        )
