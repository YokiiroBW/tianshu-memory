"""Registered project directories: preview, explicit apply and resumable incremental import.

A preview pins every candidate's path, type, size, digest and current index version. An apply
can only confirm a preview this service issued to the same client for the same project and
directory, re-reads every file outside any transaction, and refuses any item whose path,
bytes, size or current version no longer match instead of importing new content under an old
plan. A source that disappeared only becomes an index tombstone when the operator lists it
explicitly; no code path here writes, moves or deletes a project file, and a truncated walk or
an exhausted budget is never treated as a removal.
"""

import re
from pathlib import Path

from .domain import Fault, canonical, fingerprint, now, require, terms, utc
from .knowledge_directories_schema import VERSION
from .knowledge_sources import (
    MAX_BYTES,
    content_hash,
    decode,
    directory_path,
    file_path,
    presence,
    read_file,
    walk_directory,
)

MIN_FILES, MAX_FILES = 1, 256
MIN_TOTAL, MAX_TOTAL = 1024, 8 * 1024 * 1024
MAX_DIRECTORIES = 16
LIST_LIMIT = 256
OMIT_LIMIT = 128
MISSING_LIMIT = 64
MAX_TOMBSTONES = 64
ITEM_FIELDS = "locator kind media_type size digest source_id document_id version action"
MISSING_FIELDS = "locator document_id source_id version reason"
OMITTED_FIELDS = "locator reason size"
COUNT_KEYS = (
    "indexed listed import unchanged unchanged_unlisted omitted omitted_unlisted "
    "missing links denied_directories"
).split()
PLAN_FIELDS = (
    "plan_id project_id client directory registration revision issued_at limits items "
    "omitted missing counts walk_truncated budget_incomplete missing_truncated deletions scan"
)
REASONS = {
    "unsupported_type",
    "refused_name",
    "source_too_large",
    "unsupported_encoding",
    "empty_source",
    "unreadable",
    "source_changed",
    "file_budget",
    "byte_budget",
}
PRESENCE_REASONS = {"not_present": "source_missing", "not_scannable": "source_not_scannable"}


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields.split()), "invalid_input", 400)


def source_id(project_id, locator):
    """The stable source identity of one imported file, derived exactly as a single import does."""
    return "source:" + fingerprint([project_id, "file", locator])


def document_id(source):
    """The stable document identity of one source."""
    return "document:" + fingerprint(source)


def text(value, maximum=2048):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)


def integer(value, minimum=0, maximum=2**31):
    require(type(value) is int and minimum <= value <= maximum, "invalid_input", 400)


def configuration(condition, code="invalid_configuration"):
    require(condition, code, 503)


def reason_of(error):
    """The preview reason for one refused path, taken from the source reader's fault code."""
    code = getattr(error, "code", "")
    if code == "unsupported":
        return "unsupported_type"
    if code == "forbidden":
        return "refused_name"
    return code if code in REASONS else "unreadable"


def registration(knowledge, project_id):
    """The operator-registered scan directories of one project, validated but not resolved.

    An absent section means this project may not scan anything: a directory never becomes
    readable by default and is never inferred from the project root. Limits are part of the
    registration, so a caller cannot raise them by asking for more.
    """
    section = knowledge.get("directories", {})
    configuration(isinstance(section, dict))
    entries = section.get(project_id, [])
    configuration(isinstance(entries, list) and len(entries) <= MAX_DIRECTORIES)
    result = []
    for entry in entries:
        configuration(
            isinstance(entry, dict) and set(entry) == set("path max_files max_bytes".split())
        )
        path = entry["path"]
        configuration(isinstance(path, str) and 0 < len(path) <= 1024)
        relative = Path(path)
        configuration(
            not relative.is_absolute()
            and not relative.drive
            and ":" not in path
            and all(part not in {".", ".."} for part in relative.parts)
        )
        for name, low, high in (
            ("max_files", MIN_FILES, MAX_FILES),
            ("max_bytes", MIN_TOTAL, MAX_TOTAL),
        ):
            configuration(type(entry[name]) is int and low <= entry[name] <= high)
        result.append(dict(entry))
    configuration(len({item["path"] for item in result}) == len(result))
    return result


