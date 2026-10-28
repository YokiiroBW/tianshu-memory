"""Passive material proof joined to the existing complete source checkpoint.

Platform owns archive/current actor policy, Core owns channel/conversation identity,
and Memory owns person binding and suppression. No observation creates a turn or
an admission. Only this checkpoint can mark its original source current.
"""

import json

from jsonschema import ValidationError

from .domain import Fault, canonical, require
from .segment_migration import installed


def frame(scope):
    return {key: scope[key] for key in ("actor_id", "audience", "conversation_id")}


def admitted(db, key):
    return (
        key.startswith("observation:")
        and installed(db)
        and db.execute(
            "SELECT 1 FROM memory_materials WHERE source_key=? AND kind='observation' AND verified=1",
            (key,),
        ).fetchone()
        is not None
    )


def collect(
    db,
    scope,
    summary_scopes=(),
    *,
    material_ids=(),
    source_keys=(),
    group_read=False,
    profile=False,
    required_only=False,
):
    if not installed(db):
        return []
    required = set(source_keys)
    # Read the same candidate metadata domain as the authority caller, not the
    # archive or every material ever accepted in a room. Historical raw episodes
    # are added explicitly by the v2 reader's existing material selection.
    domains = set()
    for owner, shared in () if required_only else ((scope, group_read), *summary_scopes):
        if shared:
            predicate = "json_extract(g.scope,'$.actor_id')=? AND json_extract(g.scope,'$.audience')='group' AND json_extract(g.scope,'$.conversation_id')=?"
            parameters = (owner["actor_id"], owner["conversation_id"])
        else:
            predicate, parameters = "g.scope=?", (canonical(owner),)
        if (predicate, parameters) in domains:
            continue
        domains.add((predicate, parameters))
        for table in ("lineage", "memory_summary_sources"):
            required.update(
                row[0]
                for row in db.execute(
                    "SELECT d.source_key FROM "
                    + table
                    + " d JOIN groups g ON g.id=d.group_id WHERE g.state='active' AND "
                    + predicate,
                    parameters,
                )
            )
    if profile and not required_only:
        required.update(
            row[0]
            for row in db.execute(
                "SELECT l.source_key FROM lineage l JOIN groups g ON g.id=l.group_id JOIN profile_shares p ON p.group_id=g.id "
                "WHERE g.state='active' AND p.actor_id=? AND (p.sharing='public_preference' OR (p.sharing='group_only' AND ?='group' AND p.conversation_id=?))",
                (scope["actor_id"], scope["audience"], scope["conversation_id"]),
            )
        )
    required = equivalent_keys(db, required)
    result = {}
    for field, values in (("id", sorted(set(material_ids))), ("source_key", sorted(required))):
        for start in range(0, len(values), 256):
            page = values[start : start + 256]
            predicate = "m." + field + " IN (" + ",".join("?" for _ in page) + ")"
            current = (
                " JOIN sources s ON s.key=m.source_key AND s.scope=m.scope"
                " AND s.revision=CAST(json_extract(m.payload,'$.source_revision') AS BIGINT)"
                " AND s.epoch=CAST(json_extract(m.payload,'$.source_epoch') AS BIGINT)"
                if field == "source_key"
                else ""
            )
            for row in db.execute(
                "SELECT m.* FROM memory_materials m" + current + " WHERE " + predicate,
                page,
            ):
                result[row["id"]] = dict(row)
    return [result[key] for key in sorted(result)]


def requirements(materials):
    checks, windows = {}, {}
    for row in materials:
        payload = json.loads(row["payload"])
        if row["kind"] == "core":
            event = payload["event"]
            check = dict(
                turn_id=event["aggregate_id"],
                input_revision=event["input_revision"],
                scope=event["scope"],
                sources=event["sources"],
            )
            checks[canonical(check)] = check
        for window in payload.get("context_windows", []):
            manifest = window["manifest"]
            windows[manifest["window_id"]] = manifest
    return list(checks.values()), list(windows.values())


