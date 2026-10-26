"""Bounded group topic projection over immutable completed segment receipts."""

import json
from datetime import UTC

from . import browser
from .domain import canonical, fingerprint, parse_time, require, utc
from .segment_migration import installed
from .segments import group_projection_current


def _stamp(value):
    return utc(parse_time(value).astimezone(UTC)).removesuffix("Z")


def _page(db, scope, body, last):
    clauses = [
        "j.extraction_schema=6",
        "j.state='committed'",
        "json_extract(s.frame,'$.actor_id')=?",
        "json_extract(s.frame,'$.audience')='group'",
        "json_extract(s.frame,'$.conversation_id')=?",
    ]
    parameters = [scope["actor_id"], scope["conversation_id"]]
    for key, comparison in (("start_at", ">="), ("end_at", "<")):
        if body[key] is not None:
            clauses.append("RTRIM(b.recorded_at,'Z')" + comparison + "?")
            parameters.append(_stamp(body[key]))
    if last is not None:
        require(
            isinstance(last, list)
            and len(last) == 2
            and all(isinstance(item, str) for item in last),
            "invalid_input",
            400,
        )
        clauses.append("(RTRIM(b.recorded_at,'Z')<? OR (RTRIM(b.recorded_at,'Z')=? AND j.id<?))")
        parameters.extend([last[0], last[0], last[1]])
    parameters.append(body["limit"] + 1)
    return [
        dict(row)
        for row in db.execute(
            "SELECT s.segment_id,j.id AS job_id,j.event,j.source_snapshot,b.id AS batch_id,b.window_id,"
            "b.manifest,b.recorded_at,l.result AS receipt FROM memory_segments s JOIN jobs j ON j.id=s.job_id "
            "JOIN memory_batches b ON b.job_id=j.id JOIN write_ledger l ON l.job_id=j.id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY RTRIM(b.recorded_at,'Z') DESC,j.id DESC LIMIT ?",
            parameters,
        )
    ]


def _requirements(rows):
    materials, keys, scopes, windows = set(), set(), {}, {}
    for row in rows:
        event = json.loads(row["event"])
        materials.update(event["material_ids"])
        keys.update(json.loads(row["source_snapshot"]))
        for target in event["targets"]:
            scopes[canonical(target["scope"])] = target["scope"]
        for window in event["context_windows"]:
            windows[window["manifest"]["window_id"]] = window["manifest"]
    return dict(
        material_ids=sorted(materials),
        dependency_keys=sorted(keys),
        summary_scopes=[(scope, False) for scope in scopes.values()],
        windows=list(windows.values()),
    )


def _project(service, db, row, scope):
    event, manifest, receipt = (json.loads(row[key]) for key in ("event", "manifest", "receipt"))
    service.contracts.validate("memory-extraction#segment_receipt", receipt)
    require(
        receipt["job_id"] == row["job_id"]
        and receipt["segment_id"] == row["segment_id"]
        and receipt["batch_ref"] == row["batch_id"]
        and manifest["window_id"] == row["window_id"],
        "dependency_unavailable",
        503,
    )
    if not group_projection_current(service, db, event, json.loads(row["source_snapshot"]), scope):
        return None
    members = {item["member_id"]: item for item in manifest["members"]}
    topics = []
    omitted = False
    for topic in receipt["topics"]:
        require(set(topic["member_ids"]) <= set(members), "dependency_unavailable", 503)
        refs, valid = [], True
        for result in receipt["results"]:
            if result["subject_person_id"] not in topic["person_ids"]:
                continue
            for ref in result["memory_refs"]:
                record = db.execute(
                    "SELECT r.*,g.scope,g.state AS group_state FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
                    (ref["record_id"],),
                ).fetchone()
                if (
                    record is None
                    or record["version"] != ref["record_version"]
                    or record["state"] != "active"
                    or record["group_state"] != "active"
                    or not service._group_current(db, record["group_id"])
                    or json.loads(record["scope"]) != result["scope"]
                    or result["scope"]["actor_id"] != scope["actor_id"]
                    or result["scope"]["audience"] != "group"
                    or result["scope"]["conversation_id"] != scope["conversation_id"]
                ):
                    valid = False
                    break
                refs.append(dict(subject_person_id=result["subject_person_id"], **ref))
        if not valid:
            omitted = True
            continue
        member_refs = []
        for identity in topic["member_ids"]:
            member = members[identity]
            member_refs.append(
                dict(
                    member_id=identity,
                    role=member["role"],
                    person_id=member["speaker"].get("person_id"),
                    actor_id=member["speaker"].get("actor_id"),
                    observed_at=member["observed_at"],
                    reality=member["reality"],
                )
            )
        topics.append(
            dict(
                topic_id=topic["topic_id"],
                summary=topic["summary"],
                person_ids=topic["person_ids"],
                member_refs=member_refs,
                memory_refs=refs,
            )
        )
    return dict(
        segment_id=row["segment_id"],
        window_id=row["window_id"],
        recorded_at=row["recorded_at"],
        topics=topics,
        coverage=dict(manifest["coverage"], result_person_count=len(receipt["results"])),
    ), omitted