def require_registration(registered, directory):
    """One registered directory entry; anything else is refused before a path is resolved."""
    text(directory, 1024)
    for entry in registered:
        if entry["path"] == directory:
            return entry
    raise Fault("directory_unregistered", 403)


def require_schema(db):
    row = db.execute(
        "SELECT value FROM metadata WHERE key='knowledge_directories_schema'"
    ).fetchone()
    require(row is not None and row[0] == VERSION, "dependency_unavailable", 503)


def indexed_documents(db, project_id):
    """Every file document of one project with its current version, state and stored hash.

    Tombstoned rows are included: a tombstoned path that reappears is previewed as a normal
    re-import at its current version, exactly like the single-file operation. Only this
    project's own rows are read.
    """
    rows = db.execute(
        "SELECT d.id,d.source_id,d.locator,d.version,d.state,v.hash FROM knowledge_documents d "
        "LEFT JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.version "
        "WHERE d.project_id=? AND d.kind='file'",
        (project_id,),
    ).fetchall()
    return [
        {
            "document_id": row["id"],
            "source_id": row["source_id"],
            "locator": row["locator"],
            "version": row["version"],
            "state": row["state"],
            "hash": row["hash"],
        }
        for row in rows
    ]


def versions(rows):
    """The current version identity of every captured document, for the final re-check."""
    return {row["document_id"]: [row["version"], row["state"], row["hash"]] for row in rows}


def under_directory(locator, directory):
    """Whether a stored relative locator names a path inside one registered directory.

    Compares path components, so it also answers for a locator whose file no longer resolves,
    whatever separator the stored locator used.
    """
    parts, prefix = Path(locator).parts, Path(directory).parts
    return len(parts) > len(prefix) and all(
        left.casefold() == right.casefold() for left, right in zip(parts, prefix)
    )


