"""Owner views and exact revision edits over the existing actor authority."""

import base64
import hmac
import json

from ..domain import Fault, canonical, fingerprint, require, utc
from . import lifecycle, search
from .facts import INTERNAL, permitted, source_refs
from .timeline import encode, signature


def _ref(kind, row):
    return dict(kind=kind, id=row["id"], revision=row["revision"])


def _payload(ref, row):
    return json.loads(row["fact"] if ref["kind"] == "episode" else row["payload"])


def _readable(basis, observed, context):
    if not basis:
        return False
    for expected, row in basis:
        if (
            row is None
            or row["state"] != "active"
            or row["revision"] != expected["expected_revision"]
        ):
            return False
        fact = json.loads(row["fact"])
        if not permitted(fact, context):
            return False
        for source in source_refs(fact):
            proof = observed[canonical(source)]
            if not (proof["state"] == "current" and proof["actor_known"] and proof["read_allowed"]):
                return False
            if context["audience"]["kind"] != "actor_only" and not proof["disclosure_allowed"]:
                return False
    return True


def _bases(mem, db, actor_id, rows):
    # Historical derived payloads still need the exact original episode basis.
    readable = [(ref, {**row, "state": "active"}) for ref, row in rows if _payload(ref, row)]
    return mem._basis(db, actor_id, readable)


def _bound(request):
    return {key: request[key] for key in ("actor_id", "role_version", "context", "states", "kinds")}


def _after(db, request, version):
    if request["cursor"] is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(request["cursor"].encode("ascii")))
        signed = value.pop("signature")
        require(hmac.compare_digest(signature(db, value), signed), "invalid_input", 400)
        require(value["binding"] == fingerprint(_bound(request)), "invalid_input", 400)
        require(value["domain_version"] == version, "source_changed", 409)
        after = value["after"]
        require(
            isinstance(after, list) and len(after) == 2 and all(isinstance(v, str) for v in after),
            "invalid_input",
            400,
        )
        return after
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Fault("invalid_input", 400) from None


def _list_rows(db, request, version):
    after = _after(db, request, version)
    rows = []
    for kind, table in (("episode", "actor_episodes"), ("derived", "actor_derived")):
        parameters = [request["actor_id"], *request["states"]]
        predicate = ""
        if after:
            predicate = " AND (recorded_at<? OR (recorded_at=? AND id<?))"
            parameters += [after[0], after[0], after[1]]
        for row in db.execute(
            "SELECT * FROM "
            + table
            + " WHERE actor_id=? AND state IN ("
            + ",".join("?" for _ in request["states"])
            + ")"
            + predicate
            + " ORDER BY recorded_at DESC,id DESC",
            parameters,
        ):
            row = dict(row)
            ref = _ref(kind, row)
            payload = _payload(ref, row)
            item_kind = "episode" if kind == "episode" else payload.get("kind")
            if item_kind is not None and item_kind not in request["kinds"]:
                continue
            rows.append((ref, row))
            if len([part for part in rows if part[0]["kind"] == kind]) > request["limit"]:
                break
    return sorted(rows, key=lambda part: (part[1]["recorded_at"], part[1]["id"]), reverse=True)


def _managed(mem, ref, row, basis):
    payload = _payload(ref, row)
    forgotten = not payload
    result = dict(
        ref=ref,
        kind=None,
        state=row["state"],
        text=None,
        episode_refs=[],
        source_refs=[],
        recorded_at=row["recorded_at"],
        expires_at=row.get("expires_at"),
        importance=None,
        retention=None,
        interest=None,
        occurred_at=None,
        editable=False,
        forgotten=forgotten,
    )
    if forgotten:
        return result
    result.update(mem._item(ref, row, basis))
    if ref["kind"] == "episode":
        result["occurred_at"] = payload.get("occurred_at")
    else:
        result.update(
            importance=payload["importance"],
            retention=payload["retention"],
            interest=payload.get("interest"),
            editable=row["state"] == "active",
        )
    return result


