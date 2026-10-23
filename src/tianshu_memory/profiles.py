"""Memory-owned shared profile domain. No standalone service or unreviewed HTTP writer."""

import json
from collections import defaultdict
from datetime import timedelta

from .domain import DependencyChanged, Fault, canonical, parse_time, query_terms, require, utc
from .postgres import GROUP_READ_BATCH, is_postgres


def audience_key(actor_id, sharing, conversation_id=None):
    return {
        "profile_audience": sharing,
        "actor_id": actor_id,
        "conversation_id": conversation_id if sharing == "group_only" else None,
    }


def storage_scope(draft):
    return dict(
        audience_key(draft["source_scope"]["actor_id"], draft["sharing"], draft["conversation_id"]),
        profile_subject=draft["subject"],
    )


def validate_draft(service, db, draft):
    """Validate an exact explicit projection, including every semantic qualifier and source.

    This is not a consent detector. Production approval issuance remains unconfigured.
    """
    require(
        set(draft)
        == {
            "source_scope",
            "subject",
            "sharing",
            "conversation_id",
            "category",
            "field_key",
            "units",
        },
        "invalid_input",
        400,
    )
    source_scope, subject = draft["source_scope"], draft["subject"]
    service.contracts.validate("common#scope", source_scope)
    require(isinstance(subject, dict), "invalid_input", 400)
    kind = subject.get("kind")
    require(kind in {"person", "group"}, "invalid_input", 400)
    identifier = "person_id" if kind == "person" else "conversation_id"
    require(set(subject) == {"kind", identifier}, "invalid_input", 400)
    require(
        isinstance(subject[identifier], str) and bool(subject[identifier]), "invalid_input", 400
    )
    require(
        draft["category"] in ({"interest", "style"} if kind == "person" else {"topic", "style"}),
        "invalid_input",
        400,
    )
    require(isinstance(draft["field_key"], str) and bool(draft["field_key"]), "invalid_input", 400)
    if kind == "person":
        require(subject["person_id"] == source_scope["person_id"])
    else:
        require(source_scope["audience"] == "group")
        require(subject["conversation_id"] == source_scope["conversation_id"])
    if draft["sharing"] == "public_preference":
        require(kind == "person" and draft["category"] == "interest", "invalid_input", 400)
        require(draft["conversation_id"] is None, "invalid_input", 400)
        # Publication still needs a separate exact approval by the source's person. A
        # previous group_only approval cannot authorize public disclosure of the same source.
    else:
        require(draft["sharing"] == "group_only", "invalid_input", 400)
        require(
            isinstance(draft["conversation_id"], str) and bool(draft["conversation_id"]),
            "invalid_input",
            400,
        )
        if source_scope["audience"] == "group":
            require(draft["conversation_id"] == source_scope["conversation_id"])
        if kind == "group":
            require(draft["conversation_id"] == subject["conversation_id"])
    require(
        isinstance(draft["units"], list) and 0 < len(draft["units"]) <= service.max_units,
        "invalid_input",
        400,
    )
    sources = {}
    for unit in draft["units"]:
        require(
            set(unit)
            == {
                "statement",
                "conditions",
                "negations",
                "valid_time",
                "uncertainty",
                "reality",
                "sources",
            },
            "invalid_input",
            400,
        )
        # Reuse v1's semantic qualifiers for reviewed input, before replacing private sources.
        service.contracts.validate(
            "identity-memory#unit",
            dict(
                unit,
                record_id="validation-record",
                record_version=1,
                semantic_group_id="validation-group",
                subject_person_id=source_scope["person_id"],
                visibility="self_private",
                sources=[{"kind": "raw_message", "source": s} for s in unit["sources"]],
            ),
        )
        require(bool(unit["sources"]), "invalid_input", 400)
        for row in service._source_rows(db, unit["sources"], source_scope, reality=unit["reality"]):
            sources[row["key"]] = row
    return sources


def approval_authority(service, db, draft, context):
    binding = service._authorize(db, context)
    require(binding is not None and binding["person_id"] == draft["source_scope"]["person_id"])
    allowed = dict(context["allowed_scope"], person_id=binding["person_id"])
    service._authorize(db, context, scope=allowed)
    require(allowed["actor_id"] == draft["source_scope"]["actor_id"])
    if draft["sharing"] == "group_only":
        require(
            allowed["audience"] == "group"
            and allowed["conversation_id"] == draft["conversation_id"]
        )
    else:
        require(allowed == draft["source_scope"])


def snapshot(sources):
    return canonical({key: [row["revision"], row["epoch"]] for key, row in sources.items()})