def scan(project, project_id, entry, indexed):
    """Walk one registered directory and pin every candidate, outside any transaction.

    Files are read twice and their decoded text validated, so the preview only offers what the
    existing importer accepts. Refused entries keep their path and reason, unchanged indexed
    documents stay listed without consuming the byte or file budget, and both budgets bound
    only the work an apply would actually write.
    """
    base = directory_path(project, entry["path"])
    paths, walk = walk_directory(project, entry["path"])
    resolved, members = {}, []
    for row in indexed:
        try:
            target = file_path(project, row["locator"])
        except (Fault, OSError, ValueError):
            target = None
        if target is None:
            if under_directory(row["locator"], entry["path"]):
                members.append((row, None))
            continue
        if not target.is_relative_to(base):
            continue
        resolved[target] = row
        members.append((row, target))

    seen, omitted, ready, order, listed = set(), [], [], {}, set()
    for parts in paths:
        locator = "/".join(parts)
        try:
            target = file_path(project, locator)
        except Fault as error:
            omitted.append({"locator": locator, "reason": reason_of(error), "size": None})
            continue
        except (OSError, ValueError):
            omitted.append({"locator": locator, "reason": "unreadable", "size": None})
            continue
        seen.add(target)
        row = resolved.get(target)
        if row is not None:
            locator = row["locator"]
        if locator in listed:
            continue
        try:
            first = read_file(project, locator)
            second = read_file(project, locator)
        except Fault as error:
            omitted.append({"locator": locator, "reason": reason_of(error), "size": None})
            continue
        except (OSError, ValueError):
            omitted.append({"locator": locator, "reason": "unreadable", "size": None})
            continue
        raw, media = first
        digest = content_hash(raw)
        if second[1] != media or content_hash(second[0]) != digest:
            omitted.append({"locator": locator, "reason": "source_changed", "size": len(raw)})
            continue
        try:
            decode(raw, media)
        except Fault as error:
            omitted.append({"locator": locator, "reason": reason_of(error), "size": len(raw)})
            continue
        source = row["source_id"] if row is not None else source_id(project_id, locator)
        listed.add(locator)
        order[locator] = len(order)
        ready.append(
            {
                "locator": locator,
                "kind": "file",
                "media_type": media,
                "size": len(raw),
                "digest": digest,
                "source_id": source,
                "document_id": row["document_id"] if row is not None else document_id(source),
                "version": row["version"] if row is not None else 0,
                "action": (
                    "unchanged"
                    if row is not None and row["state"] == "ready" and row["hash"] == digest
                    else "import"
                ),
            }
        )

    chosen, total, budget_incomplete = [], 0, False
    for item in [candidate for candidate in ready if candidate["action"] == "import"]:
        if len(chosen) >= min(entry["max_files"], LIST_LIMIT):
            omitted.append(
                {"locator": item["locator"], "reason": "file_budget", "size": item["size"]}
            )
            budget_incomplete = True
        elif total + item["size"] > entry["max_bytes"]:
            omitted.append(
                {"locator": item["locator"], "reason": "byte_budget", "size": item["size"]}
            )
            budget_incomplete = True
        else:
            chosen.append(item)
            total += item["size"]
    unchanged_unlisted = 0
    for item in [candidate for candidate in ready if candidate["action"] == "unchanged"]:
        if len(chosen) < LIST_LIMIT:
            chosen.append(item)
        else:
            unchanged_unlisted += 1
    chosen.sort(key=lambda item: order[item["locator"]])

    # An incomplete walk cannot prove that an unseen path was removed, so it proposes nothing.
    candidates, missing_truncated = [], False
    if not walk["truncated"]:
        for row, target in members:
            if row["state"] == "deleted":
                continue
            if target is None:
                candidates.append({**row, "reason": "not_present"})
            elif target not in seen:
                candidates.append({**row, "reason": "not_scannable"})
    candidates.sort(key=lambda candidate: candidate["locator"])
    missing = [
        {key: candidate[key] for key in MISSING_FIELDS.split()}
        for candidate in candidates[:MISSING_LIMIT]
    ]
    missing_truncated = len(candidates) > MISSING_LIMIT
    counts = {
        "indexed": len(members),
        "listed": len(chosen),
        "import": len([item for item in chosen if item["action"] == "import"]),
        "unchanged": len([item for item in chosen if item["action"] == "unchanged"]),
        "unchanged_unlisted": unchanged_unlisted,
        "omitted": len(omitted),
        "omitted_unlisted": max(0, len(omitted) - OMIT_LIMIT),
        "missing": len(candidates),
        "links": walk["links"],
        "denied_directories": walk["denied"],
    }
    return {
        "items": chosen,
        "omitted": omitted[:OMIT_LIMIT],
        "missing": missing,
        "counts": counts,
        "walk_truncated": walk["truncated"],
        "budget_incomplete": budget_incomplete,
        "missing_truncated": missing_truncated,
    }


def plan(project_id, client, entry, project, revision, scanned):
    """The preview body the operator confirms, with an id that covers every field."""
    body = {
        "project_id": project_id,
        "client": client,
        "directory": entry["path"],
        "registration": fingerprint(project),
        "revision": revision or 0,
        "issued_at": utc(now()),
        "limits": {
            "max_files": entry["max_files"],
            "max_bytes": entry["max_bytes"],
            "file_bytes": MAX_BYTES,
        },
        "items": scanned["items"],
        "omitted": scanned["omitted"],
        "missing": scanned["missing"],
        "counts": scanned["counts"],
        "walk_truncated": scanned["walk_truncated"],
        "budget_incomplete": scanned["budget_incomplete"],
        "missing_truncated": scanned["missing_truncated"],
        "deletions": "explicit_approval_only",
        "scan": "registered_directory_only",
    }
    return {**body, "plan_id": plan_fingerprint(body)}


def plan_fingerprint(body):
    return fingerprint({key: value for key, value in body.items() if key != "plan_id"})