def _impact(mem, db, request):
    ref, row = mem._rows(db, request["actor_id"], [request["ref"]])[0]
    require(row is not None, "not_found", 404)
    require(row["revision"] == ref["revision"], "version_conflict", 409)
    dependencies = [
        (_ref("derived", part), part) for part in lifecycle.dependents(db, request["actor_id"], ref)
    ]
    return (ref, row), dependencies


def _interests(mem, db, request):
    allowed, parameters = search.basis_predicate(db, request["context"])
    kind = "payload::jsonb->>'kind'" if hasattr(db, "native") else "json_extract(payload,'$.kind')"
    topic = (
        "payload::jsonb->'interest'->>'topic'"
        if hasattr(db, "native")
        else "json_extract(payload,'$.interest.topic')"
    )
    # Select current representatives before their histories, so many repeated
    # formulations of one topic cannot crowd out an older, distinct interest.
    rows = list(
        db.execute(
            "SELECT * FROM (SELECT m.*,ROW_NUMBER() OVER (PARTITION BY COALESCE(lower(trim("
            + topic
            + ")),id) ORDER BY CASE WHEN state='active' THEN 0 ELSE 1 END,recorded_at DESC,id DESC) AS topic_rank FROM actor_derived m WHERE actor_id=? AND state IN ('active','superseded') AND expires_at>? AND "
            + kind
            + "='preference' AND ("
            + allowed
            + ")) candidates ORDER BY topic_rank,recorded_at DESC,id DESC LIMIT 257",
            [request["actor_id"], utc(mem.app.clock()), *parameters],
        )
    )
    return [(_ref("derived", row), dict(row)) for row in rows[:256]], len(rows) > 256


