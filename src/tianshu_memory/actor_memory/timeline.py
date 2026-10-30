"""Occurrence-time episode pages, isolated from relevance-ranked query semantics."""

import base64
import hashlib
import hmac
import json
from datetime import UTC

from ..domain import Fault, canonical, fingerprint, parse_time, require, utc


def normalized(value):
    parsed = parse_time(value)
    require(parsed.tzinfo is not None, "invalid_input", 400)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def binding(request):
    window = {key: normalized(request["window"][key]) for key in ("start", "end")}
    require(window["start"] < window["end"], "invalid_input", 400)
    return dict(
        actor_id=request["actor_id"],
        role_version=request["role_version"],
        context=request["context"],
        kinds=request["kinds"],
        window=window,
    )


def signature(db, value):
    key = db.execute("SELECT value FROM actor_metadata WHERE key='recovery'").fetchone()[0]
    return hmac.new(key.encode(), canonical(value).encode(), hashlib.sha256).hexdigest()


def decode(db, request, version):
    bound = binding(request)
    cursor = request.get("cursor")
    if cursor is None:
        return bound, None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        require(isinstance(value, dict), "invalid_input", 400)
        signed = value.pop("signature")
        require(
            isinstance(signed, str) and hmac.compare_digest(signature(db, value), signed),
            "invalid_input",
            400,
        )
        require(value["binding"] == fingerprint(bound), "invalid_input", 400)
        require(value["domain_version"] == version, "source_changed", 409)
        after = value["after"]
        require(
            after is None
            or (
                isinstance(after, list)
                and len(after) == 2
                and normalized(after[0]) == after[0]
                and isinstance(after[1], str)
            ),
            "invalid_input",
            400,
        )
        return bound, after
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Fault("invalid_input", 400) from None


def encode(db, bound, version, after):
    value = dict(binding=fingerprint(bound), domain_version=version, after=after)
    return base64.urlsafe_b64encode(
        canonical(value | {"signature": signature(db, value)}).encode()
    ).decode()


def candidates(db, request, version):
    bound, after = decode(db, request, version)
    postgres = hasattr(db, "native")
    if postgres:
        # Python/SQLite/cursors truncate sub-microsecond digits; PostgreSQL's
        # timestamp parser rounds them, potentially across the window boundary.
        # Truncate the textual fraction before parsing so every key agrees.
        occurred = (
            r"regexp_replace(fact::jsonb->>'occurred_at', '(\.[0-9]{6})[0-9]+', '\1')"
            "::timestamptz"
        )
        parameter = "%s"
        execute = db.native
    else:
        # Fixed-width UTC retains all six microseconds; SQLite julianday does not.
        db.create_function(
            "actor_occurred_utc",
            1,
            lambda value: normalized(value) if value is not None else None,
            deterministic=True,
        )
        occurred = "actor_occurred_utc(json_extract(fact,'$.occurred_at'))"
        parameter = "?"
        execute = db.execute
    params = [request["actor_id"], bound["window"]["start"], bound["window"]["end"]]
    predicate = ""
    if after:
        predicate = f" AND ({occurred},id)>({parameter},{parameter})"
        params.extend(after)
    sql = (
        f"SELECT * FROM actor_episodes WHERE actor_id={parameter} AND state='active' "
        f"AND {occurred}>={parameter} AND {occurred}<{parameter}{predicate} "
        f"ORDER BY {occurred},id LIMIT {parameter}"
    )
    params.append(request["limit"] + 1)
    rows = [dict(row) for row in execute(sql, params)]
    undated = (
        execute(
            f"SELECT 1 FROM actor_episodes WHERE actor_id={parameter} AND state='active' "
            f"AND {occurred} IS NULL LIMIT 1",
            [request["actor_id"]],
        ).fetchone()
        is not None
    )
    return bound, after, rows[: request["limit"]], len(rows) > request["limit"], undated


def execute(memories, request, *, actor, caller, recheck):
    app, context = memories.app, request["context"]
    context_before = app.authority.context(request, context)
    with app.store.transaction() as db:
        version = app.store.version(db)
        bound, after, rows, more, undated = candidates(db, request, version)
        refs = [dict(kind="episode", id=row["id"], revision=row["revision"]) for row in rows]
        bases = memories._basis(db, request["actor_id"], list(zip(refs, rows)))
    observed, version, _ = app._prove(
        request,
        memories._refs(bases),
        context,
        actor,
        caller,
        recheck,
        version,
        context_before,
    )
    with app.store.transaction() as db:
        require(app.store.version(db) == version, "source_changed", 409)
        current = memories._rows(db, request["actor_id"], refs)
        bases = memories._basis(db, request["actor_id"], current)
        items, used, omitted = [], 2, undated  # JSON array brackets count too.
        for (ref, row), original in zip(current, rows):
            basis = bases.get((ref["kind"], ref["id"]), [])
            state, _ = memories._status(ref, row, basis, observed, context)
            position = [normalized(json.loads(original["fact"])["occurred_at"]), ref["id"]]
            if state != "current":
                omitted, after = True, position
                continue
            item = memories._item(ref, row, basis) | {"fact": json.loads(row["fact"])}
            item_size = len(canonical(item).encode())
            if item_size + 2 > 32768:
                # The contract cannot fit this item even with the maximum budget.
                # Advance explicitly as omitted instead of trapping every later page.
                omitted, after = True, position
                continue
            size = item_size + bool(items)  # Comma between JSON array entries.
            if used + size > request["budget_bytes"]:
                # This row remains at the next cursor. Pagination is incomplete,
                # but no material has been skipped; later pages can still close
                # the same day without erasing genuine source omissions.
                more = True
                break
            items.append(item)
            used += size
            after = position
        result = dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            role_version=request["role_version"],
            operation="query",
            mode="timeline",
            context=context,
            domain_version=version,
            checked_at=utc(app.clock()),
            window=request["window"],
            items=items,
            omitted=omitted,
            next_cursor=encode(db, bound, version, after) if more else None,
            complete=not more and not omitted,
        )
        app.contracts.validate("timeline_result", result)
        return result