def plan_shape(plan):
    """Validate one submitted preview before anything is read or written.

    The shape is fully pinned, every locator must stay inside the previewed directory, and the
    recorded limits may not exceed the registered ones, so an edited or hand-made plan cannot
    widen the work an apply performs.
    """
    exact(plan, PLAN_FIELDS)
    for field in ("plan_id", "project_id", "client", "directory", "issued_at"):
        text(plan[field], 1024)
    require(
        isinstance(plan["registration"], str)
        and re.fullmatch(r"[0-9a-f]{64}", plan["registration"]) is not None,
        "invalid_plan",
        400,
    )
    integer(plan["revision"])
    exact(plan["limits"], "max_files max_bytes file_bytes")
    integer(plan["limits"]["max_files"], MIN_FILES, MAX_FILES)
    integer(plan["limits"]["max_bytes"], MIN_TOTAL, MAX_TOTAL)
    require(plan["limits"]["file_bytes"] == MAX_BYTES, "invalid_plan", 400)
    require(
        plan["deletions"] == "explicit_approval_only"
        and plan["scan"] == "registered_directory_only",
        "invalid_plan",
        400,
    )
    for field in ("walk_truncated", "budget_incomplete", "missing_truncated"):
        require(type(plan[field]) is bool, "invalid_plan", 400)
    require(
        isinstance(plan["counts"], dict)
        and set(plan["counts"]) == set(COUNT_KEYS)
        and all(type(value) is int and value >= 0 for value in plan["counts"].values()),
        "invalid_plan",
        400,
    )
    require(
        isinstance(plan["items"], list) and len(plan["items"]) <= LIST_LIMIT, "invalid_plan", 400
    )
    for item in plan["items"]:
        exact(item, ITEM_FIELDS)
        text(item["locator"], 2048)
        require(not Path(item["locator"]).is_absolute(), "invalid_plan", 400)
        require(under_directory(item["locator"], plan["directory"]), "invalid_plan", 400)
        require(item["kind"] == "file", "invalid_plan", 400)
        require(item["media_type"] in {"text/plain", "text/html"}, "invalid_plan", 400)
        integer(item["size"], 1, MAX_BYTES)
        require(
            isinstance(item["digest"], str)
            and re.fullmatch(r"[0-9a-f]{64}", item["digest"]) is not None,
            "invalid_plan",
            400,
        )
        text(item["source_id"], 256)
        text(item["document_id"], 256)
        require(item["document_id"] == document_id(item["source_id"]), "invalid_plan", 400)
        require(
            item["source_id"] == source_id(plan["project_id"], item["locator"]),
            "invalid_plan",
            400,
        )
        integer(item["version"])
        require(item["action"] in {"import", "unchanged"}, "invalid_plan", 400)
    require(
        isinstance(plan["omitted"], list) and len(plan["omitted"]) <= OMIT_LIMIT,
        "invalid_plan",
        400,
    )
    for entry in plan["omitted"]:
        exact(entry, OMITTED_FIELDS)
        text(entry["locator"], 2048)
        require(entry["reason"] in REASONS, "invalid_plan", 400)
        require(
            entry["size"] is None
            or (type(entry["size"]) is int and 0 <= entry["size"] <= MAX_BYTES),
            "invalid_plan",
            400,
        )
    require(
        isinstance(plan["missing"], list) and len(plan["missing"]) <= MISSING_LIMIT,
        "invalid_plan",
        400,
    )
    for candidate in plan["missing"]:
        exact(candidate, MISSING_FIELDS)
        text(candidate["locator"], 2048)
        require(under_directory(candidate["locator"], plan["directory"]), "invalid_plan", 400)
        text(candidate["document_id"], 256)
        text(candidate["source_id"], 256)
        require(
            candidate["document_id"] == document_id(candidate["source_id"]), "invalid_plan", 400
        )
        require(
            candidate["source_id"] == source_id(plan["project_id"], candidate["locator"]),
            "invalid_plan",
            400,
        )
        integer(candidate["version"])
        require(candidate["reason"] in PRESENCE_REASONS, "invalid_plan", 400)
    return plan


