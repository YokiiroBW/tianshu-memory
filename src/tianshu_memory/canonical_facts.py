"""Atomic sourced properties, explicit updates and stable object/event identity."""

import json

from .domain import canonical, fingerprint, parse_time, require
from .fact_entities import normalize_mention

FIELDS = (
    "subject",
    "predicate",
    "value",
    "cardinality",
    "modality",
    "state",
    "valid_from",
    "valid_to",
    "keywords",
)
MODALITIES = {
    "asserted": "陈述",
    "observed": "来源报告已发生",
    "planned": "计划（不代表已执行）",
    "conditional": "有条件",
    "uncertain": "未确定",
}


def stored(proposal):
    return {key: proposal[key] for key in FIELDS}


def render(value, *, operation=None):
    if value["predicate"] == "identity.name" and value["subject"]["kind"] == "author":
        text = "姓名：" + value["value"]
    else:
        subject = value["subject"]["mention"] or "用户"
        text = f"{subject} 的 {value['predicate']}：{value['value']}"
    if operation == "retract":
        text = "已撤回这项主张：" + text
    if value["modality"] != "asserted":
        text = MODALITIES[value["modality"]] + "；" + text
    return text


def prepare(proposal):
    """The public schema is validated before conversion; metadata has one owner."""
    value = stored(proposal)
    if proposal["operation"] == "retract":
        value = dict(value, state="historical")
    require(value["modality"] in MODALITIES, "invalid_input", 400)
    if value["predicate"] == "identity.name":
        require(
            value["subject"]["kind"] == "author" and value["modality"] == "asserted",
            "invalid_input",
            400,
        )
    subject = value["subject"]
    return dict(
        key=value["predicate"],
        value=value["value"],
        state=value["state"],
        valid_from=value["valid_from"],
        valid_to=value["valid_to"],
        keywords=value["keywords"],
        entity=None
        if subject["kind"] == "author"
        else {"id": subject["id"], "kind": subject["type"], "mention": subject["mention"]},
        canonical=value,
        update={"operation": proposal["operation"], "target_refs": proposal["target_refs"]},
    )


def resolve(service, db, scope, fact, quotes):
    value = fact["canonical"]
    subject = dict(value["subject"])
    if subject["kind"] == "author":
        return fact
    require(any(subject["mention"] in proof["quote"] for proof in quotes), "invalid_input", 400)
    anchor = normalize_mention(subject["mention"])
    available = []
    from .fact_queries import snapshot_for_groups
    from .recall_index import eligible_groups

    groups = db.execute(
        "SELECT g.* FROM groups g JOIN automatic_facts f ON f.group_id=g.id WHERE g.scope=? AND g.state='active' AND f.superseded_by IS NULL",
        (scope,),
    ).fetchall()
    eligible = eligible_groups(service, db, groups, json.loads(scope))
    snapshot = snapshot_for_groups(db, eligible, service=service)
    for prior in snapshot.decoded.values():
        if prior.get("reality", "real") != fact["reality"]:
            continue
        entity = prior.get("entity")
        if entity and entity["kind"] == subject["type"]:
            available.append((prior, entity))
    matches = {
        entity["id"]
        for prior, entity in available
        if normalize_mention(entity["mention"]) == anchor
        or (
            prior["key"].split(".")[-1] in {"name", "title"}
            and prior["value"].strip() == subject["mention"].strip()
        )
    }
    if subject["id"] is not None:
        known = {entity["id"] for _, entity in available}
        require(subject["id"] in known, "invalid_input", 400)
        # A short reference may reuse a uniquely known object/event, never choose
        # arbitrarily between two objects of the same type.
        require(
            matches == {subject["id"]} or (not matches and known == {subject["id"]}),
            "invalid_input",
            400,
        )
        identity = subject["id"]
    else:
        require(len(matches) <= 1, "invalid_input", 400)
        identity = next(iter(matches), None) if subject["kind"] != "event" else None
        identity = identity or "entity:" + fingerprint(
            [scope, subject["kind"], subject["type"], anchor, quotes[0]["source_key"]]
        )
    existing = db.execute("SELECT * FROM fact_subjects WHERE id=?", (identity,)).fetchone()
    if existing:
        require(
            existing["scope"] == scope
            and existing["kind"] == subject["kind"]
            and existing["type"] == subject["type"],
            "invalid_input",
            400,
        )
    else:
        db.execute(
            "INSERT INTO fact_subjects VALUES (?,?,?,?,?,?)",
            (identity, scope, subject["kind"], subject["type"], anchor, quotes[0]["source_key"]),
        )
    subject["id"] = identity
    return dict(
        fact,
        canonical=dict(value, subject=subject),
        entity={"id": identity, "kind": subject["type"], "mention": subject["mention"]},
    )