def observation_request(rows, actor_id):
    first = json.loads(rows[0]["payload"])["archive"]
    event = first["event"]
    return dict(
        operation="segment",
        actor_id=actor_id,
        instance_id=event["platform_id"],
        self_id=event["self_id"],
        conversation_id=event["conversation_id"],
        sources=[
            dict(
                source_ref=(value := json.loads(row["payload"])["archive"])["source_ref"],
                source_digest=value["source_digest"],
                scope_version=value["scope_version"],
                archive_epoch=json.loads(row["payload"])["archive_epoch"],
            )
            for row in rows
        ],
    )


def check_platform(service, request):
    require(getattr(service, "observations", None) is not None, "dependency_unavailable", 503)
    try:
        service.contracts.validate("memory-extraction#segment_observation_check_request", request)
        proof = service.observations.verifier(request)
        service.contracts.validate("memory-extraction#segment_observation_check_response", proof)
    except ValidationError:
        raise Fault("dependency_unavailable", 503) from None
    require(
        proof.get("valid") is True and proof.get("actor_id") is not None,
        "dependency_unavailable",
        503,
    )
    if request["actor_id"] is not None:
        require(proof["actor_id"] == request["actor_id"], "forbidden", 403)
    require(
        all(
            proof.get(key) == request[key] for key in ("instance_id", "self_id", "conversation_id")
        ),
        "dependency_unavailable",
        503,
    )
    authority = proof.get("authority", {})
    require(
        set(authority) == {"owner_id", "epoch", "revision", "digest"}
        and type(authority["revision"]) is int
        and authority["revision"] >= 1
        and type(authority["epoch"]) is int
        and authority["epoch"] >= 1
        and isinstance(authority["digest"], str)
        and len(authority["digest"]) == 64,
        "dependency_unavailable",
        503,
    )
    require(len(proof.get("sources", [])) == len(request["sources"]), "dependency_unavailable", 503)
    for original, current in zip(request["sources"], proof["sources"], strict=True):
        require(
            all(current.get(key) == value for key, value in original.items()), "scope_changed", 409
        )
    return proof


def check_frame(service, actor_id, channel, conversation_id, delivery_channel=None):
    request = dict(
        schema_version=1,
        actor_id=actor_id,
        channel=channel,
        conversation_id=conversation_id,
        delivery_channel=delivery_channel,
    )
    result = service.source_authority.transport.frame_check(request)
    require(
        result.get("valid") is True
        and result.get("channel") == channel
        and result.get("delivery_channel") == delivery_channel
        and result.get("frame", {}).get("actor_id") == actor_id,
        "dependency_unavailable",
        503,
    )
    if conversation_id is not None:
        require(result["frame"]["conversation_id"] == conversation_id, "scope_changed", 409)
    return result["frame"]


def read(service, materials):
    groups = {}
    for row in materials:
        if row["kind"] != "observation":
            continue
        value = json.loads(row["payload"])
        event = value["archive"]["event"]
        key = canonical(
            [row["frame"], event["platform_id"], event["self_id"], event["conversation_id"]]
        )
        groups.setdefault(key, []).append(row)
    result = []
    for rows in groups.values():
        owner = json.loads(rows[0]["frame"])
        for offset in range(0, len(rows), 256):
            page = rows[offset : offset + 256]
            request = observation_request(page, owner["actor_id"])
            try:
                proof = check_platform(service, request)
                require(
                    all(
                        json.loads(row["payload"])["authority_owner"]
                        == proof["authority"]["owner_id"]
                        for row in page
                    ),
                    "scope_changed",
                    409,
                )
                require(
                    proof["channel"] == json.loads(page[0]["payload"])["channel"],
                    "scope_changed",
                    409,
                )
                require(
                    check_frame(
                        service,
                        owner["actor_id"],
                        proof["channel"],
                        owner["conversation_id"],
                        proof["delivery_channel"],
                    )
                    == owner,
                    "scope_changed",
                    409,
                )
            except Fault as error:
                if error.code != "scope_changed" or error.status != 409:
                    raise
                proof = None
            result.append(dict(rows=page, request=request, proof=proof))
    return result