def execute(mem, request, *, actor, caller, recheck):
    app, op = mem.app, request["operation"]
    if op != "interests":
        require(request["context"] == INTERNAL, "forbidden", 403)
    if op == "forget_memory":
        app._refresh(request, {}, [], INTERNAL, actor, recheck, caller)
        with app.store.transaction(write=True) as db:
            require(
                app.store.version(db) == request["expected_domain_version"] + 1,
                "version_conflict",
                409,
            )
            ref = lifecycle.forget(db, request["actor_id"], request["ref"])
            return mem._receipt(db, request, caller, ref, "withdrawn")
    if op == "revise_memory":
        with app.store.transaction() as db:
            ref, row = mem._rows(db, request["actor_id"], [request["ref"]])[0]
            require(
                ref["kind"] == "derived"
                and row is not None
                and row["state"] == "active"
                and row["revision"] == ref["revision"],
                "version_conflict",
                409,
            )
            payload = json.loads(row["payload"])
        replacement = {
            **request,
            "kind": payload["kind"],
            "episode_refs": payload["episode_refs"],
            "replaces": [ref],
        }
        if "interest" not in replacement and "interest" in payload:
            replacement["interest"] = payload["interest"]
        return mem._append(replacement, actor, caller, recheck, receipt_request=request)
    context = request["context"]
    context_before = app.authority.context(request, context)
    with app.store.transaction() as db:
        version = app.store.version(db)
        if op == "manage_list":
            rows = _list_rows(db, request, version)
            has_more = len(rows) > request["limit"]
            rows = rows[: request["limit"]]
        elif op == "interests":
            rows, interest_omitted = _interests(mem, db, request)
        else:
            target, dependencies = _impact(mem, db, request)
            rows = [target, *dependencies[:4096]]
        bases = _bases(mem, db, request["actor_id"], rows)
    sources = mem._refs(bases)
    observed = {}
    for offset in range(0, max(1, len(sources)), 4096):
        proved, version, _ = app._prove(
            request,
            sources[offset : offset + 4096],
            context,
            actor,
            caller,
            recheck,
            version,
            context_before,
        )
        observed.update(proved)
    with app.store.transaction() as db:
        require(app.store.version(db) == version, "source_changed", 409)
        result = dict(
            schema_version=1,
            request_id=request["request_id"],
            actor_id=request["actor_id"],
            role_version=request["role_version"],
            operation=op,
            context=context,
            domain_version=version,
            checked_at=utc(app.clock()),
        )
        # A negative source proof can have cleared bodies and advanced revisions.
        current = mem._rows(db, request["actor_id"], [ref for ref, _ in rows])
        rows = [(_ref(ref["kind"], row), row) for ref, row in current if row is not None]
        bases = _bases(mem, db, request["actor_id"], rows)
        if op == "memory_impact":
            target, dependencies = _impact(mem, db, {**request, "ref": rows[0][0]})
            refs = list({canonical(source): source for source in sources}.values())
            result.update(
                ref=target[0],
                dependent_refs=[ref for ref, _ in dependencies[:4096]],
                source_refs=refs[:4096],
                owner_originals_affected=False,
                can_revise=target[0]["kind"] == "derived" and target[1]["state"] == "active",
                can_forget=bool(_payload(*target)),
                omitted=len(dependencies) > 4096 or len(refs) > 4096,
            )
        elif op == "manage_list":
            items, used, after, omitted = [], 0, None, False
            for ref, row in rows:
                if len(items) == request["limit"]:
                    break
                basis = bases.get((ref["kind"], ref["id"]), [])
                if _payload(ref, row) and not _readable(basis, observed, context):
                    omitted = True
                    after = [row["recorded_at"], row["id"]]
                    continue
                item = _managed(mem, ref, row, basis)
                size = len(canonical(item).encode())
                if used + size > request["budget_bytes"]:
                    omitted = True
                    if not items:
                        after = [row["recorded_at"], row["id"]]
                    break
                items.append(item)
                used += size
                after = [row["recorded_at"], row["id"]]
            more = (
                bool(rows and after != [rows[-1][1]["recorded_at"], rows[-1][1]["id"]]) or has_more
            )
            result.update(
                items=items,
                omitted=omitted,
                next_cursor=encode(db, _bound(request), version, after) if more and after else None,
            )
        else:
            groups, omitted = {}, interest_omitted
            for ref, row in rows:
                payload = _payload(ref, row)
                basis = bases.get((ref["kind"], ref["id"]), [])
                if not payload or not _readable(basis, observed, context):
                    omitted = True
                    continue
                tag = payload.get("interest")
                key = tag["topic"].strip().casefold() if tag else ref["id"]
                groups.setdefault(key, []).append((ref, row, basis, payload))
            items, used = [], 0
            for group in groups.values():
                active = [part for part in group if part[1]["state"] == "active"]
                if not active:
                    continue
                ref, row, basis, payload = active[0]
                tag = payload.get("interest", {})
                supporting = set()
                for _, _, _, other in active:
                    if other.get("interest", {}).get("stance") == tag.get("stance"):
                        supporting.update(
                            lifecycle.evidence(db, request["actor_id"], other["episode_refs"])
                        )
                item = mem._item(ref, row, basis)
                history = [
                    dict(
                        ref=p[0],
                        stance=p[3].get("interest", {}).get("stance", "unspecified"),
                        state=p[1]["state"],
                        recorded_at=p[1]["recorded_at"],
                    )
                    for p in group
                ]
                item.update(
                    topic=tag.get("topic"),
                    stance=tag.get("stance", "unspecified"),
                    support_count=len(supporting),
                    history=history[:64],
                    history_omitted=len(history) > 64 or interest_omitted,
                )
                size = len(canonical(item).encode())
                if len(items) == request["limit"] or used + size > request["budget_bytes"]:
                    omitted = True
                    continue
                items.append(item)
                used += size
            result.update(items=items, omitted=omitted)
        app.contracts.validate(op + "_result", result)
        return result
