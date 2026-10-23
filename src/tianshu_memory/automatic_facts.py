"""Sourced automatic fact identity, consolidation and temporal retrieval.

All helpers run inside Memory's existing source/permission barrier and transaction.
Inferences never issue revision consent or publish a shared profile.
"""

import json
import re
import unicodedata

from . import fact_entities, fact_labels, fact_names
from .domain import canonical, fingerprint, parse_time, require
from .fact_migration import SUPPORTED_VERSIONS, installed, source_text_installed
from .fact_queries import for_groups, snapshot_for_groups
from .fact_updates import may_replace
from .postgres import GROUP_READ_BATCH

FIELDS = {"key", "value", "state", "valid_from", "valid_to", "keywords"}


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def validate(value):
    require(
        type(value) is dict
        and set(value)
        in (FIELDS, FIELDS | {"entity"}, FIELDS | {"entity", "canonical", "update", "reality"}),
        "invalid_input",
        400,
    )
    fact_entities.validate(value.get("entity"))
    require(
        type(value["key"]) is str and re.fullmatch(r"[a-z][a-z0-9_.-]{0,119}", value["key"]),
        "invalid_input",
        400,
    )
    require(
        type(value["value"]) is str
        and 0 < len(value["value"].strip()) <= (1200 if "canonical" in value else 600),
        "invalid_input",
        400,
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
    from .recall_index import eligible_groups

    groups = db.execute(
        "SELECT g.* FROM automatic_facts f JOIN groups g ON g.id=f.group_id "
        "WHERE f.scope=? ORDER BY f.observed_at DESC LIMIT 128",
        (canonical(scope),),
    ).fetchall()
    approved = eligible_groups(service, db, groups, scope)
    snapshot = snapshot_for_groups(db, approved, service=service)
    current_names = snapshot.names.current(service.clock, normalize)
    result = []
    for group, _, units in approved:
        identity = group["id"]
        row, value = snapshot.rows[identity], snapshot.decoded[identity]
        name = snapshot.names.entries.get(identity)
        if value["key"] == fact_names.KEY and name is None:
            continue
        if name is not None:
            value = name.fact
        value = {key: value[key] for key in (*FIELDS, "entity") if key in value}
        unit = units[0]
        applicable = (
            value["state"] == "current"
            and (not value["valid_from"] or parse_time(value["valid_from"]) <= service.clock())
            and (not value["valid_to"] or parse_time(value["valid_to"]) > service.clock())
        )
        result.append(
            dict(
                value,
                statement=unit["statement"],
                conditions=unit["conditions"],
                negations=unit["negations"],
                group_id=identity,
                superseded=row["superseded_by"] is not None
                or (name is not None and applicable and identity not in current_names),
            )
        )
    # The material is untrusted matching context, not new evidence or permission.
    while len(canonical(result).encode()) > 32768:
        result.pop()
    return result


def _semantic(fact, unit):
    value = dict(
        value=normalize(fact["value"]),
        state=fact["state"],
        valid_from=fact["valid_from"],
        valid_to=fact["valid_to"],
        conditions=sorted(normalize(x) for x in unit["conditions"]),
        negations=sorted(normalize(x) for x in unit["negations"]),
        reality=unit["reality"],
    )
    modality = fact.get("canonical", {}).get("modality", "asserted")
    if modality != "asserted":
        value["modality"] = modality
    if fact.get("update", {}).get("operation") == "retract":
        value["operation"] = "retract"
    return fingerprint(value)


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


def _supersede(writer, db, scope, group_id, successor, effective, fact):
    db.execute(
        "UPDATE automatic_facts SET superseded_by=? WHERE group_id=?",
        (successor, group_id),
    )
    for record in db.execute(
        "SELECT * FROM records WHERE group_id=? AND state='active'", (group_id,)
    ).fetchall():
        value = json.loads(record["payload"])
        value["valid_time"] = (
            f"历史记录；{effective} 起被后续陈述替代，不能作为当前事实。原时间：{value['valid_time']}"[
                :1200
            ]
        )
        _replace_unit(writer, db, record, value, scope, "automatic." + fact["key"], None)


def _prior_unit(db, identity, names):
    if names is not None:
        return names.entries[identity].unit
    row = db.execute(
        "SELECT payload FROM records WHERE group_id=? AND state='active'", (identity,)
    ).fetchone()
    return json.loads(row[0]) if row is not None else None


def commit(writer, db, draft, event, source_map):
    """Merge a semantic duplicate or append an auditable fact, never a bare DB injection."""
    require(installed(db), "dependency_unavailable", 503)
    service = writer.service
    fact = validate(draft["fact"])
    require(len(draft["units"]) == 1, "invalid_input", 400)
    unit = draft["units"][0]
    typed = fact.get("canonical") is not None
    selected_targets = []
    if typed:
        from . import canonical_facts

        require(fact["reality"] == unit["reality"] == event["reality"], "invalid_input", 400)
        service.contracts.validate(
            "memory-extraction#proposal_fact", fact["canonical"] | fact["update"]
        )
        require(
            all(
                fact[key] == fact["canonical"][key]
                for key in ("value", "state", "valid_from", "valid_to", "keywords")
            )
            and fact["key"] == fact["canonical"]["predicate"],
            "invalid_input",
            400,
        )
        fact = canonical_facts.resolve(
            service, db, canonical(draft["scope"]), fact, draft["quotes"]
        )
        draft = dict(draft, fact=fact)
        selected_targets = canonical_facts.targets(service, db, draft, fact)
        unit["statement"] = canonical_facts.render(
            fact["canonical"], operation=fact["update"]["operation"]
        )
    names = None
    if fact_names.candidate(fact):
        require(source_text_installed(db), "dependency_unavailable", 503)
        require(
            fact["key"] == fact_names.KEY and fact_names.grounded(fact, unit, draft["quotes"]),
            "invalid_input",
            400,
        )
        names = fact_names.family(service, db, draft["scope"])
        require(not names.incomplete, "invalid_input", 400)
    value_key = _semantic(fact, unit)
    scope = canonical(draft["scope"])
    if not typed:
        fact = fact_entities.resolve(service, db, scope, fact, draft["quotes"])
    identity_key = fact_entities.fact_key(fact)
    observed = draft["observed_at"]
    require(parse_time(observed).tzinfo is not None, "invalid_input", 400)
    existing = (
        db.execute(
            "SELECT f.*,g.members FROM automatic_facts f JOIN groups g ON g.id=f.group_id "
            "WHERE f.scope=? AND f.fact_key=? AND f.value_key=? AND g.state='active' "
            "AND f.superseded_by IS NULL ORDER BY f.observed_at DESC",
            (scope, identity_key, value_key),
        ).fetchall()
        if names is None
        else [
            dict(entry.row, members=entry.group["members"])
            for entry in names.entries.values()
            if entry.row["value_key"] == value_key and entry.row["superseded_by"] is None
        ]
    )
    existing = [
        row for row in existing if bool(json.loads(row["payload"]).get("canonical")) == typed
    ]
    existing = sorted(existing, key=lambda row: parse_time(row["observed_at"]), reverse=True)
    duplicate = next(
        (r for r in existing if names is not None or service._group_current(db, r["group_id"])),
        None,
    )
    if (
        duplicate is None
        and fact["state"] == "current"
        and not fact["valid_from"]
        and not fact["valid_to"]
    ):
        # A new undated confirmation may reinforce an explicitly dated current fact.
        # Retain that original interval and its evidence instead of erasing or splitting it.
        prior_rows = (
            db.execute(
                "SELECT f.*,g.members FROM automatic_facts f JOIN groups g ON g.id=f.group_id "
                "WHERE f.scope=? AND f.fact_key=? AND g.state='active' AND f.superseded_by IS NULL "
                "ORDER BY f.observed_at DESC",
                (scope, identity_key),
            ).fetchall()
            if names is None
            else [
                dict(entry.row, members=entry.group["members"])
                for entry in names.entries.values()
                if entry.row["superseded_by"] is None
            ]
        )
        for prior_row in sorted(
            prior_rows, key=lambda row: parse_time(row["observed_at"]), reverse=True
        ):
            prior = (
                json.loads(prior_row["payload"])
                if names is None
                else names.entries[prior_row["group_id"]].fact
            )
            if (
                bool(prior.get("canonical")) != typed
                or prior["state"] != "current"
                or (prior["valid_from"] and parse_time(prior["valid_from"]) > service.clock())
                or (prior["valid_to"] and parse_time(prior["valid_to"]) <= service.clock())
                or (names is None and not service._group_current(db, prior_row["group_id"]))
            ):
                continue
            prior_unit = _prior_unit(db, prior_row["group_id"], names)
            if (
                prior_unit
                and _semantic(dict(prior, valid_from=None, valid_to=None), prior_unit) == value_key
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
        if typed:
            merged.update(
                canonical=dict(
                    fact["canonical"], valid_from=merged["valid_from"], valid_to=merged["valid_to"]
                ),
                update=fact["update"],
            )
            saved["statement"] = canonical_facts.render(
                merged["canonical"], operation=fact["update"]["operation"]
            )
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
        if names is None and not typed:
            db.execute(
                "INSERT OR IGNORE INTO automatic_evidence(group_id,source_key,quote) VALUES (?,?,?)",
                (group_id, quote["source_key"], quote["quote"]),
            )
        else:
            db.execute(
                "INSERT OR IGNORE INTO automatic_evidence(group_id,source_key,quote,source_text) "
                "VALUES (?,?,?,?)",
                (group_id, quote["source_key"], quote["quote"], quote.get("source_text")),
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
    if typed:
        canonical_facts.reconcile(
            writer, db, draft, fact, selected_targets, group_id, supersede=_supersede
        )
        return group_id, ids
    # A past report cannot supersede the current value. A future-effective fact waits for its date.
    if duplicate:
        fact, unit = (dict(merged, key=fact_names.KEY) if names is not None else merged), saved
        observed = max(observed, duplicate["observed_at"], key=parse_time)
    effective = fact["valid_from"] or observed
    if (
        fact["state"] == "current"
        and parse_time(effective) <= service.clock()
        and (fact["valid_to"] is None or parse_time(fact["valid_to"]) > service.clock())
    ):
        previous = (
            db.execute(
                "SELECT * FROM automatic_facts WHERE scope=? AND fact_key=? "
                "AND group_id!=? AND superseded_by IS NULL",
                (scope, identity_key, group_id),
            ).fetchall()
            if names is None
            else [
                entry.row
                for entry in names.entries.values()
                if entry.row["group_id"] != group_id and entry.row["superseded_by"] is None
            ]
        )
        eligible = []
        for old in previous:
            prior = (
                json.loads(old["payload"]) if names is None else names.entries[old["group_id"]].fact
            )
            if (
                prior["state"] != "current"
                or parse_time(prior["valid_from"] or old["observed_at"]) > service.clock()
                or (prior["valid_to"] and parse_time(prior["valid_to"]) <= service.clock())
                or (names is None and not service._group_current(db, old["group_id"]))
            ):
                continue
            prior_unit = _prior_unit(db, old["group_id"], names)
            if prior_unit:
                eligible.append((old, prior, prior_unit))
        label_quotes = {}
        if fact_labels.candidate(fact):
            # The two replacement directions must each use their current
            # group's own source evidence. Reuse incoming quotes when possible;
            # older/newer retained groups are fetched together, never per row.
            label_quotes[group_id] = [proof["quote"] for proof in draft["quotes"]]
            needed = {
                old["group_id"]
                for old, prior, _ in eligible
                if parse_time(prior["valid_from"] or old["observed_at"]) > parse_time(effective)
            }
            if not fact_labels.positive_source(fact, label_quotes[group_id]):
                needed.add(group_id)
            keys = sorted(needed)
            for offset in range(0, len(keys), GROUP_READ_BATCH):
                batch = keys[offset : offset + GROUP_READ_BATCH]
                for row in db.execute(
                    "SELECT group_id,quote FROM automatic_evidence WHERE group_id IN ("
                    + ",".join("?" for _ in batch)
                    + ")",
                    batch,
                ):
                    label_quotes.setdefault(row["group_id"], []).append(row["quote"])
        # A queued older proposal is evidence of the past, even if it finishes last.
        # Decide this before retiring any previously current group.
        newer = [
            (old, prior, prior_unit)
            for old, prior, prior_unit in eligible
            if parse_time(prior["valid_from"] or old["observed_at"]) > parse_time(effective)
            and may_replace(
                fact, unit, prior, prior_unit, current_quotes=label_quotes.get(old["group_id"])
            )
        ]
        if newer:
            old, prior, _ = max(
                newer, key=lambda item: parse_time(item[1]["valid_from"] or item[0]["observed_at"])
            )
            _supersede(
                writer,
                db,
                draft["scope"],
                group_id,
                old["group_id"],
                prior["valid_from"] or old["observed_at"],
                fact,
            )
            return group_id, ids
        for old, prior, prior_unit in eligible:
            if parse_time(prior["valid_from"] or old["observed_at"]) >= parse_time(
                effective
            ) or not may_replace(
                prior, prior_unit, fact, unit, current_quotes=label_quotes.get(group_id)
            ):
                continue
            _supersede(
                writer,
                db,
                draft["scope"],
                old["group_id"],
                group_id,
                effective,
                prior,
            )
            # Only derived answers become stale; the human source itself was not withdrawn.
            service._bump(db, draft["scope"], source_history_changed=False)
    return group_id, ids


def index_text(db, unit, *, fact_schema=None):
    if not (installed(db) if fact_schema is None else fact_schema in SUPPORTED_VERSIONS):
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


def filter_current(db, candidates, query, clock, *, fact_snapshot=None):
    if not (installed(db) if fact_snapshot is None else fact_snapshot.installed):
        return candidates
    from .recall_language import history_query

    history = history_query(query)
    facts = for_groups(db, candidates) if fact_snapshot is None else fact_snapshot.rows
    decoded = None if fact_snapshot is None else fact_snapshot.decoded
    names = None if fact_snapshot is None else fact_snapshot.names
    current_names = names.current(clock, normalize) if names is not None else set()
    result = []
    for entry in candidates:
        row = (
            db.execute(
                "SELECT payload,superseded_by FROM automatic_facts WHERE group_id=?",
                (entry[0]["id"],),
            ).fetchone()
            if facts is None
            else facts.get(entry[0]["id"])
        )
        if row:
            fact = json.loads(row["payload"]) if decoded is None else decoded[entry[0]["id"]]
            if names is not None:
                name = names.entries.get(entry[0]["id"])
                if (fact["key"] == fact_names.KEY and name is None) or (
                    name is not None and not history and entry[0]["id"] not in current_names
                ):
                    continue
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
    if not history and fact_snapshot is not None:
        from .canonical_facts import current_groups

        result = current_groups(result, fact_snapshot, clock)
    return fact_entities.filter_targets(db, result, query, facts=facts, decoded=decoded)