def same(before, after):
    return len(before) == len(after) and all(
        a["request"] == b["request"] and a["proof"] == b["proof"]
        for a, b in zip(before, after, strict=True)
    )


def _material_inputs(db, materials, *, source_keys=(), owners=(), verified=False):
    """Bounded local rows for the caller's current transaction, never another proof."""
    payloads = {material["id"]: json.loads(material["payload"]) for material in materials}
    accounts = {canonical(value["member"]["speaker"]["account"]) for value in payloads.values()}
    for material in materials:
        if material["kind"] == "observation":
            event = json.loads(material["payload"])["archive"]["event"]
            accounts.add(
                canonical(
                    dict(namespace=event["namespace"], immutable_account_id=event["account_id"])
                )
            )
    keys = {material["source_key"] for material in materials} | set(source_keys)
    requests, destinations = [], []

    def add(bucket, table, column, values, fields="*"):
        values = sorted(set(values))
        for start in range(0, len(values), 256):
            page = values[start : start + 256]
            requests.append(
                (
                    "SELECT "
                    + fields
                    + " FROM "
                    + table
                    + " WHERE "
                    + column
                    + " IN ("
                    + ",".join("?" for _ in page)
                    + ")",
                    page,
                )
            )
            destinations.append(bucket)

    add("bindings", "accounts", "account_key", accounts)
    add("sources", "sources", "key", keys)
    add("suppressed", "suppression", "source_key", keys)
    add("heads", "memory_observation_heads", "owner_id", owners)
    if verified:
        add("verified", "memory_materials", "id", payloads, "id,verified")
    cursors = (
        []
        if not requests
        else db.read_batch(requests)
        if hasattr(db, "read_batch")
        else [db.execute(sql, parameters) for sql, parameters in requests]
    )
    result = dict(
        payloads=payloads, bindings={}, sources={}, suppressed=set(), heads={}, verified={}
    )
    columns = dict(bindings="account_key", sources="key", heads="owner_id")
    for bucket, cursor in zip(destinations, cursors, strict=True):
        for row in cursor:
            if bucket == "suppressed":
                result[bucket].add(row["source_key"])
            elif bucket == "verified":
                result[bucket][row["id"]] = row["verified"]
            else:
                result[bucket][row[columns[bucket]]] = row
    return result


