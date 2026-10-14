"""Sourced automatic fact identity, consolidation and temporal retrieval.

All helpers run inside Memory's existing source/permission barrier and transaction.
Inferences never issue revision consent or publish a shared profile.
"""

import json
import re
import unicodedata

from . import fact_entities
from .domain import canonical, fingerprint, parse_time, require
from .fact_migration import installed

FIELDS = {"key", "value", "state", "valid_from", "valid_to", "keywords"}


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def validate(value):
    require(
        type(value) is dict and set(value) in (FIELDS, FIELDS | {"entity"}), "invalid_input", 400
    )
    fact_entities.validate(value.get("entity"))
    require(
        type(value["key"]) is str and re.fullmatch(r"[a-z][a-z0-9_.-]{0,119}", value["key"]),
        "invalid_input",
        400,
    )
    require(
        type(value["value"]) is str and 0 < len(value["value"].strip()) <= 600, "invalid_input", 400
    )
    require(
        type(value["state"]) is str and value["state"] in {"current", "historical"},
        "invalid_input",
        400,
    )
    require(
        type(value["keywords"]) is list
        and len(value["keywords"]) <= 12
        and all(type(x) is str and 0 < len(x.strip()) <= 40 for x in value["keywords"]),
        "invalid_input",
        400,
    )
    for key in ("valid_from", "valid_to"):
        if value[key] is not None:
            require(type(value[key]) is str and len(value[key]) <= 40, "invalid_input", 400)
            try:
                require(parse_time(value[key]).tzinfo is not None, "invalid_input", 400)
            except ValueError:
                require(False, "invalid_input", 400)
    if value["valid_from"] and value["valid_to"]:
        require(
            parse_time(value["valid_from"]) < parse_time(value["valid_to"]), "invalid_input", 400
        )
    return value


def known(service, db, scope):
    if not installed(db):
        return []
    result = []
    for row in db.execute(
        "SELECT * FROM automatic_facts WHERE scope=? ORDER BY observed_at DESC LIMIT 128",
        (canonical(scope),),
    ).fetchall():
        if service._group_current(db, row["group_id"]):
            value = json.loads(row["payload"])
            record = db.execute(
                "SELECT payload FROM records WHERE group_id=? AND state='active'",
                (row["group_id"],),
            ).fetchone()
            if record is None:
                continue
            unit = json.loads(record[0])
            result.append(
                dict(
                    value,
                    statement=unit["statement"],
                    conditions=unit["conditions"],
                    negations=unit["negations"],
                    group_id=row["group_id"],
                    superseded=row["superseded_by"] is not None,
                )
            )
    # The material is untrusted matching context, not new evidence or permission.
    while len(canonical(result).encode()) > 32768:
        result.pop()
    return result


def _semantic(fact, unit):
    return fingerprint(
        dict(
            value=normalize(fact["value"]),
            state=fact["state"],
            valid_from=fact["valid_from"],
            valid_to=fact["valid_to"],
            conditions=sorted(normalize(x) for x in unit["conditions"]),
            negations=sorted(normalize(x) for x in unit["negations"]),
            reality=unit["reality"],
        )
    )


def _replace_unit(writer, db, row, unit, scope, field, item):
    unit["record_version"] = row["version"] + 1
    writer.service.contracts.validate("identity-memory#unit", unit)
    db.execute(
        "UPDATE records SET version=?,payload=? WHERE id=?",
        (unit["record_version"], canonical(unit), row["id"]),
    )
    db.execute(
        "INSERT INTO history VALUES (?,?,'active',?)",
        (row["id"], unit["record_version"], canonical(unit)),
    )
    db.execute("DELETE FROM search_index WHERE record_id=?", (row["id"],))
    writer._index(db, unit, scope, field, item)


