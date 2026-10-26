"""Known-forget checks for exact, registered archive-to-native message mappings."""

import json

from jsonschema.exceptions import ValidationError

from . import browser, segment_sources
from .domain import Fault, require, source_key, utc
from .segment_migration import installed


def _same_scope(saved, scope):
    return all(
        saved.get(key) == scope[key] for key in ("actor_id", "audience", "conversation_id")
    ) and (scope["audience"] == "group" or saved.get("person_id") == scope["person_id"])


def _mapping(service, auth, caller_name, body):
    entry = (
        auth.config()
        .get("browser_readers", {})
        .get(caller_name, {})
        .get("archive_mappings", {})
        .get(body["mapping_id"])
    )
    require(isinstance(entry, dict) and entry.get("scope") == body["scope"], "forbidden", 403)
    try:
        service.contracts.validate("common#channel_key", entry["channel"])
        require(
            all(
                isinstance(entry[key], str) and 0 < len(entry[key]) <= 256
                for key in ("platform_id", "self_id", "room_id", "conversation_id")
            ),
            "dependency_unavailable",
            503,
        )
    except (KeyError, TypeError, ValidationError):
        raise Fault("dependency_unavailable", 503) from None
    return entry


def _core(db, mapping, scope, items, keys, known, negative, mismatched):
    physical_keys = {
        index: source_key(
            dict(
                message_key=dict(channel=mapping["channel"], message_id=item["alias"]["message_id"])
            )
        )
        for index, item in items.items()
    }
    if not physical_keys:
        return
    ids = sorted(set(physical_keys.values()))
    physicals = {
        row["key"]: row
        for row in db.execute(
            "SELECT * FROM physical_sources WHERE key IN (" + ",".join("?" for _ in ids) + ")",
            ids,
        )
    }
    admissions = {}
    for row in db.execute(
        "SELECT a.physical_key,a.key,s.scope FROM source_admissions a JOIN sources s ON s.key=a.key "
        "WHERE a.actor_id=? AND a.physical_key IN (" + ",".join("?" for _ in ids) + ")",
        [scope["actor_id"], *ids],
    ):
        if _same_scope(json.loads(row["scope"]), scope):
            admissions.setdefault(row["physical_key"], set()).add(row["key"])
    for index, key in physical_keys.items():
        physical = physicals.get(key)
        if physical is None or physical["payload"] is None:
            continue
        saved, item = json.loads(physical["payload"]), items[index]
        if not (
            saved["key"] == dict(channel=mapping["channel"], message_id=item["alias"]["message_id"])
            and saved["author"] == dict(namespace="qq", immutable_account_id=item["sender_id"])
            and saved["audience"] == scope["audience"]
            and saved["conversation_id"] == scope["conversation_id"]
        ):
            mismatched.add(index)
            continue
        known[index] = "core"
        keys[index].update(admissions.get(key, set()))
        if physical["state"] == "withdrawn":
            negative.add(index)


def _observations(db, mapping, scope, items, keys, known):
    if not items or not installed(db):
        return
    pairs = sorted({(item["sender_id"], item["alias"]["message_id"]) for item in items.values()})
    predicate = ",".join("(?,?)" for _ in pairs)
    archived = {
        (row["author"], row["event_id"]): row["source_ref"]
        for row in db.execute(
            "SELECT source_ref,author,event_id FROM observation_sources WHERE instance_id=? AND self_id=? "
            "AND conversation=? AND (author,event_id) IN (" + predicate + ")",
            [
                mapping["platform_id"],
                mapping["self_id"],
                mapping["conversation_id"],
                *(value for pair in pairs for value in pair),
            ],
        )
    }
    if not archived:
        return
    refs = sorted(set(archived.values()))
    materials = {}
    for row in db.execute(
        "SELECT source_key,scope,payload FROM memory_materials WHERE kind='observation' "
        "AND json_extract(frame,'$.actor_id')=? AND json_extract(frame,'$.audience')=? "
        "AND json_extract(frame,'$.conversation_id')=? AND json_extract(payload,'$.archive.source_ref') IN ("
        + ",".join("?" for _ in refs)
        + ")",
        [scope["actor_id"], scope["audience"], scope["conversation_id"], *refs],
    ):
        if not _same_scope(json.loads(row["scope"]), scope):
            continue
        event = json.loads(row["payload"])["archive"]["event"]
        if (event["platform_id"], event["self_id"], event["conversation_id"]) != (
            mapping["platform_id"],
            mapping["self_id"],
            mapping["conversation_id"],
        ):
            continue
        materials.setdefault((event["account_id"], event["event_id"]), set()).add(row["source_key"])
    for index, item in items.items():
        matched = materials.get((item["sender_id"], item["alias"]["message_id"]), set())
        if matched:
            keys[index].update(matched)
            # Observation is the canonical mapping if both proven entrances exist.
            known[index] = "observation"


