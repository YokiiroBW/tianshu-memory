"""Exact owner windows and the dependencies of derived summaries, not text adjudication."""

import hashlib
import json

from .consolidation_migration import installed
from .domain import Fault, canonical, fingerprint, new_id, require, source_selector


class WindowChanged(Fault):
    """An exact window negative; only an unclaimed read may replace old context."""

    def __init__(self, window_id, member_ids, *, new_inputs_valid):
        super().__init__("scope_changed", 409)
        self.window_id = window_id
        self.member_ids = member_ids
        self.new_inputs_valid = new_inputs_valid


def validate_window(window, scope, contracts=None):
    manifest = window["manifest"]
    require(manifest["scope"] == scope, "invalid_input", 400)
    require(manifest["anchor"] in manifest["new_turns"], "invalid_input", 400)
    require(
        manifest["window_id"]
        == fingerprint({k: v for k, v in manifest.items() if k != "window_id"}),
        "invalid_input",
        400,
    )
    bodies = {member["member_id"]: member["text"] for member in window["members"]}
    identities = [member["member_id"] for member in manifest["members"]]
    require(
        len(bodies) == len(window["members"]) == len(set(identities)) == len(identities),
        "invalid_input",
        400,
    )
    require(set(bodies) == set(identities), "invalid_input", 400)
    require([item["member_id"] for item in window["members"]] == identities, "invalid_input", 400)
    require(sum(len(text.encode()) for text in bodies.values()) <= 49152, "invalid_input", 400)
    for member in manifest["members"]:
        if contracts is not None:
            for key, definition in (
                ("group_dependencies", "group_dependency"),
                ("episode_dependencies", "episode_dependency"),
                ("source_dependencies", "source_dependency"),
            ):
                for dependency in member["memory_dependencies"][key]:
                    contracts.validate("memory-context-v2#" + definition, dependency)
        require(
            hashlib.sha256(bodies[member["member_id"]].encode()).hexdigest()
            == member["content_sha256"],
            "invalid_input",
            400,
        )
        owner = member["source_scope"]
        require(
            all(owner[key] == scope[key] for key in ("actor_id", "audience", "conversation_id")),
            "forbidden",
            403,
        )
        require(member["binding_version"] > 0, "invalid_input", 400)
        if member["role"] == "user":
            require(
                member["reference"]["source"]["message_key"]["channel"] == manifest["channel"],
                "invalid_input",
                400,
            )
            require(
                member["speaker"]["kind"] == "person"
                and member["speaker"]["person_id"] == owner["person_id"],
                "invalid_input",
                400,
            )
        else:
            require(
                any(
                    item["role"] == "user" and item["turn_id"] == member["turn_id"]
                    for item in manifest["members"]
                ),
                "invalid_input",
                400,
            )
            require(
                member["role"] == "assistant"
                and member["speaker"] == {"kind": "actor", "actor_id": scope["actor_id"]},
                "invalid_input",
                400,
            )
    return manifest


def check_owner(transport, manifest):
    request = dict(schema_version=1, request_id=new_id("summary-window"), manifest=manifest)
    response = transport.window_check(request)
    require(
        response["request_id"] == request["request_id"]
        and response["request_digest"] == fingerprint(request)
        and response["window_id"] == manifest["window_id"],
        "dependency_unavailable",
        503,
    )
    identities = {member["member_id"] for member in manifest["members"]}
    invalid = response["invalid_member_ids"]
    require(
        set(invalid) <= identities and len(invalid) == len(set(invalid)),
        "dependency_unavailable",
        503,
    )
    require(response["state"] != "valid" or not invalid, "dependency_unavailable", 503)
    return response