def targets(service, db, draft, fact):
    """Validate exact optimistic targets before any write or semantic retirement."""
    from .fact_entities import fact_key

    update = fact["update"]
    result = []
    for target in update["target_refs"]:
        row = db.execute(
            "SELECT f.*,g.scope AS group_scope,g.members FROM automatic_facts f JOIN groups g ON g.id=f.group_id WHERE f.group_id=?",
            (target["group_id"],),
        ).fetchone()
        record = db.execute(
            "SELECT * FROM records WHERE id=? AND group_id=?",
            (target["record_id"], target["group_id"]),
        ).fetchone()
        require(
            row is not None
            and record is not None
            and row["scope"] == canonical(draft["scope"])
            and row["group_scope"] == row["scope"],
            "invalid_input",
            400,
        )
        require(
            record["version"] == target["record_version"]
            and record["state"] == "active"
            and service._group_current(db, row["group_id"]),
            "version_conflict",
            409,
        )
        require(json.loads(row["members"]) == [target["record_id"]], "invalid_input", 400)
        prior = json.loads(row["payload"])
        require(prior.get("canonical") is not None, "invalid_input", 400)
        require(fact_key(prior) == fact_key(fact), "invalid_input", 400)
        if prior.get("canonical"):
            require(
                prior["canonical"]["cardinality"] == fact["canonical"]["cardinality"],
                "invalid_input",
                400,
            )
        if update["operation"] in {"confirm", "retract"}:
            require(prior["value"] == fact["value"], "invalid_input", 400)
        if update["operation"] == "confirm":
            require(
                prior["state"] == fact["state"]
                and prior["valid_from"] == fact["valid_from"]
                and prior["valid_to"] == fact["valid_to"],
                "invalid_input",
                400,
            )
            require(
                json.loads(record["payload"])["conditions"] == draft["units"][0]["conditions"]
                and json.loads(record["payload"])["negations"] == draft["units"][0]["negations"],
                "invalid_input",
                400,
            )
        if update["operation"] == "replace" and fact["state"] == "historical":
            require(prior["state"] == "historical", "invalid_input", 400)
            require(
                json.loads(record["payload"])["conditions"] == draft["units"][0]["conditions"]
                and json.loads(record["payload"])["negations"] == draft["units"][0]["negations"],
                "invalid_input",
                400,
            )
        result.append((dict(row), prior))
    require(
        update["operation"] not in {"replace", "retract", "confirm"} or bool(result),
        "invalid_input",
        400,
    )
    current = db.execute(
        "SELECT f.group_id,f.payload FROM automatic_facts f JOIN groups g ON g.id=f.group_id WHERE f.scope=? AND f.fact_key=? AND f.superseded_by IS NULL AND g.state='active'",
        (canonical(draft["scope"]), fact_key(fact)),
    ).fetchall()
    for row in current:
        prior = json.loads(row["payload"])
        # Legacy statements are retained evidence, not a canonical slot lock.
        # A new explicit source can establish its first typed current value.
        if not prior.get("canonical"):
            continue
        if (
            prior["state"] != "current"
            or (prior["valid_from"] and parse_time(prior["valid_from"]) > service.clock())
            or (prior["valid_to"] and parse_time(prior["valid_to"]) <= service.clock())
            or not service._group_current(db, row["group_id"])
        ):
            continue
        if prior.get("canonical"):
            require(
                prior["canonical"]["cardinality"] == fact["canonical"]["cardinality"],
                "invalid_input",
                400,
            )
        active = (
            fact["state"] == "current"
            and (not fact["valid_from"] or parse_time(fact["valid_from"]) <= service.clock())
            and (not fact["valid_to"] or parse_time(fact["valid_to"]) > service.clock())
        )
        if (
            update["operation"] == "assert"
            and fact["canonical"]["cardinality"] == "single"
            and active
        ):
            require(prior["value"] == fact["value"], "invalid_input", 400)
    return result