def apply(service, db, observations):
    materials = [material for observation in observations for material in observation["rows"]]
    owners = [
        observation["proof"]["authority"]["owner_id"]
        for observation in observations
        if observation["proof"]
    ]
    local = _material_inputs(db, materials, owners=owners, verified=True)
    for observation in observations:
        proof = observation["proof"]
        if proof:
            head = proof["authority"]
            previous = local["heads"].get(head["owner_id"])
            require(
                previous is None
                or (
                    head["epoch"] >= previous["epoch"] and head["revision"] >= previous["revision"]
                ),
                "dependency_unavailable",
                503,
            )
            if previous is None or (head["epoch"], head["revision"]) != (
                previous["epoch"],
                previous["revision"],
            ):
                db.execute(
                    "INSERT INTO memory_observation_heads VALUES (?,?,?) ON CONFLICT(owner_id) DO UPDATE SET epoch=excluded.epoch,revision=excluded.revision WHERE excluded.epoch!=memory_observation_heads.epoch OR excluded.revision!=memory_observation_heads.revision",
                    (head["owner_id"], head["epoch"], head["revision"]),
                )
                local["heads"][head["owner_id"]] = dict(
                    owner_id=head["owner_id"], epoch=head["epoch"], revision=head["revision"]
                )
        for index, material in enumerate(observation["rows"]):
            payload = json.loads(material["payload"])
            event = payload["archive"]["event"]
            account = {"namespace": event["namespace"], "immutable_account_id": event["account_id"]}
            binding = local["bindings"].get(canonical(account))
            scope = json.loads(material["scope"])
            legal = (
                proof is not None
                and binding is not None
                and binding["person_id"] == scope["person_id"]
                and binding["version"] == material["binding_version"]
            )
            if legal:
                physical = proof["sources"][index]["physical_key"]
                require(
                    physical
                    == dict(
                        platform_id=event["platform_id"],
                        self_id=event["self_id"],
                        conversation_id=event["conversation_id"],
                        account_id=event["account_id"],
                        event_id=event["event_id"],
                        revision=event["revision"],
                    ),
                    "scope_changed",
                    409,
                )
            key = material["source_key"]
            old = local["sources"].get(key)
            if not legal:
                if local["verified"].get(material["id"]) not in (None, 0):
                    db.execute(
                        "UPDATE memory_materials SET verified=0 WHERE id=? AND verified!=0",
                        (material["id"],),
                    )
                    local["verified"][material["id"]] = 0
                if old is not None and old["state"] == "active":
                    db.execute(
                        "UPDATE sources SET state='withdrawn',epoch=epoch+1 WHERE key=?", (key,)
                    )
                    service._invalidate_sources(db, [key])
                    # The original invalidator broadcasts to exact aliases. Reload
                    # that real closure before later rows; an old active map must
                    # never re-verify a source just withdrawn by the same batch.
                    affected = equivalent_keys(db, [key])
                    changed = _material_inputs(
                        db,
                        [item for item in materials if item["source_key"] in affected],
                        verified=True,
                    )
                    for affected_key in affected:
                        local["sources"].pop(affected_key, None)
                    local["sources"].update(changed["sources"])
                    local["bindings"].update(changed["bindings"])
                    local["suppressed"].difference_update(affected)
                    local["suppressed"].update(changed["suppressed"])
                    local["verified"].update(changed["verified"])
                continue
            source = dict(
                kind="observation",
                owner="platform",
                source_ref=payload["archive"]["source_ref"],
                source_digest=payload["archive"]["source_digest"],
                scope_version=payload["archive"]["scope_version"],
                archive_epoch=payload["archive_epoch"],
            )
            if old is None:
                db.execute(
                    "INSERT INTO sources VALUES (?,1,1,'active',?,'real',?)",
                    (key, canonical(scope), canonical(source)),
                )
                local["sources"][key] = dict(
                    key=key,
                    revision=1,
                    epoch=1,
                    state="active",
                    scope=canonical(scope),
                    reality="real",
                    payload=canonical(source),
                )
            else:
                require(
                    old["scope"] == canonical(scope) and old["payload"] == canonical(source),
                    "scope_changed",
                    409,
                )
            if key not in local["suppressed"] and (old is None or old["state"] == "active"):
                if local["verified"].get(material["id"]) not in (None, 1):
                    db.execute(
                        "UPDATE memory_materials SET verified=1 WHERE id=? AND verified!=1",
                        (material["id"],),
                    )
                    local["verified"][material["id"]] = 1


def current(service, db, material):
    return material["id"] in current_many(service, db, [material])


def current_many(service, db, materials):
    """The material predicates over one bounded transaction-local input batch."""
    return _current_materials(materials, _material_inputs(db, materials))


def invalidated_many(db, materials):
    """Only durable negatives, not missing data or an unproved binding mismatch."""
    local = _material_inputs(db, materials)
    result = set()
    for material in materials:
        key = material["source_key"]
        payload = local["payloads"][material["id"]]
        row = local["sources"].get(key)
        expected = (payload["source_revision"], payload["source_epoch"])
        if key in local["suppressed"] or (
            row is not None
            and row["scope"] == material["scope"]
            and (row["state"] == "withdrawn" or (row["revision"], row["epoch"]) > expected)
        ):
            result.add(material["id"])
    return result