def turns_current(manifest, turns, *, member_ids=None):
    """Fresh owner turn facts bind captured inputs and immutable sent replies."""
    for identity in {member["turn_id"] for member in manifest["members"]}:
        turn = turns.get(identity)
        members = [member for member in manifest["members"] if member["turn_id"] == identity]
        if member_ids is not None and not any(
            member["member_id"] in member_ids for member in members
        ):
            continue
        if turn is None or turn.get("state") == "missing":
            return False
        humans = [member for member in members if member["role"] == "user"]
        complete_input = [item["reference"]["source"] for item in humans] == turn["input_sources"]
        for member in members:
            if member_ids is not None and member["member_id"] not in member_ids:
                continue
            if turn["scope"] != member["source_scope"]:
                return False
            if member["role"] == "user":
                if member["purpose"] == "new" and not complete_input:
                    return False
                if (
                    member["reference"]["input_revision"] != turn["input_revision"]
                    or member["reference"]["source"] not in turn["input_sources"]
                ):
                    return False
            elif member["reference"]["reply_id"] not in turn["reply_ids"] or not complete_input:
                return False
    return True


def human_sources(manifest):
    """Actual input identities retain their own scopes, including context speakers."""
    result = {}
    for member in manifest["members"]:
        if member["role"] == "user":
            source, scope = member["reference"]["source"], member["source_scope"]
            key = canonical(source_selector(source, scope))
            prior = result.get(key)
            purpose = (
                "new" if member["purpose"] == "new" or (prior and prior[2] == "new") else "context"
            )
            result[key] = (source, scope, purpose)
    return list(result.values())


def subject_material(service, db, manifest, scope):
    """Rebuild only this owner's human originals, never a mixed summary's prose."""
    if manifest.get("schema_version") == 6:
        material = portrait_material(service, db, manifest, scope)
        return (
            [{key: value for key, value in item.items() if key != "reality"} for item in material]
            if material
            else None
        )
    result, budget = [], 0
    for member in manifest["members"]:
        if (
            member["role"] != "user"
            or member["source_scope"] != scope
            or member["speaker"]["kind"] != "person"
            or member["speaker"]["person_id"] != scope["person_id"]
        ):
            continue
        account = member["speaker"]["account"]
        binding = service._binding(db, account)
        if (
            binding is None
            or binding["person_id"] != scope["person_id"]
            or binding["version"] != member["binding_version"]
        ):
            return None
        source = member["reference"]["source"]
        key = service.source_identity(source, scope)
        row = db.execute(
            "SELECT s.* FROM sources s JOIN source_admissions a ON a.key=s.key AND a.verified=1 "
            "WHERE s.key=? AND NOT EXISTS (SELECT 1 FROM suppression w WHERE w.source_key=s.key)",
            (key,),
        ).fetchone()
        if (
            row is None
            or row["scope"] != canonical(scope)
            or row["state"] != "active"
            or row["payload"] != canonical(source)
            or row["revision"] != source["message_key"]["revision"]
        ):
            return None
        body = bytearray()
        for episode in db.execute(
            "SELECT * FROM memory_documents WHERE kind='episode' AND source_key=? "
            "AND source_revision=? AND source_epoch=? AND scope=? ORDER BY span_start,span_end,id",
            (key, row["revision"], row["epoch"], canonical(scope)),
        ):
            part = episode["text"].encode("utf-8")
            start, end = episode["span_start"], episode["span_end"]
            if (
                start < 0
                or end != start + len(part)
                or start > len(body)
                or hashlib.sha256(episode["text"].encode("utf-8")).hexdigest()
                != episode["content_hash"]
                or bytes(body[start : min(end, len(body))]) != part[: max(0, len(body) - start)]
            ):
                return None
            if end > len(body):
                body.extend(part[len(body) - start :])
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not text or hashlib.sha256(text.encode("utf-8")).hexdigest() != member["content_sha256"]:
            return None
        budget += len(text)
        if budget > 6000:
            return None
        result.append(
            dict(
                member_id=member["member_id"],
                speaker=member["speaker"],
                observed_at=member["observed_at"],
                text=text,
            )
        )
    return result or None


def source_dependencies(manifest):
    return [
        dep
        for member in manifest["members"]
        for dep in member["memory_dependencies"]["source_dependencies"]
    ]


def selectors(service, db, manifests):
    result = {}
    for manifest in manifests:
        for source, scope, _ in human_sources(manifest):
            selected = source_selector(source, scope)
            result[canonical(selected)] = selected
        for dep in source_dependencies(manifest):
            row = db.execute(
                "SELECT selector FROM source_admissions WHERE key=?", (dep["source_key"],)
            ).fetchone()
            if row is not None:
                result[row[0]] = json.loads(row[0])
    return list(result.values())


