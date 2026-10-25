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
from .authorization import authorized_scopes
from .scopes import group_read


def contextual(bundle):
    return bundle.get("retrieval_mode", "factual") == "context"


def selection_proof(db, bundle):
    """Authenticate server-selected semantics; freshness still requires the full source barrier."""
    key = db.execute("SELECT value FROM metadata WHERE key='memory_selection_proof_key'").fetchone()
    require(key is not None and len(key[0]) == 64, "dependency_unavailable", 503)
    # These fields are freshly recomputed by every verify. Every other field,
    # including future schema additions, belongs to the immutable selection.
    fresh = {"bundle_id", "verified_at", "valid_until", "owner_heads", "source_revision"}
    if contextual(bundle):
        # A positive selection retains its material identities, while current
        # authorization and history observations are rebuilt at every boundary.
        fresh.update(
            {
                "scope_version",
                "association_version",
                "scope_checks",
                "history_snapshot",
                "input_history_snapshot",
            }
        )
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
        selected = None
        bundle = request.get("bundle")
        if service.synchronized and bundle is not None and contextual(bundle):
            require_selection(bundle["effective_scope"] == scope)
            verify_selection_proof(db, bundle)
            selected = dict(
                scope=scope,
                group_ids=[
                    item["group"]["semantic_group_id"] for item in bundle["group_dependencies"]
                ],
                episode_ids=[item["episode_id"] for item in bundle["episode_dependencies"]],
                source_keys=[item["source_key"] for item in bundle["source_dependencies"]],
            )
        allowed = authorized_scopes(
            service,
            db,
            scope,
            request.get(
                "include_associated", request.get("bundle", {}).get("include_associated", False)
            ),
        )
        permitted = {canonical(item[0]) for item in allowed}

        def add(target, resolved=None, profile=False, *, into=result, selection=None):
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
            if selection is not None:
                into[-1]["selected"] = selection
                into[-1]["selected_only"] = True
            if selection is None and not profile and ("queries" in request or "bundle" in request):
                bundle = request.get("bundle")
                if bundle is not None:
                    into[-1]["dependency_keys"] = [
                        item["source_key"] for item in bundle["source_dependencies"]
                    ]
                else:
                    from .. import memory_documents

                    candidates = []
                    if memory_documents.installed(db):
                        owners = [target]
                        if same_room:
                            import json

                            owners = [
                                json.loads(row[0])
                                for row in db.execute(
                                    "SELECT DISTINCT scope FROM sources WHERE state='active' AND json_extract(scope,'$.actor_id')=? AND json_extract(scope,'$.audience')='group' AND json_extract(scope,'$.conversation_id')=?",
                                    (target["actor_id"], target["conversation_id"]),
                                )
                            ]
                        candidates = list(memory_documents.episode_metadata(service, db, owners))
                    into[-1]["dependency_keys"] = list(
                        dict.fromkeys(
                            row["source_key"]
                            for query in request.get("queries", [])
                            for row in memory_documents.eligible_episodes(
                                service, db, candidates, query
                            )
                        )
                    )

        add(scope, context, selection=selected)
        for other, _, _ in allowed[1:]:
            add(
                other,
                selection=(
                    dict(scope=other, group_ids=[], episode_ids=[], source_keys=[])
                    if selected is not None
                    else None
                ),
            )
        for check, resolved in legacy:
            raw = check["kind"] == "input_sources"
            if raw:
                require(service.synchronized, "dependency_unavailable", 503)
            pending = []
            try:
                if isinstance(resolved, (OriginDenied, OriginUnavailable)):
                    raise resolved
                payload = check["request"]
                target = payload.get("requested_scope", payload.get("requester_scope"))
                if raw:
                    require(target == payload["source_check"]["scope"], "invalid_input", 400)
                authorize_check(service, db, resolved, target)
                # A preparation rechecks each origin within its final transaction.
                # Its source coverage remains complete even if an optional lease
                # expires while waiting for the shared operation lock.
                add(
                    target,
                    resolved if rejected is None else None,
                    profile=check["kind"] == "profile",
                    into=pending,
                    selection=(
                        dict(scope=target, group_ids=[], episode_ids=[], source_keys=[])
                        if raw and selected is not None
                        else None
                    ),
                )
                if raw:
                    pending[-1]["source_checks"] = [payload["source_check"]]
                elif check["kind"] != "profile":
                    for other, _, _ in authorized_scopes(
                        service, db, target, payload["include_associated"]
                    )[1:]:
                        add(other, into=pending)
                if covered is not None:
                    require_dependency(
                        all(
                            (
                                (
                                    canonical(item["scope"]),
                                    item["profile"],
                                    item["group_read"],
                                    "full",
                                )
                                in covered
                                or (
                                    item.get("selected_only")
                                    and (
                                        canonical(item["scope"]),
                                        item["profile"],
                                        item["group_read"],
                                        "selected",
                                    )
                                    in covered
                                )
                            )
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
            if key not in unique:
                unique[key] = item
            else:
                if not item.get("selected_only") or not unique[key].get("selected_only"):
                    unique[key].pop("selected_only", None)
                if "selected" in item:
                    if "selected" not in unique[key]:
                        unique[key]["selected"] = item["selected"]
                    else:
                        for field in ("group_ids", "episode_ids", "source_keys"):
                            unique[key]["selected"][field] = sorted(
                                set(unique[key]["selected"][field]) | set(item["selected"][field])
                            )
            if item.get("source_checks") and unique[key] is not item:
                unique[key].setdefault("source_checks", []).extend(item["source_checks"])
            if item.get("dependency_keys") and unique[key] is not item:
                unique[key].setdefault("dependency_keys", []).extend(item["dependency_keys"])
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
        if check["kind"] == "input_sources":
            require(
                application.service.synchronized and db is not None, "dependency_unavailable", 503
            )
            require(scope == request["source_check"]["scope"], "invalid_input", 400)
            try:
                application.service._source_rows(db, request["source_check"]["sources"], scope)
            except Fault as error:
                # Only this existing local source decision is a known negative.
                # Owner relation, transport, authorization and CAS errors escape.
                if error.code == "version_conflict" and error.status == 409:
                    raise DependencyChanged() from error
                raise
            actual = dict(valid_until=context["expires_at"])
        elif check["kind"] == "profile":
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


def verify_legacy(application, checks, contexts, *, db=None):
    result = []
    for check, context in zip(checks, contexts, strict=True):
        verify_component(
            application, check, context, db=db if check["kind"] == "input_sources" else None
        )
        result.append(dict(check_id=check["check_id"], kind=check["kind"], status="valid"))
    return result