def _current_materials(materials, local):
    bindings, sources, suppressed = (local[key] for key in ("bindings", "sources", "suppressed"))
    payloads = local["payloads"]
    result = set()
    for material in materials:
        payload = payloads[material["id"]]
        member = payload["member"]
        binding = bindings.get(canonical(member["speaker"]["account"]))
        source = sources.get(material["source_key"])
        if (
            binding is not None
            and binding["person_id"] == member["source_scope"]["person_id"]
            and binding["version"] == material["binding_version"]
            and source is not None
            and source["state"] == "active"
            and source["scope"] == material["scope"]
            and source["revision"] == payload["source_revision"]
            and source["epoch"] == payload["source_epoch"]
            and source["key"] not in suppressed
            and (material["kind"] != "observation" or material["verified"] == 1)
        ):
            result.add(material["id"])
    return result


def _snapshot_current(snapshot, local):
    for key, (revision, epoch) in snapshot.items():
        row = local["sources"].get(key)
        if (
            row is None
            or row["state"] != "active"
            or row["revision"] != revision
            or row["epoch"] != epoch
            or key in local["suppressed"]
        ):
            return False
    return bool(snapshot)


def pending_current_many(service, db, jobs):
    """All original pending obligations over this transaction's bounded union."""
    if not jobs:
        return set()
    identities = sorted(
        {identity for event, _ in jobs.values() for identity in event["material_ids"]}
    )
    materials = {}
    for start in range(0, len(identities), 256):
        page = identities[start : start + 256]
        materials.update(
            (row["id"], dict(row))
            for row in db.execute(
                "SELECT * FROM memory_materials WHERE id IN (" + ",".join("?" for _ in page) + ")",
                page,
            )
        )
    rows = list(materials.values())
    keys = {key for _, snapshot in jobs.values() for key in snapshot}
    local = _material_inputs(db, rows, source_keys=keys)
    current = _current_materials(rows, local)
    return {
        job_id
        for job_id, (event, snapshot) in jobs.items()
        if _snapshot_current(snapshot, local)
        and all(identity in current for identity in event["material_ids"])
    }


def dependency_rows(db, keys):
    """Complete bounded original-source metadata for current evidence consumers."""
    result = {}
    for start in range(0, len(keys), 256):
        page = keys[start : start + 256]
        marks = ",".join("?" for _ in page)
        for row in db.execute(
            "SELECT s.*,p.payload AS physical FROM sources s JOIN source_admissions a ON a.key=s.key AND a.verified=1 JOIN physical_sources p ON p.key=a.physical_key WHERE s.key IN ("
            + marks
            + ")",
            page,
        ):
            result[row["key"]] = dict(
                row, source_content_hash=json.loads(row["physical"])["content_digest"]
            )
        if installed(db):
            for row in db.execute(
                "SELECT s.*,m.payload AS material_payload FROM sources s JOIN memory_materials m ON m.source_key=s.key AND m.scope=s.scope AND m.kind='observation' AND m.verified=1 WHERE s.key IN ("
                + marks
                + ")",
                page,
            ):
                result[row["key"]] = dict(
                    row,
                    physical=None,
                    source_content_hash=json.loads(row["material_payload"])["member"][
                        "content_sha256"
                    ],
                )
    return result


def equivalent_keys(db, keys):
    """Only the guarded exact Core→physical observation mappings, in both directions."""
    result = set(keys)
    if not installed(db):
        return result
    pending = set(result)
    while pending:
        page = sorted(pending)[:256]
        pending.difference_update(page)
        marks = ",".join("?" for _ in page)
        for row in db.execute(
            "SELECT a.source_key AS core_key,m.source_key AS observation_key FROM memory_material_aliases a "
            "JOIN memory_materials m ON m.id=a.material_id JOIN sources s ON s.key=a.source_key "
            "WHERE s.scope=m.scope AND (a.source_key IN ("
            + marks
            + ") OR m.source_key IN ("
            + marks
            + "))",
            [*page, *page],
        ):
            for key in (row["core_key"], row["observation_key"]):
                if key not in result:
                    result.add(key)
                    pending.add(key)
    return result