def commit(writer, db, draft, event, source_map):
    """Merge a semantic duplicate or append an auditable fact, never a bare DB injection."""
    require(installed(db), "dependency_unavailable", 503)
    service = writer.service
    fact = validate(draft["fact"])
    require(len(draft["units"]) == 1, "invalid_input", 400)
    unit = draft["units"][0]
    value_key = _semantic(fact, unit)
    scope = canonical(draft["scope"])
    fact = fact_entities.resolve(service, db, scope, fact, draft["quotes"])
    identity_key = fact_entities.fact_key(fact)
    observed = draft["observed_at"]
    require(parse_time(observed).tzinfo is not None, "invalid_input", 400)
    existing = db.execute(
        "SELECT f.*,g.members FROM automatic_facts f JOIN groups g ON g.id=f.group_id "
        "WHERE f.scope=? AND f.fact_key=? AND f.value_key=? AND g.state='active' "
        "AND f.superseded_by IS NULL ORDER BY f.observed_at DESC",
        (scope, identity_key, value_key),
    ).fetchall()
    duplicate = next((r for r in existing if service._group_current(db, r["group_id"])), None)
    if (
        duplicate is None
        and fact["state"] == "current"
        and not fact["valid_from"]
        and not fact["valid_to"]
    ):
        # A new undated confirmation may reinforce an explicitly dated current fact.
        # Retain that original interval and its evidence instead of erasing or splitting it.
        for prior_row in db.execute(
            "SELECT f.*,g.members FROM automatic_facts f JOIN groups g ON g.id=f.group_id "
            "WHERE f.scope=? AND f.fact_key=? AND g.state='active' AND f.superseded_by IS NULL "
            "ORDER BY f.observed_at DESC",
            (scope, identity_key),
        ).fetchall():
            prior = json.loads(prior_row["payload"])
            if (
                prior["state"] != "current"
                or (prior["valid_from"] and parse_time(prior["valid_from"]) > service.clock())
                or (prior["valid_to"] and parse_time(prior["valid_to"]) <= service.clock())
                or not service._group_current(db, prior_row["group_id"])
            ):
                continue
            prior_record = db.execute(
                "SELECT payload FROM records WHERE group_id=? AND state='active'",
                (prior_row["group_id"],),
            ).fetchone()
            if (
                prior_record
                and _semantic(
                    dict(prior, valid_from=None, valid_to=None), json.loads(prior_record[0])
                )
                == value_key
            ):
                duplicate = prior_row
                break
    raw = {k: v for k, v in draft.items() if k not in {"fact", "observed_at", "quotes"}}
    if duplicate:
        group_id, ids = duplicate["group_id"], json.loads(duplicate["members"])
        require(len(ids) == 1, "invalid_input", 400)
        row = db.execute(
            "SELECT * FROM records WHERE id=? AND state='active'", (ids[0],)
        ).fetchone()
        require(row is not None, "version_conflict", 409)
        saved = json.loads(row["payload"])
        sources = {canonical(s): s for s in saved["sources"]}
        for source in unit["sources"]:
            key = service.source_identity(source, draft["scope"])
            require(key in source_map and json.loads(source_map[key]["payload"]) == source)
            if saved["visibility"] == "self_private":
                sources[canonical(dict(kind="raw_message", source=source))] = dict(
                    kind="raw_message", source=source
                )
            db.execute(
                "INSERT OR IGNORE INTO lineage VALUES (?,?,?,?)",
                (group_id, key, source_map[key]["revision"], source_map[key]["epoch"]),
            )
        # Complete provenance stays in lineage/evidence. Bound the portable unit's references.
        saved["sources"] = list(sources.values())[-16:]
        merged = json.loads(duplicate["payload"])
        merged["keywords"] = list(dict.fromkeys(merged["keywords"] + fact["keywords"]))[:12]
        db.execute(
            "UPDATE automatic_facts SET observed_at=?,payload=? WHERE group_id=?",
            (max(observed, duplicate["observed_at"], key=parse_time), canonical(merged), group_id),
        )
    else:
        group_id, ids = writer._write_raw_group(db, raw, event, source_map)
        db.execute(
            "INSERT INTO automatic_facts VALUES (?,?,?,?,?,?,NULL)",
            (group_id, scope, identity_key, value_key, observed, canonical(fact)),
        )
    for quote in draft["quotes"]:
        require(quote["source_key"] in source_map, "invalid_input", 400)
        db.execute(
            "INSERT OR IGNORE INTO automatic_evidence VALUES (?,?,?)",
            (group_id, quote["source_key"], quote["quote"]),
        )
    if duplicate:
        _replace_unit(writer, db, row, saved, draft["scope"], draft["field_key"], draft["item_key"])
    else:
        for row in db.execute("SELECT * FROM records WHERE group_id=?", (group_id,)).fetchall():
            db.execute("DELETE FROM search_index WHERE record_id=?", (row["id"],))
            writer._index(
                db,
                json.loads(row["payload"]),
                draft["scope"],
                draft["field_key"],
                draft["item_key"],
            )
    # A past report cannot supersede the current value. A future-effective fact waits for its date.
    effective = fact["valid_from"] or observed
    if (
        fact["state"] == "current"
        and parse_time(effective) <= service.clock()
        and (fact["valid_to"] is None or parse_time(fact["valid_to"]) > service.clock())
    ):
        previous = db.execute(
            "SELECT * FROM automatic_facts WHERE scope=? AND fact_key=? "
            "AND group_id!=? AND superseded_by IS NULL",
            (scope, identity_key, group_id),
        ).fetchall()
        for old in previous:
            prior = json.loads(old["payload"])
            if (
                prior["state"] != "current"
                or parse_time(prior["valid_from"] or old["observed_at"]) >= parse_time(effective)
                or not service._group_current(db, old["group_id"])
            ):
                continue
            db.execute(
                "UPDATE automatic_facts SET superseded_by=? WHERE group_id=?",
                (group_id, old["group_id"]),
            )
            # A changed current fact invalidates replies that relied on its old value.
            service._bump(db, draft["scope"])
            for record in db.execute(
                "SELECT * FROM records WHERE group_id=? AND state='active'", (old["group_id"],)
            ).fetchall():
                value = json.loads(record["payload"])
                value["valid_time"] = (
                    f"历史记录；{effective} 起被后续陈述替代，不能作为当前事实。原时间：{value['valid_time']}"[
                        :1200
                    ]
                )
                _replace_unit(
                    writer, db, record, value, draft["scope"], "automatic." + prior["key"], None
                )
    return group_id, ids


