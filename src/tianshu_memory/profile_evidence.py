"""Build a reviewable profile draft from exact evidence versions under existing consent grants."""

import json

from .domain import canonical, parse_time, require
from .fact_migration import installed
from .user_approval import LocalUserApproval
from .workflow import TrustedWorkflow


def prepare(application, action, principal, credential):
    require(
        set(action)
        == {
            "operation",
            "origin",
            "records",
            "field_key",
            "sharing",
            "conversation_id",
            "expires_at",
        },
        "invalid_input",
        400,
    )
    refs = action["records"]
    require(type(refs) is list and 0 < len(refs) <= 8, "invalid_input", 400)
    service = application.service
    # Authenticate the existing local user and resolve their owner scope before reading IDs.
    # Construction only checks authority; it does not create an approval or publish anything.
    identity = LocalUserApproval(
        service,
        application.config_path,
        principal,
        credential,
        dict(
            operation="approve_profile",
            origin=action["origin"],
            draft={},
            expires_at=action["expires_at"],
        ),
    )
    allowed = identity.context["allowed_scope"]
    units, scopes, groups = [], [], []
    with service.store.transaction() as db:
        for ref in refs:
            require(
                type(ref) is dict and set(ref) == {"record_id", "record_version"},
                "invalid_input",
                400,
            )
            row = db.execute(
                "SELECT r.*,g.scope,g.category FROM records r JOIN groups g ON g.id=r.group_id "
                "WHERE r.id=? AND r.state='active' AND g.state='active' "
                "AND json_extract(g.scope,'$.person_id')=? AND json_extract(g.scope,'$.actor_id')=?",
                (ref["record_id"], allowed["person_id"], allowed["actor_id"]),
            ).fetchone()
            require(row is not None, "not_found", 404)
            require(row["version"] == ref["record_version"], "version_conflict", 409)
            from .consolidation_sources import metadata as summary_metadata

            # Contextual summaries have several authors/derived dependencies;
            # the old exact-evidence public draft cannot represent that closure.
            require(
                row["group_id"] not in summary_metadata(db, [row["group_id"]]), "invalid_input", 400
            )
            unit = json.loads(row["payload"])
            require(
                unit.get("reality") == "real"
                and row["category"] == "evidence"
                and all(s["kind"] == "raw_message" for s in unit["sources"]),
                "invalid_input",
                400,
            )
            if installed(db):
                fact_row = db.execute(
                    "SELECT payload,superseded_by FROM automatic_facts WHERE group_id=?",
                    (row["group_id"],),
                ).fetchone()
                if fact_row:
                    fact = json.loads(fact_row["payload"])
                    require(
                        fact_row["superseded_by"] is None
                        and fact["state"] == "current"
                        and (
                            fact["valid_from"] is None
                            or parse_time(fact["valid_from"]) <= service.clock()
                        )
                        and (
                            fact["valid_to"] is None
                            or parse_time(fact["valid_to"]) > service.clock()
                        ),
                        "version_conflict",
                        409,
                    )
            scopes.append(json.loads(row["scope"]))
            groups.append(row["group_id"])
            units.append(
                dict(
                    {
                        k: unit[k]
                        for k in (
                            "statement",
                            "conditions",
                            "negations",
                            "valid_time",
                            "uncertainty",
                            "reality",
                        )
                    },
                    sources=[s["source"] for s in unit["sources"]],
                )
            )
    require(all(scope == scopes[0] for scope in scopes), "invalid_input", 400)
    draft = dict(
        source_scope=scopes[0],
        subject=dict(kind="person", person_id=scopes[0]["person_id"]),
        category="interest",
        field_key=action["field_key"],
        sharing=action["sharing"],
        conversation_id=action["conversation_id"],
        units=units,
    )
    approval_action = dict(
        operation="approve_profile",
        origin=action["origin"],
        draft=draft,
        expires_at=action["expires_at"],
    )
    # Authenticate and recheck the exact existing user grant, but do not issue/consume consent.
    approval = LocalUserApproval(
        service, application.config_path, principal, credential, approval_action
    )
    workflow = TrustedWorkflow(service, approval, approval)
    with workflow._profile_operation(draft, approval.context) as db:
        approval.authority(db, draft, approval.context)
        for ref, group in zip(refs, groups, strict=True):
            row = db.execute(
                "SELECT version,state FROM records WHERE id=?", (ref["record_id"],)
            ).fetchone()
            require(
                row
                and row["state"] == "active"
                and row["version"] == ref["record_version"]
                and service._group_current(db, group),
                "version_conflict",
                409,
            )
    return dict(
        draft=json.loads(canonical(draft)),
        source_records=refs,
        approval_required=True,
        published=False,
    )