def select(service, request, context):
    scope, target = request["requester_scope"], request["target"]
    with service.operation(scope=scope, context=context, profile=True) as db:
        service._authorize(db, context, scope=scope)
        service.store.require_profiles(db)
        kind = target["kind"]
        require(kind in {"person", "group"}, "invalid_input", 400)
        if kind == "group":
            require(
                scope["audience"] == "group"
                and target["conversation_id"] == scope["conversation_id"]
            )
        require(
            set(request["selection"])
            <= ({"interest", "style"} if kind == "person" else {"topic", "style"}),
            "invalid_input",
            400,
        )
        version = service._scope_version(db, audience_key(scope["actor_id"], "public_preference"))
        if scope["audience"] == "group":
            version += (
                service._scope_version(
                    db, audience_key(scope["actor_id"], "group_only", scope["conversation_id"])
                )
                - 1
            )
        if request["known_scope_version"] not in (None, version):
            raise DependencyChanged(version)
        if service.source_authority is None:
            raise Fault("dependency_unavailable", 503)
        verified = service.clock()
        response = {
            "schema_version": 1,
            "version_domain": "profile-memory/v1",
            "request_id": request["query"]["request_id"],
            "requester_scope": scope,
            "target": target,
            "scope_version": version,
            "verified_at": utc(verified),
            "valid_until": utc(
                min(verified + timedelta(seconds=30), parse_time(context["expires_at"]))
            ),
            "selected_units": [],
            "dependency_groups": [],
            "budget_used": {"tokens": 0, "bytes": 0},
            "omissions": [],
        }
        if not request["budget"]["tokens"] or not request["budget"]["bytes"]:
            response["omissions"] = ["budget"]
            return response
        exact_kind, separator, exact_value = request["query_text"].partition(":")
        exact = separator and exact_kind in {"field", "item"}
        search_terms = [] if exact else query_terms(request["query_text"])
        if not exact and not search_terms:
            response["omissions"] = ["no_match"]
            return response
        # Never enumerate the target's private records or even look it up in the people table.
        parameters = [
            scope["actor_id"],
            kind,
            target["person_id" if kind == "person" else "conversation_id"],
            scope["audience"],
            scope["conversation_id"],
            *request["selection"],
        ]
        placeholders = ",".join("?" for _ in request["selection"])
        exact_filter = f" AND g.{exact_kind}_key=?" if exact else ""
        if exact:
            parameters.append(exact_value)
        groups = db.execute(
            "SELECT g.*,p.sharing FROM profile_shares p JOIN groups g ON g.id=p.group_id "
            "WHERE p.actor_id=? AND p.subject_kind=? AND p.subject_id=? "
            "AND (p.sharing='public_preference' OR (p.sharing='group_only' AND ?='group' AND p.conversation_id=?)) "
            f"AND g.state='active' AND g.category IN ({placeholders}){exact_filter} ORDER BY g.id",
            parameters,
        ).fetchall()
        eligible = _eligible(service, db, groups, scope, target)
        if not exact:
            eligible = service._rank_groups(db, eligible, search_terms, request["query_text"])
        response.update(service._fit_groups(eligible, request["budget"]))
        return response


def _units(group, rows, target):
    """Profile integrity remains separate from ordinary person-memory units."""
    if set(json.loads(group["members"])) != {row["id"] for row in rows} or not rows:
        return None
    units = [json.loads(row["payload"]) for row in rows]
    if any(
        row["state"] != "active"
        or unit.get("subject") != target
        or unit.get("sharing") != group["sharing"]
        or unit.get("category") != group["category"]
        or unit.get("field_key") != group["field_key"]
        or unit.get("visibility") != "shared_projection"
        or unit.get("record_version") != row["version"]
        or unit.get("record_id") != row["id"]
        or unit.get("semantic_group_id") != group["id"]
        or not unit.get("sources")
        or any(source.get("kind") != "shareable_projection" for source in unit["sources"])
        for row, unit in zip(rows, units, strict=True)
    ):
        return None
    return units


def _eligible(service, db, groups, scope, target):
    from .recall_index import current_groups, projection_queries

    result = []
    postgres = is_postgres(db)
    batch_size = GROUP_READ_BATCH if postgres else 1
    for offset in range(0, len(groups), batch_size):
        batch = groups[offset : offset + batch_size]
        approved = [
            group
            for group in batch
            if json.loads(group["scope"])
            == dict(
                audience_key(scope["actor_id"], group["sharing"], scope["conversation_id"]),
                profile_subject=target,
            )
        ]
        records, projections = defaultdict(list), None
        if postgres:
            keys = [group["id"] for group in approved]
            if not keys:
                continue
            lineage = defaultdict(list)
            for row in db.execute(
                "SELECT l.group_id,l.revision AS required_revision,l.epoch AS required_epoch,"
                "s.key,s.state,s.revision,s.epoch FROM lineage l JOIN sources s "
                "ON s.key=l.source_key WHERE l.group_id IN (" + ",".join("?" for _ in keys) + ")",
                keys,
            ):
                lineage[row["group_id"]].append(row)
            current = current_groups(service, db, keys, lineage)
            approved = [group for group in approved if group["id"] in current]
            keys = [group["id"] for group in approved]
            if not keys:
                continue
            # Bodies are queued only after every retained group's source authority passes.
            for row in db.execute(
                "SELECT * FROM records WHERE group_id IN ("
                + ",".join("?" for _ in keys)
                + ") ORDER BY group_id,id",
                keys,
            ):
                records[row["group_id"]].append(row)
        else:
            approved = [group for group in approved if service._group_current(db, group["id"])]
            for group in approved:
                records[group["id"]] = db.execute(
                    "SELECT * FROM records WHERE group_id=? ORDER BY id", (group["id"],)
                ).fetchall()
        units, malformed = {}, None
        for group in approved:
            try:
                units[group["id"]] = _units(group, records[group["id"]], target)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                # Preserve the first group's projection/schema error before a
                # later malformed body, as the original scalar loop did.
                malformed = error
                break
        if postgres:
            projections = {}
            for sql, parameters in projection_queries(units):
                projections.update((row["ref"], row) for row in db.execute(sql, parameters))
        for group in approved:
            if group["id"] not in units:
                raise malformed
            group_units = units[group["id"]]
            if group_units is None or not service._projections_current(
                db, group_units, projections=projections
            ):
                continue
            if service.contracts.profile_version is not None:
                for unit in group_units:
                    service.contracts.validate("profiles#unit", unit)
            result.append((group, records[group["id"]], group_units))
    return result