def index_text(db, unit):
    if not installed(db):
        return ""
    group_id = unit["semantic_group_id"]
    row = db.execute("SELECT payload FROM automatic_facts WHERE group_id=?", (group_id,)).fetchone()
    if row is None:
        return ""
    fact = json.loads(row[0])
    quotes = [
        r[0]
        for r in db.execute(
            "SELECT quote FROM automatic_evidence WHERE group_id=? ORDER BY rowid LIMIT 128",
            (group_id,),
        )
    ]
    mention = fact["entity"]["mention"] if fact.get("entity") else ""
    return " ".join([fact["key"], fact["value"], mention, *fact["keywords"], *quotes])


def filter_current(db, candidates, query, clock):
    if not installed(db):
        return candidates
    history = any(
        x in query.casefold()
        for x in (
            "以前",
            "之前",
            "过去",
            "原来",
            "曾经",
            "旧",
            "去年",
            "上个月",
            "历史",
            "那次",
            "那时候",
            "当时",
            "经历",
            "that trip",
            "back then",
            "previous",
            "before",
            "history",
            "used to",
        )
    )
    result = []
    for entry in candidates:
        row = db.execute(
            "SELECT payload,superseded_by FROM automatic_facts WHERE group_id=?", (entry[0]["id"],)
        ).fetchone()
        if row:
            fact = json.loads(row["payload"])
            if row["superseded_by"] and not history:
                continue
            if fact["valid_from"] and parse_time(fact["valid_from"]) > clock():
                continue
            if not history and (
                fact["state"] == "historical"
                or (fact["valid_to"] and parse_time(fact["valid_to"]) <= clock())
            ):
                continue
        result.append(entry)
    return fact_entities.filter_targets(db, result, query)