def read(service, auth, caller_name, caller, body, context):
    service.contracts.validate("group-memory-projection#request", body)
    scope = body["scope"]
    require(
        scope["audience"] == "group"
        and body["subject"]["conversation_id"] == scope["conversation_id"],
        "forbidden",
        403,
    )
    if body["start_at"] is not None and body["end_at"] is not None:
        require(parse_time(body["start_at"]) < parse_time(body["end_at"]), "invalid_input", 400)
    require(service.synchronized, "dependency_unavailable", 503)
    if service.contracts.context_v2_version is None:
        service.contracts.load_memory_v2()
    with browser.authorized_snapshot(service, auth, caller_name, caller, body) as (
        db,
        current_caller,
    ):
        require(installed(db), "dependency_unavailable", 503)
        expected = dict(
            operation="topics",
            scope=fingerprint(scope),
            subject=fingerprint(body["subject"]),
            start_at=body["start_at"],
            end_at=body["end_at"],
            revision=service.source_authority.revision(db),
            version=service._scope_version(db, scope),
        )
        last = browser.decode_cursor(body["cursor"], current_caller["token"], expected)
        found = _page(db, scope, body, last)
        more = len(found) > body["limit"]
        page = found[: body["limit"]]
        selected, omitted = [], False
        for row in page:
            if group_projection_current(
                service, db, json.loads(row["event"]), json.loads(row["source_snapshot"]), scope
            ):
                selected.append(row)
            else:
                omitted = True
    with browser.authorized_db(
        service, auth, caller_name, caller, body, False, **_requirements(selected)
    ) as (db, current_caller):
        items = []
        for row in selected:
            current = db.execute(
                "SELECT j.event,j.source_snapshot,b.manifest,l.result AS receipt FROM jobs j JOIN memory_batches b ON b.job_id=j.id JOIN write_ledger l ON l.job_id=j.id WHERE j.id=?",
                (row["job_id"],),
            ).fetchone()
            require(
                current is not None
                and all(
                    current[key] == row[key]
                    for key in ("event", "source_snapshot", "manifest", "receipt")
                ),
                "scope_changed",
                409,
            )
            projected = _project(service, db, row, scope)
            if projected is None:
                omitted = True
            else:
                item, changed = projected
                items.append(item)
                omitted |= changed
        expected.update(
            revision=service.source_authority.revision(db),
            version=service._scope_version(db, scope),
        )
        next_last = [_stamp(page[-1]["recorded_at"]), page[-1]["job_id"]] if more and page else None
        result = dict(
            schema_version=1,
            request_id=body["request_id"],
            scope=scope,
            subject=body["subject"],
            scope_version=service._scope_version(db, scope),
            checked_at=utc(service.clock()),
            items=items,
            next_cursor=browser.encode_cursor(
                dict(expected, last=next_last), current_caller["token"]
            )
            if next_last
            else None,
            omitted_invalidated=omitted,
        )
        while len(canonical(result).encode("utf-8")) > browser.MAX_RESPONSE_BYTES:
            require(len(items) > 1, "response_too_large", 413)
            items.pop()
            retained = items[-1]
            anchor = next(row for row in page if row["segment_id"] == retained["segment_id"])
            result["next_cursor"] = browser.encode_cursor(
                dict(expected, last=[_stamp(anchor["recorded_at"]), anchor["job_id"]]),
                current_caller["token"],
            )
        service.contracts.validate("group-memory-projection#response", result)
        return result
