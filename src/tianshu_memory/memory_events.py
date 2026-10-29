"""Rebuildable state from explicit same-owner record references and original sources."""

import json

from . import consolidation_sources
from .domain import Fault, canonical, fingerprint, require
from .event_projection import project
from .retrieval_migration import backup_before
from .segment_materials import authorize

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_event_refs (
 event_id TEXT NOT NULL, group_id TEXT NOT NULL REFERENCES groups(id), scope TEXT NOT NULL,
 relation TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(event_id,group_id));
CREATE INDEX IF NOT EXISTS memory_event_group ON memory_event_refs(group_id,event_id);
CREATE TABLE IF NOT EXISTS memory_event_state (
 event_id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, slots TEXT NOT NULL,
 basis TEXT NOT NULL, version BIGINT NOT NULL, updated_at TEXT NOT NULL);
"""


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='memory_event_schema'").fetchone()
    return row is not None and row[0] == "1"


def migrate(store, backup_path):
    with store.transaction() as db:
        if installed(db):
            return dict(memory_event_schema=1, already_installed=True)
        require(
            dict(db.execute("SELECT key,value FROM metadata")).get("memory_segment_schema") == "2",
            "dependency_unavailable",
            503,
        )
        backup, guard = backup_before(store, db, backup_path)
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("INSERT INTO metadata VALUES ('memory_event_schema','1')")
        db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
    return dict(memory_event_schema=1, backup=str(backup), backup_guard=str(guard))


def _kind(memory):
    if memory["kind"] == "agreement":
        return "agreement"
    if memory["kind"] == "thought" and memory.get("operation") == "replace":
        return "preference"
    if memory["kind"] == "experience" and memory["state"] in {
        "considering",
        "confirmed",
        "completed",
        "cancelled",
    }:
        return "task"
    return None


def _basis(db, group_id):
    sources = [
        dict(row)
        for row in db.execute(
            "SELECT source_key,revision,epoch,source_scope AS scope FROM memory_summary_sources WHERE group_id=?",
            (group_id,),
        )
    ]
    records = [
        dict(
            id=row["id"],
            version=row["version"],
            payload_hash=fingerprint(json.loads(row["payload"])),
        )
        for row in db.execute(
            "SELECT id,version,payload FROM records WHERE group_id=? ORDER BY id", (group_id,)
        )
    ]
    return dict(sources=sources, groups=[dict(group_id=group_id, records=records)])


def _valid(db, basis):
    # Explicit forgetting is checked before any reconstruction or slot use.
    for source in basis["sources"]:
        if db.execute(
            "SELECT 1 FROM suppression WHERE source_key=?", (source["source_key"],)
        ).fetchone():
            return False
        row = db.execute("SELECT * FROM sources WHERE key=?", (source["source_key"],)).fetchone()
        if (
            row is None
            or row["state"] != "active"
            or any(row[key] != source[key] for key in ("revision", "epoch", "scope"))
        ):
            return False
    for group in basis["groups"]:
        row = db.execute("SELECT state FROM groups WHERE id=?", (group["group_id"],)).fetchone()
        if row is None or row["state"] not in {"active", "superseded"}:
            return False
        for record in group["records"]:
            row = db.execute(
                "SELECT * FROM records WHERE id=? AND group_id=?", (record["id"], group["group_id"])
            ).fetchone()
            if (
                row is None
                or row["state"] != "active"
                or row["version"] != record["version"]
                or fingerprint(json.loads(row["payload"])) != record["payload_hash"]
            ):
                return False
    return bool(basis["sources"] and basis["groups"])


def _claims(db, event_id, group_id, scope, memory, recorded_at, prior_groups):
    state = db.execute(
        "SELECT effective_at FROM memory_segment_items WHERE group_id=?", (group_id,)
    ).fetchone()
    values = [
        ("global", "description", memory["text"]),
        ("participant", "reported_state", memory["state"]),
    ]
    if memory.get("valid_until"):
        values.append(("global", "valid_to", memory["valid_until"]))
    relation = (
        "cancels"
        if memory["state"] == "cancelled"
        else "updates"
        if memory.get("operation") == "replace"
        else "introduces"
    )
    return [
        dict(
            id=f"{group_id}:{slot}:{attribute}",
            event_id=event_id,
            group_id=group_id,
            slot=slot,
            attribute=attribute,
            value=value,
            person_id=scope["person_id"] if slot == "participant" else None,
            speaker=scope["person_id"],
            qualification="owner_summary",
            relation=relation,
            replaces=[f"{prior}:{slot}:{attribute}" for prior in prior_groups]
            if memory.get("operation") == "replace"
            else [],
            observed_at=state[0],
            valid_from=memory.get("valid_from"),
            valid_to=memory.get("valid_until"),
            recorded_at=recorded_at,
        )
        for slot, attribute, value in values
    ]


def record(db, scope, group_id, memory, recorded_at):
    kind = _kind(memory)
    if kind is None or not installed(db):
        return
    prior_groups = [
        row[0]
        for ref in memory.get("prior_refs", [])
        for row in db.execute(
            "SELECT r.group_id FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=? AND r.version=? AND g.scope=?",
            (ref["record_id"], ref["record_version"], canonical(scope)),
        )
    ]
    prior_states = {}
    for prior in prior_groups:
        for row in db.execute(
            "SELECT s.* FROM memory_event_state s JOIN memory_event_refs r ON r.event_id=s.event_id "
            "WHERE r.group_id=? AND s.scope=? AND s.kind=?",
            (prior, canonical(scope), kind),
        ):
            prior_states[row["event_id"]] = dict(row)
    previous = next(iter(prior_states.values())) if len(prior_states) == 1 else None
    event_id = previous["event_id"] if previous else "event:" + fingerprint([scope, group_id])
    basis = _basis(db, group_id)
    claims = []
    if previous:
        old = json.loads(previous["basis"])
        basis["sources"] += old["sources"]
        basis["groups"] += old["groups"]
        claims += old["claims"]
    basis["sources"] = list({canonical(row): row for row in basis["sources"]}.values())
    basis["groups"] = list({row["group_id"]: row for row in basis["groups"]}.values())
    if not _valid(db, basis):
        return
    claims += _claims(db, event_id, group_id, scope, memory, recorded_at, prior_groups)
    basis["claims"] = claims
    slots = project(claims)
    relation = claims[-1]["relation"]
    db.execute(
        "INSERT INTO memory_event_refs VALUES (?,?,?,?,?) ON CONFLICT(event_id,group_id) DO NOTHING",
        (event_id, group_id, canonical(scope), relation, recorded_at),
    )
    db.execute(
        "INSERT INTO memory_event_state VALUES (?,?,?,?,?,1,?) ON CONFLICT(event_id) DO UPDATE SET "
        "slots=excluded.slots,basis=excluded.basis,version=memory_event_state.version+1,updated_at=excluded.updated_at",
        (event_id, canonical(scope), kind, canonical(slots), canonical(basis), recorded_at),
    )


def read(service, request, publisher):
    require(set(request) == {"event_id", "scope"}, "invalid_input", 400)
    require(
        isinstance(request["event_id"], str) and 1 <= len(request["event_id"]) <= 256,
        "invalid_input",
        400,
    )
    scope = request["scope"]
    service.contracts.validate("common#scope", scope)
    authorize(publisher, scope)
    with service.store.transaction() as db:
        require(installed(db), "dependency_unavailable", 503)
        row = db.execute(
            "SELECT * FROM memory_event_state WHERE event_id=? AND scope=?",
            (request["event_id"], canonical(scope)),
        ).fetchone()
        require(row is not None, "not_found", 404)
        basis = json.loads(row["basis"])
    try:
        with service.operation(
            scope=scope,
            dependency_keys=[row["source_key"] for row in basis["sources"]],
            summary_scopes=[(scope, False)],
        ) as db:
            current = db.execute(
                "SELECT * FROM memory_event_state WHERE event_id=? AND scope=?",
                (request["event_id"], canonical(scope)),
            ).fetchone()
            if (
                current is None
                or current["version"] != row["version"]
                or not _valid(db, json.loads(current["basis"]))
            ):
                return dict(available=False, event_id=request["event_id"])
            allowed = consolidation_sources.permitted_scopes(service, db, scope)
            if not all(
                consolidation_sources.permits(scope, json.loads(source["scope"]), allowed)
                for source in basis["sources"]
            ):
                return dict(available=False, event_id=request["event_id"])
            return dict(
                available=True,
                event_id=current["event_id"],
                kind=current["kind"],
                version=current["version"],
                slots=json.loads(current["slots"]),
                updated_at=current["updated_at"],
            )
    except Fault as error:
        if error.code not in {"scope_changed", "not_found"}:
            raise
        return dict(available=False, event_id=request["event_id"])


def selection_links(db, groups):
    """Only whole currently eligible bases can connect existing evidence units.

    No slot body or extra source becomes evidence. Every linked group must fit
    and pass the original final source proof, or the entire unit is omitted.
    """
    if not groups or not installed(db):
        return []
    by_id = {entry[0]["id"]: entry for entry in groups}
    rows, seen, result = [], set(), []
    identities = list(by_id)
    for start in range(0, len(identities), 256):
        batch = identities[start : start + 256]
        rows.extend(
            db.execute(
                "SELECT s.* FROM memory_event_state s JOIN memory_event_refs r ON r.event_id=s.event_id "
                "WHERE r.group_id IN (" + ",".join("?" for _ in batch) + ")",
                batch,
            ).fetchall()
        )
    for row in rows:
        if row["event_id"] in seen:
            continue
        seen.add(row["event_id"])
        basis = json.loads(row["basis"])
        if not all(group["group_id"] in by_id for group in basis["groups"]):
            continue
        valid = True
        for group in basis["groups"]:
            entry = by_id[group["group_id"]]
            records = {record["id"]: record for record in entry[1]}
            if entry[0]["scope"] != row["scope"] or any(
                ref["id"] not in records
                or records[ref["id"]]["version"] != ref["version"]
                or fingerprint(json.loads(records[ref["id"]]["payload"])) != ref["payload_hash"]
                for ref in group["records"]
            ):
                valid = False
                break
        if valid:
            result.append([group["group_id"] for group in basis["groups"]])
    return result