def read(service, auth, caller_name, caller, body, context):
    service.contracts.validate("archive-memory-check#request", body)
    require(service.synchronized, "dependency_unavailable", 503)
    scope = body["scope"]
    for item in body["items"]:
        require(
            item["evidence_id"].rsplit(":", 1)[-1] == item["content_hash"], "invalid_input", 400
        )
    # The original browser assertion supplied by HTTP is independently current
    # again before opening the guarded local read. This is not a Core proof.
    with browser.authorized_snapshot(service, auth, caller_name, caller, body) as (db, _):
        require(
            dict(db.execute("SELECT key,value FROM metadata")).get("schema") == "3",
            "dependency_unavailable",
            503,
        )
        mapping = _mapping(service, auth, caller_name, body)
        supported = {
            index: item
            for index, item in enumerate(body["items"])
            if item["alias"]["id_kind"] == "external"
            and item["alias"]["platform"] == mapping["channel"]["namespace"] == "qq"
            and item["alias"]["account_id"] == mapping["self_id"]
            and item["alias"]["room_id"] == mapping["room_id"]
            and item["alias"]["message_type"]
            == ("group" if scope["audience"] == "group" else "private")
        }
        keys = {index: set() for index in supported}
        known, negative, mismatched = {}, set(), set()
        _core(db, mapping, scope, supported, keys, known, negative, mismatched)
        _observations(db, mapping, scope, supported, keys, known)
        closure = sorted(
            segment_sources.equivalent_keys(db, {key for values in keys.values() for key in values})
        )
        withdrawn = set()
        for start in range(0, len(closure), 256):
            page = closure[start : start + 256]
            for row in db.execute(
                "SELECT s.key,s.scope,s.state,p.source_key AS suppressed FROM sources s LEFT JOIN suppression p ON p.source_key=s.key "
                "WHERE s.key IN (" + ",".join("?" for _ in page) + ")",
                page,
            ):
                if _same_scope(json.loads(row["scope"]), scope) and (
                    row["state"] == "withdrawn" or row["suppressed"] is not None
                ):
                    withdrawn.add(row["key"])
        withdrawn = segment_sources.equivalent_keys(db, withdrawn)
        stamp = utc(service.clock())
        results = []
        for index, item in enumerate(body["items"]):
            state = (
                "unsupported"
                if index not in supported or index in mismatched
                else "no_known_forget"
            )
            if index in negative or keys.get(index, set()) & withdrawn:
                state = "withdrawn"
            results.append(
                dict(
                    evidence_id=item["evidence_id"],
                    content_hash=item["content_hash"],
                    forget_state=state,
                    mapping=known.get(index, "unmapped"),
                    checked_at=stamp,
                )
            )
        result = dict(
            schema_version=1,
            request_id=body["request_id"],
            scope=scope,
            mapping_id=body["mapping_id"],
            checked_at=stamp,
            items=results,
        )
        service.contracts.validate("archive-memory-check#response", result)
        return result
