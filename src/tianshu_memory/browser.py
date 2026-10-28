"""Read-only, source-checked catalog for one server-configured account and scope."""

import base64
import hashlib
import hmac
import json
import math
from contextlib import contextmanager

from .domain import Fault, canonical, fingerprint, require, utc

MAX_PAGE = 50
MAX_SCAN = 200
MAX_OVERVIEW_SCAN = 1000
MAX_RESPONSE_BYTES = 262144
UNIT_FIELDS = (
    "record_id",
    "record_version",
    "statement",
    "conditions",
    "negations",
    "valid_time",
    "uncertainty",
    "reality",
)


def _required_string(value, maximum=128):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)
    return value


def validate_request(operation, body):
    require(isinstance(body, dict), "invalid_input", 400)
    common = {"schema_version", "request_id", "origin", "scope"}
    extra = set() if operation == "overview" else {"limit", "cursor"}
    if operation == "records":
        extra.add("subject")
    require(common <= set(body) <= common | extra, "invalid_input", 400)
    require(
        type(body["schema_version"]) is int and body["schema_version"] == 1,
        "unsupported_version",
        400,
    )
    _required_string(body["request_id"])
    require(
        isinstance(body["origin"], dict) and set(body["origin"]) == {"assertion_ref"},
        "invalid_input",
        400,
    )
    _required_string(body["origin"]["assertion_ref"], 512)
    scope = body["scope"]
    require(
        isinstance(scope, dict)
        and set(scope) == {"actor_id", "person_id", "audience", "conversation_id"},
        "invalid_input",
        400,
    )
    _required_string(scope["actor_id"])
    _required_string(scope["person_id"])
    _required_string(scope["conversation_id"])
    require(scope["audience"] in {"self_private", "group"}, "invalid_input", 400)
    if operation != "overview":
        limit = body.get("limit", 20)
        require(type(limit) is int and 1 <= limit <= MAX_PAGE, "invalid_input", 400)
        require(
            body.get("cursor") is None
            or (isinstance(body["cursor"], str) and len(body["cursor"]) <= 2048),
            "invalid_input",
            400,
        )
    if "subject" in body:
        subject = body["subject"]
        require(isinstance(subject, dict), "invalid_input", 400)
        if subject.get("kind") == "person":
            require(set(subject) == {"kind", "person_id"}, "invalid_input", 400)
            _required_string(subject["person_id"])
        elif subject.get("kind") == "group":
            require(set(subject) == {"kind", "conversation_id"}, "invalid_input", 400)
            _required_string(subject["conversation_id"])
        else:
            raise Fault("invalid_input", 400)


def _cursor_key(token):
    return hashlib.sha256(("memory-browser-cursor-v1:" + token).encode("utf-8")).digest()