def stored_windows(db, scope, *, group_read=False):
    if not installed(db):
        return []
    if group_read:
        query = "json_extract(s.scope,'$.actor_id')=? AND json_extract(s.scope,'$.audience')='group' AND json_extract(s.scope,'$.conversation_id')=?"
        params = (scope["actor_id"], scope["conversation_id"])
    else:
        query, params = "s.scope=?", (canonical(scope),)
    manifests = {}
    for row in db.execute(
        "SELECT s.manifest FROM memory_summaries s JOIN groups g ON g.id=s.group_id WHERE g.state='active' AND "
        + query,
        params,
    ):
        value = json.loads(row[0])
        if value.get("schema_version") != 6:
            manifests[value["window_id"]] = value
        else:
            for window in value["context_windows"]:
                manifest = window["manifest"]
                manifests[manifest["window_id"]] = manifest
    return list(manifests.values())


def selected_dependencies(service, db, selection):
    """Current local proof obligations for a signed selection, not a new selection.

    Keep the originally signed sources as well as current DB edges. A changed
    record is rejected by the final evidence verifier; it must never introduce
    a source or inherited window that this checkpoint did not prove.
    """
    from .domain import require_selection
    from .memory_migration import installed as documents_installed
    from .segment_sources import equivalent_keys

    scope = selection["scope"]
    if not any(
        selection.get(key) for key in ("source_keys", "group_ids", "episode_ids", "windows")
    ):
        return set(), []
    allowed = permitted_scopes(service, db, scope)
    keys = set(selection["source_keys"])
    episode_ids = set(selection["episode_ids"])
    pending, seen, windows = set(selection["group_ids"]), set(), {}

    def include_window(window):
        windows[window["window_id"]] = window
        keys.update(dep["source_key"] for dep in source_dependencies(window))
        keys.update(
            service.source_identity(source, owner) for source, owner, _ in human_sources(window)
        )
        for member in window["members"]:
            dependencies = member["memory_dependencies"]
            pending.update(
                dep["group"]["semantic_group_id"]
                for dep in dependencies["group_dependencies"]
                if dep["group"]["semantic_group_id"] not in seen
            )
            episode_ids.update(dep["episode_id"] for dep in dependencies["episode_dependencies"])

    for window in selection.get("windows", []):
        include_window(window)
    while pending:
        page = sorted(pending)[:256]
        pending.difference_update(page)
        seen.update(page)
        marks = ",".join("?" for _ in page)
        groups = db.execute("SELECT id,scope FROM groups WHERE id IN (" + marks + ")", page)
        found = set()
        for group in groups:
            found.add(group["id"])
            require_selection(permits(scope, json.loads(group["scope"]), allowed))
        require_selection(found == set(page))
        keys.update(
            row[0]
            for row in db.execute(
                "SELECT source_key FROM lineage WHERE group_id IN (" + marks + ")", page
            )
        )
        if not installed(db):
            continue
        keys.update(
            row[0]
            for row in db.execute(
                "SELECT source_key FROM memory_summary_sources WHERE group_id IN (" + marks + ")",
                page,
            )
        )
        for summary in metadata(db, page).values():
            manifest = json.loads(summary["manifest"])
            inherited = (
                [item["manifest"] for item in manifest["context_windows"]]
                if manifest.get("schema_version") == 6
                else [manifest]
            )
            for window in inherited:
                include_window(window)
            refs = json.loads(summary["summary_refs"])
            for start in range(0, len(refs), 256):
                ids = [ref["record_id"] for ref in refs[start : start + 256]]
                rows = db.execute(
                    "SELECT id,group_id FROM records WHERE id IN ("
                    + ",".join("?" for _ in ids)
                    + ")",
                    ids,
                ).fetchall()
                require_selection({row["id"] for row in rows} == set(ids))
                pending.update(row["group_id"] for row in rows if row["group_id"] not in seen)
    episodes = sorted(episode_ids)
    if episodes:
        require_selection(documents_installed(db))
    for start in range(0, len(episodes), 256):
        ids = episodes[start : start + 256]
        rows = db.execute(
            "SELECT id,scope,source_key FROM memory_documents WHERE kind='episode' AND id IN ("
            + ",".join("?" for _ in ids)
            + ")",
            ids,
        ).fetchall()
        require_selection({row["id"] for row in rows} == set(ids))
        for row in rows:
            require_selection(permits(scope, json.loads(row["scope"]), allowed))
            keys.add(row["source_key"])
    keys = equivalent_keys(db, keys)
    for start in range(0, len(keys), 256):
        page = sorted(keys)[start : start + 256]
        rows = db.execute(
            "SELECT key,scope FROM sources WHERE key IN (" + ",".join("?" for _ in page) + ")",
            page,
        ).fetchall()
        require_selection({row["key"] for row in rows} == set(page))
        require_selection(all(permits(scope, json.loads(row["scope"]), allowed) for row in rows))
    return keys, list(windows.values())


