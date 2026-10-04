"""Read original message times transiently through existing authenticated source ports."""

import json

from ..domain import canonical, fingerprint, new_id, parse_time, require, utc

SOURCE_LIMIT = 256


def prepare(db, candidates):
    groups, selectors = {}, {}
    for group, _, _ in candidates:
        rows = db.execute(
            "SELECT l.source_key AS key,a.selector,a.payload,a.physical_key,p.payload AS physical FROM lineage l "
            "LEFT JOIN source_admissions a ON a.key=l.source_key "
            "LEFT JOIN physical_sources p ON p.key=a.physical_key WHERE l.group_id=?",
            (group["id"],),
        ).fetchall()
        groups[group["id"]] = [row["key"] for row in rows] or [None]
        for row in rows:
            if row["selector"] is not None and row["payload"] is not None:
                row = dict(row)
                access = db.execute(
                    "SELECT payload FROM source_observations WHERE kind='access' AND key=?",
                    (row["key"],),
                ).fetchone()
                row["access"] = json.loads(access[0]) if access else None
                selectors[row["key"]] = (row, json.loads(group["scope"]))
    heads = {
        row["owner"]: {"generation": row["generation"], "sequence": row["sequence"]}
        for row in db.execute("SELECT * FROM owner_heads")
    }
    revision = int(
        db.execute("SELECT value FROM metadata WHERE key='source_revision'").fetchone()[0]
    )
    return dict(groups=groups, selectors=selectors, heads=heads, revision=revision)


def filter_candidates(service, prepared, candidates, scope, context, period):
    groups, selectors, heads = (prepared[key] for key in ("groups", "selectors", "heads"))
    times = {}
    if service.synchronized:
        authority = service.source_authority
        by_scope = {}
        for key in sorted(selectors)[:SOURCE_LIMIT]:
            row, selected_scope = selectors[key]
            by_scope.setdefault(canonical(selected_scope), []).append(row)
        for encoded_scope, rows in by_scope.items():
            selected_scope = json.loads(encoded_scope)
            request = dict(
                schema_version=1,
                request_id=new_id("source-times"),
                mode="snapshot",
                selectors=[json.loads(row["selector"]) for row in rows],
                turn_ids=[],
                include_content=True,
            )
            snapshot = authority.transport.facts(request)
            authority._relations("source_snapshot", request, snapshot)
            viewer = (
                {"origin": {"assertion_ref": context["assertion_ref"]}, "scope": scope}
                if selected_scope == scope
                else None
            )
            access_request, access = authority._read_access(snapshot, viewer)
            final_request = dict(
                request,
                request_id=new_id("time-head"),
                mode="head",
                selectors=[],
                include_content=False,
            )
            final = authority.transport.facts(final_request)
            authority._relations("source_snapshot", final_request, final)
            authority._relations(
                "sync_barrier",
                dict(
                    request=request,
                    snapshot=snapshot,
                    access_request=access_request,
                    access=access,
                    final_head=final["head"],
                    now=utc(service.clock()),
                ),
                heads,
            )
            require(
                snapshot["head"] == heads["core"] and access["head"] == heads["platform"],
                "dependency_unavailable",
                503,
            )
            if viewer is not None:
                require(
                    access["viewer_context"]["verified_account"] == context["verified_account"]
                    and access["viewer_context"]["verified_channel"] == context["verified_channel"],
                    "dependency_unavailable",
                    503,
                )
            physicals = {fingerprint(p["key"]): p for p in snapshot["physicals"]}
            admissions = {fingerprint(a["selector"]): a for a in snapshot["admissions"]}
            grants = {fingerprint(g["selector"]): g for g in access["grants"]}
            for row in rows:
                physical = physicals[row["physical_key"]]
                metadata = dict(physical, content=None)
                require(
                    metadata == json.loads(row["physical"])
                    and admissions[row["key"]] == json.loads(row["payload"]),
                    "dependency_unavailable",
                    503,
                )
                require(
                    row["access"] == grants[row["key"]],
                    "dependency_unavailable",
                    503,
                )
                times[row["key"]] = parse_time(physical["content"]["sent_at"])
    start, end = parse_time(period["from"]), parse_time(period["to"])
    missing = {key for keys in groups.values() for key in keys if key not in times}
    retained = [
        candidate
        for candidate in candidates
        if any(key not in times or start <= times[key] < end for key in groups[candidate[0]["id"]])
    ]
    return retained, len(missing)