def encode_cursor(data, token):
    payload = canonical(data).encode("utf-8")
    signature = hmac.new(_cursor_key(token), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def decode_cursor(value, token, expected):
    if value is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload, signature = raw[:-32], raw[-32:]
        require(
            hmac.compare_digest(
                hmac.new(_cursor_key(token), payload, hashlib.sha256).digest(), signature
            ),
            "invalid_input",
            400,
        )
        data = json.loads(payload)
        require(
            isinstance(data, dict) and set(data) == set(expected) | {"last"}, "invalid_input", 400
        )
        for key, item in expected.items():
            if key in {"revision", "version"} and data[key] != item:
                raise Fault("scope_changed", 409, item if key == "version" else None)
            require(data[key] == item, "invalid_input", 400)
        return data["last"]
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise Fault("invalid_input", 400) from None


def _registration(auth, caller_name, caller, context, scope):
    config = auth.config()
    reader = config.get("browser_readers", {}).get(caller_name)
    require(isinstance(reader, dict), "forbidden", 403)
    require(context["verified_account"] == reader.get("account"), "forbidden", 403)
    require(context["allowed_scope"] == scope, "forbidden", 403)
    templates = reader.get("scopes", [])
    static_actor = scope["actor_id"] == reader.get("actor_id")
    runtime_actor = (
        reader.get("allow_runtime_roles") is True
        and caller.get("allow_runtime_roles") is True
        and auth.role_grants is not None
        and auth.role_grants.decision(scope["actor_id"]) is True
    )
    static = static_actor and scope in templates
    runtime = runtime_actor and any(
        isinstance(template, dict)
        and template.get("actor_id") == reader.get("actor_id")
        and {**template, "actor_id": scope["actor_id"]} == scope
        for template in templates
    )
    runtime_group = (
        reader.get("allow_runtime_groups") is True
        and scope["audience"] == "group"
        and (static_actor or runtime_actor)
        and any(
            isinstance(template, dict)
            and template.get("actor_id") == reader.get("actor_id")
            and template.get("person_id") == scope["person_id"]
            for template in templates
        )
    )
    require(static or runtime or runtime_group, "forbidden", 403)


def _group(service, db, row, scope, subject=None, *, include_details=False):
    if not service._group_current(db, row["id"]):
        return None
    records = db.execute(
        "SELECT * FROM records WHERE group_id=? ORDER BY id", (row["id"],)
    ).fetchall()
    if not records or {r["id"] for r in records} != set(json.loads(row["members"])):
        return None
    units = [json.loads(r["payload"]) for r in records]
    if any(
        r["state"] != "active"
        or u.get("record_id") != r["id"]
        or u.get("record_version") != r["version"]
        or u.get("semantic_group_id") != row["id"]
        for r, u in zip(records, units, strict=True)
    ):
        return None
    if subject is None:
        if any(u.get("subject_person_id") != scope["person_id"] for u in units):
            return None
        if scope["audience"] == "group" and any(
            u.get("visibility") != "shared_projection"
            or not u.get("sources")
            or any(s.get("kind") != "shareable_projection" for s in u["sources"])
            for u in units
        ):
            return None
    else:
        from .profiles import audience_key

        expected_scope = dict(
            audience_key(scope["actor_id"], row["sharing"], scope["conversation_id"]),
            profile_subject=subject,
        )
        if json.loads(row["scope"]) != expected_scope:
            return None
        if any(
            u.get("subject") != subject
            or u.get("sharing") != row["sharing"]
            or u.get("category") != row["category"]
            or u.get("field_key") != row["field_key"]
            or u.get("visibility") != "shared_projection"
            or not u.get("sources")
            or any(s.get("kind") != "shareable_projection" for s in u["sources"])
            for u in units
        ):
            return None
        for unit in units:
            service.contracts.validate("profiles#unit", unit)
    if not service._projections_current(db, units):
        return None
    result = {
        "semantic_group_id": row["id"],
        "category": row["category"],
        "field_key": row["field_key"],
        "item_key": row["item_key"],
        "units": [{key: unit.get(key) for key in UNIT_FIELDS} for unit in units],
    }
    if subject is None and include_details:
        from .browser_details import provenance

        details = provenance(service, db, row["id"], scope)
        if details is not None:
            result["provenance"] = details
    return result


def _profile_rows(db, scope, subject, last, limit):
    sharing = "(p.sharing='public_preference' OR (p.sharing='group_only' AND p.conversation_id=?))"
    return db.execute(
        "SELECT g.*,p.sharing FROM profile_shares p JOIN groups g ON g.id=p.group_id "
        "WHERE p.actor_id=? AND p.subject_kind=? AND p.subject_id=? AND "
        + sharing
        + " AND g.state='active' AND g.id>? ORDER BY g.id LIMIT ?",
        (
            scope["actor_id"],
            subject["kind"],
            subject["person_id" if subject["kind"] == "person" else "conversation_id"],
            scope["conversation_id"] if scope["audience"] == "group" else "",
            last,
            limit,
        ),
    ).fetchall()


def _records(service, db, scope, subject, last, limit, *, timeline=False):
    from .browser_details import personal_rows

    if not timeline:
        last = last or ""
    items = []
    positions = []
    scanned = 0
    while scanned < MAX_SCAN and len(items) < limit + 1:
        batch = min(64, MAX_SCAN - scanned)
        if subject is None:
            rows = personal_rows(db, scope, last, batch, timeline=timeline)
        else:
            rows = _profile_rows(db, scope, subject, last, batch)
        if not rows:
            break
        for row in rows:
            scanned += 1
            item = _group(service, db, row, scope, subject, include_details=True)
            last = [row["browser_epoch"], row["id"]] if timeline else row["id"]
            if item is not None:
                items.append(item)
                positions.append(last)
            if len(items) >= limit + 1:
                break
        if len(rows) < batch:
            break
    # The extra valid item proves there is another page. Since it was scanned but is not returned,
    # resume just before it, rather than skipping it.
    more = len(items) > limit
    if more:
        items.pop()
        positions.pop()
        last = positions[-1]
    elif scanned >= MAX_SCAN:
        more = True
    return items, last if more else None


def _subjects(service, db, scope, last, limit):
    items = []
    scanned = 0
    previous = last or ["", ""]
    while scanned < MAX_SCAN and len(items) < limit + 1:
        rows = db.execute(
            "SELECT DISTINCT p.subject_kind,p.subject_id FROM profile_shares p JOIN groups g "
            "ON g.id=p.group_id WHERE p.actor_id=? AND g.state='active' "
            "AND (p.sharing='public_preference' OR (p.sharing='group_only' AND p.conversation_id=?)) "
            "AND (p.subject_kind,p.subject_id)>(?,?) ORDER BY p.subject_kind,p.subject_id LIMIT ?",
            (
                scope["actor_id"],
                scope["conversation_id"] if scope["audience"] == "group" else "",
                *previous,
                min(64, MAX_SCAN - scanned),
            ),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            scanned += 1
            previous = [row["subject_kind"], row["subject_id"]]
            if row["subject_kind"] not in {"person", "group"}:
                continue
            subject = {
                "kind": row["subject_kind"],
                "person_id" if row["subject_kind"] == "person" else "conversation_id": row[
                    "subject_id"
                ],
            }
            if (
                subject["kind"] == "group"
                and subject["conversation_id"] != scope["conversation_id"]
            ):
                continue
            group_rows = _profile_rows(db, scope, subject, "", MAX_SCAN + 1)
            truncated = len(group_rows) > MAX_SCAN
            valid = [_group(service, db, group, scope, subject) for group in group_rows[:MAX_SCAN]]
            valid = [group for group in valid if group is not None]
            require(valid or not truncated, "dependency_unavailable", 503)
            if valid:
                items.append(
                    {
                        "subject": subject,
                        "categories": sorted({v["category"] for v in valid}),
                        "group_count": len(valid),
                        "group_count_truncated": truncated,
                    }
                )
            if len(items) >= limit + 1:
                break
        if len(rows) < min(64, MAX_SCAN - scanned + len(rows)):
            break
    more = len(items) > limit
    if more:
        items.pop()
        subject = items[-1]["subject"]
        previous = [subject["kind"], subject.get("person_id", subject.get("conversation_id"))]
    elif scanned >= MAX_SCAN:
        more = True
    return items, previous if more else None


def authorize_current(auth, caller_name, caller, body):
    """The original browser service/account/scope registration, freshly resolved."""
    current_name, current_caller = auth.authenticate("Bearer " + caller["token"])
    require(
        current_name == caller_name and "browse" in current_caller.get("operations", []),
        "forbidden",
        403,
    )
    context = auth.resolve(
        current_name, current_caller, body["origin"]["assertion_ref"], body["request_id"]
    )
    _registration(auth, caller_name, current_caller, context, body["scope"])
    return context, current_caller


@contextmanager
def authorized_snapshot(service, auth, caller_name, caller, body):
    """Current browser authorization over a guarded local snapshot; no source assertion."""
    context, current_caller = authorize_current(auth, caller_name, caller, body)
    with service.store.transaction() as db:
        service._authorize(db, context, scope=body["scope"])
        yield db, current_caller


@contextmanager
def authorized_db(service, auth, caller_name, caller, body, profile, **requirements):
    """Join owner sync, fresh Platform identity and the guarded local snapshot."""
    scope = body["scope"]
    for _ in range(3):
        # The frozen v1 verifier accepts a Companion viewer only. Owner-only sync still
        # validates every physical/admission/grant and commits negatives independently.
        result, _ = service.source_authority.barrier(
            service, scope, context=None, profile=profile, **requirements
        )
        # Network I/O finishes before opening the SQLite transaction. A changed bearer or
        # withdrawn/reshaped origin fails here, after owner negatives have already committed.
        context, current_caller = authorize_current(auth, caller_name, caller, body)
        with service.store.transaction() as db:
            if service.source_authority.revision(db) != result["local_revision"]:
                continue
            service._authorize(db, context, scope=scope)
            yield db, current_caller
            return
    raise Fault("dependency_unavailable", 503)


def read(service, auth, caller_name, caller, body, context, operation):
    """Read only current catalog rows under the existing source and local revision guards."""
    validate_request(operation, body)
    scope = body["scope"]
    _registration(auth, caller_name, caller, context, scope)
    subject = body.get("subject")
    if subject is not None:
        require(service.contracts.profile_version is not None, "dependency_unavailable", 503)
        if subject["kind"] == "group":
            require(
                scope["audience"] == "group"
                and subject["conversation_id"] == scope["conversation_id"],
                "forbidden",
                403,
            )
    require(service.synchronized, "dependency_unavailable", 503)
    with authorized_db(
        service,
        auth,
        caller_name,
        caller,
        body,
        profile=operation == "subjects" or subject is not None,
    ) as (db, current_caller):
        if operation in {"subjects"} or subject is not None:
            service.store.require_profiles(db)
        version = service._scope_version(db, scope)
        revision = service.source_authority.revision(db) if service.synchronized else version
        stamp = {
            "schema_version": 1,
            "request_id": body["request_id"],
            "scope": scope,
            "scope_version": version,
            "verified_at": utc(service.clock()),
        }
        if operation == "overview":
            from .browser_details import personal_rows, review, timeline_ready

            timeline = timeline_ready(db)
            rows = personal_rows(db, scope, None, MAX_OVERVIEW_SCAN + 1, timeline=timeline)

            def visible():
                for row in rows[:MAX_OVERVIEW_SCAN]:
                    item = _group(service, db, row, scope)
                    if item is not None:
                        yield row, item

            truncated = len(rows) > MAX_OVERVIEW_SCAN
            if timeline:
                valid_count, reading = review(service, visible(), truncated=truncated)
            else:
                valid_count, reading = sum(1 for _ in visible()), None
            stamp.update(
                memory_group_count=valid_count,
                counts_truncated=truncated,
                review=reading,
            )
            from .browser_details import learning_progress

            stamp["learning"] = learning_progress(db, scope)
            return stamp
        expected = {
            "operation": operation,
            "scope": fingerprint(scope),
            "subject": fingerprint(subject),
            "revision": revision,
            "version": version,
        }
        from .browser_details import record_position, timeline_ready

        timeline = operation == "records" and subject is None and timeline_ready(db)
        if timeline:
            expected["order"] = "observed_desc"
        last = decode_cursor(body.get("cursor"), current_caller["token"], expected)
        limit = body.get("limit", 20)
        if operation == "records":
            if timeline:
                require(
                    last is None
                    or (
                        isinstance(last, list)
                        and len(last) == 2
                        and type(last[0]) in (int, float)
                        and math.isfinite(last[0])
                        and isinstance(last[1], str)
                    ),
                    "invalid_input",
                    400,
                )
            else:
                require(last is None or isinstance(last, str), "invalid_input", 400)
            items, next_last = _records(service, db, scope, subject, last, limit, timeline=timeline)
        else:
            require(
                last is None
                or (
                    isinstance(last, list)
                    and len(last) == 2
                    and all(isinstance(v, str) for v in last)
                ),
                "invalid_input",
                400,
            )
            items, next_last = _subjects(service, db, scope, last, limit)
        stamp.update(
            items=items,
            next_cursor=encode_cursor(dict(expected, last=next_last), current_caller["token"])
            if next_last is not None
            else None,
        )
        while len(canonical(stamp).encode("utf-8")) > MAX_RESPONSE_BYTES:
            require(len(stamp["items"]) > 1, "response_too_large", 413)
            stamp["items"].pop()
            retained = stamp["items"][-1]
            if operation == "records":
                last_retained = (
                    record_position(retained) if timeline else retained["semantic_group_id"]
                )
            else:
                target = retained["subject"]
                last_retained = [
                    target["kind"],
                    target.get("person_id", target.get("conversation_id")),
                ]
            stamp["next_cursor"] = encode_cursor(
                dict(expected, last=last_retained), current_caller["token"]
            )
        return stamp