def window_current(service, db, manifest, *, allowed=None, snapshot=None):
    """Known dependency changes are false; configuration and storage faults still raise."""
    allowed = permitted_scopes(service, db, manifest["scope"]) if allowed is None else allowed
    for source, scope, _ in human_sources(manifest):
        if not permits(manifest["scope"], scope, allowed):
            return False
        key = service.source_identity(source, scope)
        row = (
            db.execute(
                "SELECT s.* FROM sources s JOIN source_admissions a ON a.key=s.key AND a.verified=1 WHERE s.key=?",
                (key,),
            ).fetchone()
            if snapshot is None
            else snapshot.sources.get(key)
        )
        if (
            row is None
            or row["scope"] != canonical(scope)
            or row["payload"] != canonical(source)
            or row["state"] != "active"
            or row["revision"] != source["message_key"]["revision"]
            or (
                db.execute("SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)).fetchone()
                if snapshot is None
                else row["suppressed"]
            )
        ):
            return False
    for member in manifest["members"]:
        if member["role"] == "user":
            account = member["speaker"]["account"]
            binding = (
                service._binding(db, account)
                if snapshot is None
                else snapshot.bindings.get(canonical(account))
            )
            if (
                binding is None
                or binding["person_id"] != member["source_scope"]["person_id"]
                or binding["version"] != member["binding_version"]
            ):
                return False
        dependencies = member["memory_dependencies"]
        by_id = {dep["dependency_id"]: dep for dep in dependencies["source_dependencies"]}
        if len(by_id) != len(dependencies["source_dependencies"]):
            return False
        for dep in dependencies["source_dependencies"]:
            if not permits(manifest["scope"], dep["owner_scope"], allowed):
                return False
            from .segment_sources import dependency_rows

            row = (
                dependency_rows(db, [dep["source_key"]]).get(dep["source_key"])
                if snapshot is None
                else snapshot.sources.get(dep["source_key"])
            )
            if (
                row is None
                or (row["physical"] is None and not dict(row).get("source_content_hash"))
                or row["scope"] != canonical(dep["owner_scope"])
                or row["state"] != "active"
                or (row["revision"], row["epoch"]) != (dep["revision"], dep["epoch"])
                or (
                    dict(row).get("source_content_hash")
                    or json.loads(row["physical"])["content_digest"]
                )
                != dep["content_hash"]
                or (
                    db.execute(
                        "SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)
                    ).fetchone()
                    if snapshot is None
                    else row["suppressed"]
                )
            ):
                return False
        for dep in dependencies["group_dependencies"]:
            identity = dep["group"]["semantic_group_id"]
            group = (
                db.execute(
                    "SELECT state,members,scope FROM groups WHERE id=?", (identity,)
                ).fetchone()
                if snapshot is None
                else snapshot.groups.get(identity)
            )
            projection = (
                (
                    db.execute(
                        "SELECT kind FROM memory_summaries WHERE group_id=?", (identity,)
                    ).fetchone()
                    if installed(db)
                    else None
                )
                if snapshot is None
                else ((snapshot.kinds[identity],) if identity in snapshot.kinds else None)
            )
            superseded_portrait = (
                projection is not None
                and projection[0] == "portrait"
                and group is not None
                and group["state"] == "superseded"
            )
            if (
                group is None
                or (group["state"] != "active" and not superseded_portrait)
                or group["scope"] != canonical(dep["owner_scope"])
                or set(json.loads(group["members"])) != set(dep["group"]["record_ids"])
                or not service._group_current(db, identity)
            ):
                return False
            if {ref["record_id"] for ref in dep["records"]} != set(dep["group"]["record_ids"]):
                return False
            for ref in dep["records"]:
                record = (
                    db.execute(
                        "SELECT group_id,version,state FROM records WHERE id=?", (ref["record_id"],)
                    ).fetchone()
                    if snapshot is None
                    else snapshot.records.get(ref["record_id"])
                )
                if (
                    record is None
                    or record["group_id"] != identity
                    or record["version"] != ref["record_version"]
                    or record["state"] != "active"
                ):
                    return False
            if snapshot is None:
                required_keys = {
                    row[0]
                    for row in db.execute(
                        "SELECT source_key FROM lineage WHERE group_id=?", (identity,)
                    )
                }
                if installed(db):
                    required_keys.update(
                        row[0]
                        for row in db.execute(
                            "SELECT source_key FROM memory_summary_sources WHERE group_id=?",
                            (identity,),
                        )
                    )
            else:
                required_keys = snapshot.lineage.get(identity, set())
            if (
                not set(dep["source_dependency_ids"]) <= set(by_id)
                or {by_id[key]["source_key"] for key in dep["source_dependency_ids"]}
                != required_keys
            ):
                return False
        for dep in dependencies["episode_dependencies"]:
            episode = (
                db.execute(
                    "SELECT * FROM memory_documents WHERE id=?", (dep["episode_id"],)
                ).fetchone()
                if snapshot is None
                else snapshot.episodes.get(dep["episode_id"])
            )
            source = by_id.get(dep["source_dependency_id"])
            if (
                episode is None
                or episode["kind"] != "episode"
                or episode["source_revision"] != dep["episode_version"]
                or episode["content_hash"] != dep["text_hash"]
                or source is None
                or source["source_key"] != episode["source_key"]
                or source["revision"] != episode["source_revision"]
                or source["epoch"] != episode["source_epoch"]
                or dep["span"]
                != {"start_utf8": episode["span_start"], "end_utf8": episode["span_end"]}
            ):
                return False
    return True


def permitted_scopes(service, db, scope):
    from .memory_context.authorization import authorized_scopes

    return {canonical(item[0]) for item in authorized_scopes(service, db, scope, True)}


def permits(owner, dependency, allowed):
    return canonical(dependency) in allowed or (
        owner["actor_id"] == dependency["actor_id"]
        and owner["audience"] == dependency["audience"] == "group"
        and owner["conversation_id"] == dependency["conversation_id"]
    )


def local_sources(service, db, manifest):
    """Core's window proof never bypasses Memory's durable negative or binding."""
    require(window_current(service, db, manifest), "scope_changed", 409)
    rows = {}
    for source, scope, purpose in human_sources(manifest):
        for row in service._source_rows(db, [source], scope):
            rows[row["key"]] = (dict(row), purpose)
    for dep in source_dependencies(manifest):
        row = dict(db.execute("SELECT * FROM sources WHERE key=?", (dep["source_key"],)).fetchone())
        rows.setdefault(row["key"], (row, "context"))
    require(bool(rows), "invalid_input", 400)
    return rows


def window_failure(service, db, manifest, turns, owner_check=None, *, allowed=None, snapshot=None):
    invalid = set(owner_check["invalid_member_ids"] if owner_check else [])
    unclassified = bool(owner_check and owner_check["state"] == "invalid" and not invalid)
    allowed = permitted_scopes(service, db, manifest["scope"]) if allowed is None else allowed
    for member in manifest["members"]:
        identity = member["member_id"]
        if not turns_current(manifest, turns, member_ids={identity}) or not window_current(
            service, db, dict(manifest, members=[member]), allowed=allowed, snapshot=snapshot
        ):
            invalid.add(identity)
    if not invalid and not unclassified:
        return None
    new = {member["member_id"] for member in manifest["members"] if member["purpose"] == "new"}
    return WindowChanged(
        manifest["window_id"],
        [member["member_id"] for member in manifest["members"] if member["member_id"] in invalid],
        new_inputs_valid=bool(new) and not unclassified and not new.intersection(invalid),
    )


def window_failures(service, db, manifests, turns, owner_checks):
    """Recheck every complete window before the caller invalidates any of them."""
    from .consolidation_snapshot import WindowSnapshot

    manifests = list(manifests)
    snapshot = WindowSnapshot(service, db, manifests, consolidated=installed(db))
    allowed = {}
    failures = {}
    for manifest in manifests:
        scope = manifest["scope"]
        key = canonical(scope)
        if key not in allowed:
            allowed[key] = permitted_scopes(service, db, scope)
        identity = manifest["window_id"]
        failures[identity] = window_failure(
            service,
            db,
            manifest,
            turns,
            owner_checks.get(identity),
            allowed=allowed[key],
            snapshot=snapshot,
        )
    return failures


def job_current(service, db, event, snapshot):
    """Schema 5 invalidates its actual new inputs, not unrelated typed candidates."""
    for key, (revision, epoch) in snapshot.items():
        row = db.execute(
            "SELECT s.*,a.payload AS admission,p.payload AS physical FROM sources s JOIN source_admissions a ON a.key=s.key AND a.verified=1 JOIN physical_sources p ON p.key=a.physical_key WHERE s.key=?",
            (key,),
        ).fetchone()
        if (
            row is None
            or row["scope"] != canonical(event["scope"])
            or row["state"] != "active"
            or row["revision"] != revision
            or row["epoch"] != epoch
            or db.execute("SELECT 1 FROM suppression WHERE source_key=?", (key,)).fetchone()
        ):
            return False
        admission, physical = json.loads(row["admission"]), json.loads(row["physical"])
        binding = service._binding(db, physical["author"])
        if (
            binding is None
            or binding["person_id"] != event["scope"]["person_id"]
            or binding["version"] != admission["binding_version"]
        ):
            return False
    return bool(snapshot)


def current(service, db, group_id):
    if not installed(db):
        return True
    summary = db.execute(
        "SELECT summary_refs,manifest FROM memory_summaries WHERE group_id=?", (group_id,)
    ).fetchone()
    if summary is None:
        return True
    owner = db.execute("SELECT scope FROM groups WHERE id=?", (group_id,)).fetchone()
    scope = json.loads(owner[0])
    allowed = permitted_scopes(service, db, scope)
    for row in db.execute(
        "SELECT d.revision AS required_revision,d.epoch AS required_epoch,s.* FROM memory_summary_sources d JOIN sources s ON s.key=d.source_key WHERE d.group_id=?",
        (group_id,),
    ):
        if (
            not permits(scope, json.loads(row["scope"]), allowed)
            or row["state"] != "active"
            or row["revision"] != row["required_revision"]
            or row["epoch"] != row["required_epoch"]
            or db.execute("SELECT 1 FROM suppression WHERE source_key=?", (row["key"],)).fetchone()
        ):
            return False
    for ref in json.loads(summary["summary_refs"]):
        row = db.execute(
            "SELECT r.version,r.state,g.state AS group_state FROM records r JOIN groups g ON g.id=r.group_id WHERE r.id=?",
            (ref["record_id"],),
        ).fetchone()
        if (
            row is None
            or row["state"] != "active"
            or row["group_state"]
            not in (
                {"active", "superseded"}
                if json.loads(summary["manifest"]).get("schema_version") == 6
                else {"active"}
            )
            or row["version"] != ref["record_version"]
        ):
            return False
    return True


def metadata(db, group_ids):
    if not group_ids or not installed(db):
        return {}
    result = {}
    for start in range(0, len(group_ids), 256):
        keys = group_ids[start : start + 256]
        for row in db.execute(
            "SELECT * FROM memory_summaries WHERE group_id IN ("
            + ",".join("?" for _ in keys)
            + ")",
            keys,
        ):
            result[row["group_id"]] = dict(row)
        from .segment_migration import installed as segments_installed

        if segments_installed(db):
            for row in db.execute(
                "SELECT * FROM memory_segment_items WHERE group_id IN ("
                + ",".join("?" for _ in keys)
                + ")",
                keys,
            ):
                if row["group_id"] in result:
                    result[row["group_id"]].update(
                        memory_kind=row["kind"], state=row["state"], valid_until=row["valid_until"]
                    )
    return result


def expired_sources(db, source_keys, clock):
    if not installed(db):
        return set()
    from .domain import parse_time

    keys, result = list(set(source_keys)), set()
    now = clock()
    for start in range(0, len(keys), 256):
        batch = keys[start : start + 256]
        for row in db.execute(
            "SELECT l.source_key,l.expires_at FROM memory_source_lifecycle l JOIN sources s ON s.key=l.source_key AND s.revision=l.revision WHERE l.source_key IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        ):
            if parse_time(row["expires_at"]) <= now:
                result.add(row["source_key"])
    return result


def invalidate_windows(service, db, window_ids):
    if not window_ids or not installed(db):
        return
    groups = [
        row[0]
        for row in db.execute(
            "SELECT group_id FROM memory_summaries WHERE window_id IN ("
            + ",".join("?" for _ in window_ids)
            + ")",
            list(window_ids),
        )
    ]
    service._invalidate(db, groups)
    for job in db.execute(
        "SELECT j.id FROM jobs j JOIN memory_batches b ON b.job_id=j.id WHERE j.state='pending' AND b.window_id IN ("
        + ",".join("?" for _ in window_ids)
        + ")",
        list(window_ids),
    ).fetchall():
        db.execute("UPDATE jobs SET state='scope_changed' WHERE id=?", (job[0],))
        service._emit(
            db,
            "memory.candidate_invalidated",
            dict(candidate_job_ref=job[0], state="scope_changed"),
        )


def dependents(db, groups=(), sources=()):
    result = set(groups)
    sources = set(sources)
    if not result and not sources:
        return result
    if not installed(db):
        return result
    if sources:
        result.update(
            row[0]
            for row in db.execute(
                "SELECT DISTINCT group_id FROM memory_summary_sources WHERE source_key IN ("
                + ",".join("?" for _ in sources)
                + ")",
                list(sources),
            )
        )
    changed = True
    while changed:
        changed = False
        for row in db.execute("SELECT group_id,summary_refs FROM memory_summaries"):
            if row["group_id"] in result:
                continue
            for ref in json.loads(row["summary_refs"]):
                parent = db.execute(
                    "SELECT group_id FROM records WHERE id=?", (ref["record_id"],)
                ).fetchone()
                if parent is not None and parent[0] in result:
                    result.add(row["group_id"])
                    changed = True
                    break
    return result


def portrait_material(service, db, manifest, scope):
    """Return raw owner messages, never a previous derived summary's prose."""
    if manifest.get("schema_version") != 6:
        original = subject_material(service, db, manifest, scope)
        if original is None:
            return None
        members = {member["member_id"]: member for member in manifest["members"]}
        for member in original:
            source = members[member["member_id"]]["reference"]["source"]
            row = db.execute(
                "SELECT reality FROM sources WHERE key=?", (service.source_identity(source, scope),)
            ).fetchone()
            if row is None:
                return None
            member["reality"] = row[0]
        return original
    from . import segment_sources

    result = []
    for member in manifest["members"]:
        if member["role"] != "human" or member["source_scope"] != scope:
            continue
        material = db.execute(
            "SELECT * FROM memory_materials WHERE id=?", (member["member_id"],)
        ).fetchone()
        # BOT legacy window human members can be retained as context without a
        # separately accepted material. They are never substituted into history.
        if material is None:
            return None
        original = json.loads(material["payload"])["member"]
        if (
            not segment_sources.current(service, db, material)
            or any(
                original[key] != member[key]
                for key in (
                    "speaker",
                    "source_scope",
                    "binding_version",
                    "text",
                    "observed_at",
                    "content_sha256",
                    "reality",
                )
            )
            or hashlib.sha256(original["text"].encode("utf-8")).hexdigest()
            != original["content_sha256"]
        ):
            return None
        result.append(
            dict(
                member_id=member["member_id"],
                speaker=member["speaker"],
                observed_at=member["observed_at"],
                text=original["text"],
                reality=original["reality"],
            )
        )
    return result if result and sum(len(item["text"]) for item in result) <= 6000 else None