def tombstone_ids(values, plan):
    """The explicitly approved disappearance candidates, checked against the preview."""
    require(isinstance(values, list) and len(values) <= MAX_TOMBSTONES, "invalid_input", 400)
    candidates = {candidate["document_id"] for candidate in plan["missing"]}
    for value in values:
        text(value, 256)
        require(value in candidates, "unapproved_tombstone", 400)
    require(len(set(values)) == len(values), "invalid_input", 400)
    return set(values)


def store_plan(db, plan):
    """Record the preview this service issued, replacing the earlier preview of its scope."""
    db.execute(
        "DELETE FROM knowledge_plans WHERE client=? AND project_id=? AND directory=?",
        (plan["client"], plan["project_id"], plan["directory"]),
    )
    db.execute(
        "INSERT INTO knowledge_plans VALUES (?,?,?,?,?,?,?)",
        (
            plan["plan_id"],
            plan["client"],
            plan["project_id"],
            plan["directory"],
            canonical(plan),
            plan["revision"],
            plan["issued_at"],
        ),
    )


def stored_plan(db, client, project_id, directory):
    """The one preview currently issued for this client, project and directory."""
    return db.execute(
        "SELECT * FROM knowledge_plans WHERE client=? AND project_id=? AND directory=?",
        (client, project_id, directory),
    ).fetchone()


def observe(project, entry, plan):
    """Re-read every planned path outside any transaction. Nothing is written here.

    An item whose file moved, changed size or changed bytes is a conflict for that item only;
    the previewed digest is never written under an old plan, and a planned path that vanished
    is reported instead of being treated as a removal.
    """
    return {
        "items": {item["locator"]: _prepare(project, entry, item) for item in plan["items"]},
        "missing": {
            candidate["document_id"]: presence(project, entry["path"], candidate["locator"])
            for candidate in plan["missing"]
        },
    }


def _prepare(project, entry, item):
    state = presence(project, entry["path"], item["locator"])
    if state != "present":
        return {"reason": PRESENCE_REASONS[state]}
    try:
        first = read_file(project, item["locator"])
        second = read_file(project, item["locator"])
    except Fault as error:
        return {"reason": reason_of(error)}
    except (OSError, ValueError):
        return {"reason": "unreadable"}
    raw, media = first
    digest = content_hash(raw)
    if second[1] != media or content_hash(second[0]) != digest:
        return {"reason": "source_changed"}
    if len(raw) != item["size"] or digest != item["digest"]:
        return {"reason": "source_changed"}
    try:
        decoded = decode(raw, media)
    except Fault as error:
        return {"reason": reason_of(error)}
    from .knowledge import blocks

    units = blocks(decoded, None)
    return {
        "prepared": {
            "raw": raw,
            "media": media,
            "resolved": item["locator"],
            "text": decoded,
            "units": units,
            "digest": item["digest"],
            "index": [" ".join(terms(unit["text"])) for unit in units],
        }
    }


def import_item(db, application, project_id, project, item, observed):
    """Write one re-verified item inside its own transaction.

    A conflict is reported for that item only, so a partial apply stays honest and resumable
    instead of ever importing bytes the confirmed preview did not show.
    """

    def outcome(name, version=None, digest=None):
        result = {
            "locator": item["locator"],
            "document_id": item["document_id"],
            "outcome": name,
        }
        if version is not None:
            result["version"] = version
        if digest is not None:
            result["hash"] = digest
        return result

    seen = observed["items"].get(item["locator"], {"reason": "unreadable"})
    if "reason" in seen:
        return {**outcome("conflict"), "reason": seen["reason"]}
    row = db.execute(
        "SELECT * FROM knowledge_documents WHERE id=?", (item["document_id"],)
    ).fetchone()
    if row is not None and row["project_id"] != project_id:
        return {**outcome("conflict"), "reason": "foreign_document"}
    if row is None:
        if item["version"] != 0:
            return {**outcome("conflict"), "reason": "version_conflict"}
        version, stored = 0, None
    else:
        version = row["version"]
        found = db.execute(
            "SELECT hash FROM knowledge_versions WHERE document_id=? AND version=?",
            (row["id"], version),
        ).fetchone()
        stored = found["hash"] if found is not None else None
        if version > item["version"] and row["state"] == "ready" and stored == item["digest"]:
            # The same bytes are already current: a retried or partially applied plan.
            return outcome("unchanged", version, stored)
        if version != item["version"]:
            return {**outcome("conflict"), "reason": "version_conflict"}
        if row["state"] == "ready" and stored == item["digest"]:
            return outcome("unchanged", version, stored)
    application._ensure_project(db, project_id, project)
    application._store_version(
        db,
        project_id,
        item["document_id"],
        item["source_id"],
        "file",
        item["locator"],
        seen["prepared"],
        row,
        version + 1,
    )
    return outcome("imported", version + 1, item["digest"])