def reconcile(writer, db, draft, fact, selected, group_id, *, supersede):
    operation = fact["update"]["operation"]
    if operation not in {"replace", "retract", "confirm"}:
        return
    # A confirmation carries the same value and complete target provenance in
    # its new group; its targets remain auditable predecessors, not competing
    # current copies that could reappear after retracting the confirmation.
    effective = fact["valid_from"] or draft["observed_at"]
    if parse_time(effective) > writer.service.clock():
        return
    for old, prior in selected:
        if old["group_id"] == group_id:
            continue
        prior_effective = prior["valid_from"] or old["observed_at"]
        if parse_time(prior_effective) > parse_time(effective):
            supersede(writer, db, draft["scope"], group_id, old["group_id"], prior_effective, fact)
            continue
        supersede(writer, db, draft["scope"], old["group_id"], group_id, effective, prior)
        writer.service._bump(db, draft["scope"], source_history_changed=False)


def known(service, db, scope, text, *, reality="real"):
    """Relevant current properties and events, preserving exact target versions."""
    from .domain import query_terms
    from .recall_index import eligible_groups

    groups = db.execute(
        "SELECT g.* FROM groups g JOIN automatic_facts f ON f.group_id=g.id WHERE g.scope=? AND g.state='active' AND f.superseded_by IS NULL ORDER BY f.observed_at DESC",
        (canonical(scope),),
    ).fetchall()
    eligible = eligible_groups(service, db, groups, scope)
    eligible = [entry for entry in eligible if all(unit["reality"] == reality for unit in entry[2])]
    from .fact_queries import snapshot_for_groups

    snapshot = snapshot_for_groups(db, eligible, service=service)
    eligible = current_groups(eligible, snapshot, service.clock)
    ranked = service._rank_groups(db, eligible, query_terms(text), text, fact_snapshot=snapshot)
    chosen = list(ranked)
    ids = {entry[0]["id"] for entry in chosen}
    # Open event properties and recent facts supplement lexical matches. They do
    # not displace an older directly relevant property before relevance scoring.
    chosen += [entry for entry in eligible if entry[0]["id"] not in ids]
    chosen = chosen[:128]
    result = []
    for group, rows, units in chosen:
        if len(rows) != 1:
            continue
        fact = snapshot.decoded[group["id"]]
        if fact["state"] != "current" or (
            fact["valid_to"] and parse_time(fact["valid_to"]) <= service.clock()
        ):
            continue
        value = fact.get("canonical")
        result.append(
            dict(
                representation="canonical" if value else "legacy_unstructured",
                fact=value,
                legacy_fact=None
                if value
                else {
                    key: fact[key]
                    for key in (
                        "key",
                        "value",
                        "state",
                        "valid_from",
                        "valid_to",
                        "keywords",
                        "entity",
                    )
                    if key in fact
                },
                legacy_statement=None if value else units[0]["statement"],
                conditions=units[0]["conditions"],
                negations=units[0]["negations"],
                valid_time=units[0]["valid_time"],
                target_refs=[
                    dict(
                        group_id=group["id"],
                        record_id=rows[0]["id"],
                        record_version=rows[0]["version"],
                    )
                ],
                superseded=False,
            )
        )
    while len(canonical(result).encode()) > 32768:
        result.pop()
    return result


def current_groups(candidates, snapshot, clock):
    """Canonical current slots take precedence without rewriting legacy records."""
    from .fact_entities import fact_key

    if not any(fact.get("canonical") for fact in snapshot.decoded.values()):
        return candidates
    latest = {}
    for group, _, units in candidates:
        identity = group["id"]
        fact, row = snapshot.decoded.get(identity), snapshot.rows.get(identity)
        if (
            not fact
            or not fact.get("canonical")
            or fact["canonical"]["cardinality"] != "single"
            or fact["state"] != "current"
            or row["superseded_by"]
        ):
            continue
        effective = parse_time(fact["valid_from"] or row["observed_at"])
        if effective <= clock() and (
            not fact["valid_to"] or parse_time(fact["valid_to"]) > clock()
        ):
            key = (group["scope"], fact_key(fact))
            latest[key] = max(latest.get(key, (effective, "")), (effective, identity))
    result = []
    for entry in candidates:
        group, _, units = entry
        identity = group["id"]
        fact, row = snapshot.decoded.get(identity), snapshot.rows.get(identity)
        if not fact:
            result.append(entry)
            continue
        key = (group["scope"], fact_key(dict(fact, reality=units[0].get("reality", "real"))))
        chosen = latest.get(key)
        if chosen is not None and chosen[1] != identity:
            if fact.get("canonical"):
                if fact["canonical"]["cardinality"] == "single":
                    continue
            elif chosen[0] >= parse_time(fact["valid_from"] or row["observed_at"]):
                continue
        result.append(entry)
    return result