def remove_source(db, application, project_id, candidate, observed):
    """Tombstone one explicitly approved disappeared source inside its own transaction.

    Only index rows change: this service never writes, moves or deletes a project file, and a
    path that came back or that moved on refuses the tombstone instead of recording it.
    """
    if observed["missing"].get(candidate["document_id"]) == "present":
        return {**candidate, "outcome": "present_again"}
    row = db.execute(
        "SELECT * FROM knowledge_documents WHERE id=?", (candidate["document_id"],)
    ).fetchone()
    if row is None or row["project_id"] != project_id:
        return {**candidate, "outcome": "unknown_document"}
    if row["state"] == "deleted":
        return {**candidate, "outcome": "already_deleted"}
    if row["version"] != candidate["version"]:
        return {**candidate, "outcome": "version_conflict"}
    db.execute(
        "UPDATE knowledge_documents SET state='deleted',version=version+1 WHERE id=?", (row["id"],)
    )
    db.execute(
        "DELETE FROM knowledge_index WHERE block_id IN "
        "(SELECT id FROM knowledge_blocks WHERE document_id=?)",
        (row["id"],),
    )
    application._bump(db, project_id)
    return {**candidate, "outcome": "deleted", "version": row["version"] + 1}


def pending_result(project_id, entry, plan):
    """The honest state of a claimed apply before any item has been confirmed.

    A key that was bound but never finished is not a success: `status` reports it as
    `in_progress`, and only the identical request may resume it under that key.
    """
    return {
        "status": "in_progress",
        "project_id": project_id,
        "directory": entry["path"],
        "plan_id": plan["plan_id"],
        "authority": "explicit_directory_confirmation",
    }


def applied_result(project_id, entry, plan, items, conflicts, tombstones, revision):
    """The persistent, resumable outcome of one confirmed apply."""
    written = len([item for item in items if item["outcome"] == "imported"])
    untouched = len([item for item in items if item["outcome"] == "unchanged"])
    deleted = len([item for item in tombstones if item["outcome"] == "deleted"])
    status = (
        "applied" if not conflicts else "partial" if written + untouched + deleted else "failed"
    )
    remaining = sorted(
        {conflict["locator"] for conflict in conflicts}
        | {
            candidate["locator"]
            for candidate in tombstones
            if candidate["outcome"] in {"present_again", "version_conflict", "unknown_document"}
        }
    )
    return {
        "status": status,
        "project_id": project_id,
        "directory": entry["path"],
        "plan_id": plan["plan_id"],
        "revision": revision,
        "items": items,
        "conflicts": conflicts,
        "tombstones": tombstones,
        "counts": {
            "imported": written,
            "unchanged": untouched,
            "conflicts": len(conflicts),
            "tombstoned": deleted,
            "omitted": plan["counts"]["omitted"],
            "not_approved": len([item for item in tombstones if item["outcome"] == "not_approved"]),
        },
        "remaining": remaining,
        "rescan": bool(remaining),
        "deletions": "explicit_approval_only",
        "authority": "explicit_directory_confirmation",
    }
