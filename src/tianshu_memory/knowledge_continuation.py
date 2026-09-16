"""Project continuation packages: current checkout facts with a trustworthy project state.

Three things must never be conflated, so the package keeps them apart: what the repository
history says, what this service has indexed, and what the registered working directory
currently contains. Only the last one is observed live. An indexed digest never overrides the
current file, a declared verification whose commit is not the current HEAD of its checkout is
presented as historical instead of current, and a package stays reusable only while the
checkout it was issued for still reads the same.
"""

import hmac
import json
import re

from . import knowledge_workdir as workdir
from .domain import Fault, canonical, fingerprint, now, require, utc
from .knowledge_directories import indexed_documents, versions

MIN_BUDGET, MAX_BUDGET = 4096, 32768
MAX_PACKAGE_BYTES = 262144
LIST_LIMIT = 64
UNIT_SLACK = 160
# A verification is only current when it names this checkout, records the commit that is still
# HEAD and was declared against the very uncommitted state that is still there: the stamped
# fingerprint of that state has to match. A record whose state cannot be proven (an older record
# without a fingerprint, or a working tree too large to describe completely) is `workdir_unproven`
# rather than `current`, and any observed change is `workdir_changed`. Nothing else may pass.
SCOPES = (
    "current",
    "historical_commit",
    "workdir_changed",
    "workdir_unproven",
    "other_worktree",
    "unbound",
)
INDEX_FRESHNESS = ("verified_current", "verified_changed", "unverified")
DIFFERENCES = (
    "stale_or_tampered",
    "registration_changed",
    "revision_changed",
    "index_changed",
    "branch_changed",
    "head_changed",
    "workdir_dirty",
    "workdir_unproven",
    "file_changed",
    "stale_evidence",
)
# A check answers with a verdict when a checkout cannot be observed at all, instead of failing
# with an error that says nothing about the package.
UNOBSERVABLE = (
    "git_unavailable",
    "workdir_unavailable",
    "workdir_not_repository",
    "workdir_not_root",
    "workdir_timeout",
    "workdir_output_too_large",
    "workdir_changed",
    "workdir_git_failed",
    "workdir_status_failed",
    "workdir_status_invalid",
    "workdir_file_refused",
    "workdir_file_unsupported",
    "workdir_file_changed",
    "workdir_file_too_large",
    "workdir_byte_budget",
    "workdir_helpers_unbounded",
)
PACKAGE_FIELDS = (
    "status project_id worktree history index state units omissions budget revision "
    "registration authority retrieval trust seal"
)
WORKTREE_FIELDS = (
    "id expected_branch branch detached branch_matches head unborn dirty counts changes files git "
    "collected_at registration"
)
GIT_FIELDS = "version timeout_seconds history_limit status_limit_bytes subcommands helpers writes"
HISTORY_FIELDS = "limit listed complete commits"
HISTORY_ENTRY_FIELDS = "commit subject committed_at"
INDEX_FIELDS = "documents total listed truncated"
INDEX_DOCUMENT_FIELDS = "locator kind document_id version state stored_hash freshness"
FILE_FIELDS = "locator state size digest index freshness"
FILE_INDEX_FIELDS = "document_id version state hash"
STATE_FIELDS = (
    "version authority current stale_evidence goal constraints unfinished recent_verification "
    "pitfalls evidence"
)
VERIFICATION_FIELDS = "summary worktree commit branch dirty declared_at facts"
SCOPED_FIELDS = VERIFICATION_FIELDS + " scope"
PITFALL_FIELDS = "trigger symptom cause correction verification evidence"
REFERENCE_FIELDS = "block_id document_id version hash"
BUDGET_FIELDS = "limit_bytes used_bytes unit over_budget tokenizer token_counts unit_integrity"
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ABSENT = {"missing", "unreadable", "changed", "too_large"}


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields.split()), "invalid_input", 400)


def text(value, maximum=2048):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)


def optional_text(value, maximum=2048):
    require(
        value is None or (isinstance(value, str) and 0 < len(value) <= maximum),
        "invalid_input",
        400,
    )


def integer(value, minimum=0, maximum=2**31):
    require(type(value) is int and minimum <= value <= maximum, "invalid_input", 400)


def digest(value, code="invalid_input"):
    require(isinstance(value, str) and DIGEST.match(value) is not None, code, 400)


def commit(value):
    require(
        value is None or (isinstance(value, str) and COMMIT.match(value) is not None),
        "invalid_input",
        400,
    )


def reference(value):
    exact(value, REFERENCE_FIELDS)
    text(value["block_id"], 256)
    text(value["document_id"], 256)
    integer(value["version"])
    digest(value["hash"])


def revision(db, project_id):
    row = db.execute("SELECT revision FROM knowledge_projects WHERE id=?", (project_id,)).fetchone()
    return row["revision"] if row else 0


def references(payload):
    """Every evidence reference one declared state carries, deduplicated and ordered."""
    found = list(payload["evidence"])
    for pitfall in payload.get("pitfalls", []):
        found.extend(pitfall["evidence"])
    result, seen = [], set()
    for item in found:
        key = canonical(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def facts_fingerprint(facts):
    """The observable identity of one checkout observation: everything a binding depends on.

    The commit, branch, dirtiness, classified counts, the bounded fingerprint of the changed
    entries and every registered digest are hashed together. A verification stamped with this
    value is current only while a later observation of the same checkout produces it again, so a
    second edit that leaves the counts and HEAD unchanged still invalidates it. Only facts of the
    checkout itself take part: how fresh a digest looks next to the index is a property of the
    index, not of the checkout, and must not make two observations of one unchanged checkout
    differ.
    """
    return fingerprint(
        {
            "id": facts["id"],
            "head": facts["head"],
            "unborn": facts["unborn"],
            "branch": facts["branch"],
            "detached": facts["detached"],
            "dirty": facts["dirty"],
            "counts": facts["counts"],
            "changes": facts["changes"],
            "files": {
                fact["locator"]: [fact["state"], fact["size"], fact["digest"]]
                for fact in facts["files"]
            },
        }
    )


def scoped(payload, facts):
    """One declared verification, labeled against the live facts of the target checkout.

    A declared string that names no commit stays `unbound` forever: a historical test result is
    never presented as a test of the commit in front of the agent. A bound record is `current`
    only while the checkout still reads the same: the recorded commit is still HEAD *and* the
    stamped fingerprint of the uncommitted state still matches. A state that cannot be proven
    equal (a record written before fingerprints existed for a dirty checkout, or a working tree
    with more changed entries than one observation may describe) is `workdir_unproven`, and an
    observed difference is `workdir_changed`; neither is ever `current`.
    """
    if isinstance(payload, str):
        result = dict.fromkeys(VERIFICATION_FIELDS.split())
        result["summary"] = payload
    else:
        result = {field: payload.get(field) for field in VERIFICATION_FIELDS.split()}
    if result["worktree"] is None or result["commit"] is None:
        # A sentence that names no checkout and no commit was never bound to a run of code.
        result["scope"] = "unbound"
    elif result["worktree"] != facts["id"]:
        result["scope"] = "other_worktree"
    elif result["commit"] != facts["head"]:
        result["scope"] = "historical_commit"
    elif result["facts"] is None:
        # Without a stamped fingerprint only a checkout with nothing uncommitted is provable:
        # its content is exactly the recorded commit.
        result["scope"] = "current" if not facts["dirty"] else "workdir_unproven"
    elif not facts["changes"]["complete"]:
        result["scope"] = "workdir_unproven"
    elif result["facts"] != facts_fingerprint(facts):
        result["scope"] = "workdir_changed"
    else:
        result["scope"] = "current"
    return result


def declared_state(application, db, project_id, project, read):
    """The operator-declared state with every reference re-verified against current sources."""
    row = db.execute("SELECT * FROM knowledge_states WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        return None, [], []
    payload = json.loads(row["payload"])
    stale, evidence = [], []
    for item in references(payload):
        try:
            evidence.append(application._reference(db, project_id, project, item, read))
        except Fault:
            stale.append(item)
    return {"version": row["version"], **payload}, stale, evidence


def recover(
    application, db, project_id, project, args, seal_key, worktrees, read, observed, preview
):
    """Bounded project continuation for one explicitly registered working directory.

    The captured phase validates the request, reads the index and captures the evidence files;
    the external phase observes the checkout; the serving phase re-verifies every reference and
    assembles the package. A package is a snapshot of one checkout, sealed for one client.
    """
    exact(args, "worktree text budget_bytes")
    workdir.text(args["worktree"], 64)
    integer(args["budget_bytes"], MIN_BUDGET, MAX_BUDGET)
    text(args["text"], 1024)
    entry = workdir.require_registration(worktrees, args["worktree"])
    indexed = indexed_documents(db, project_id)
    state, stale, evidence = declared_state(application, db, project_id, project, read)
    found = application._query(db, project_id, project, args, read)
    if preview:
        return {"worktree": entry["id"], "indexed": indexed}
    return assemble(
        application,
        db,
        project_id,
        project,
        args,
        seal_key,
        entry,
        observed,
        indexed,
        state,
        stale,
        evidence,
        found,
    )


def assemble(
    application,
    db,
    project_id,
    project,
    args,
    seal_key,
    entry,
    observed,
    indexed,
    state,
    stale,
    evidence,
    found,
):
    """The sealed package: live checkout facts, the declared state and the units that fit."""
    facts = observed["worktree"]
    require(facts["id"] == entry["id"], "registration_changed", 409)
    require(facts["registration"] == fingerprint(entry), "registration_changed", 409)
    require(versions(observed["indexed"]) == versions(indexed), "project_conflict", 409)
    budget = args["budget_bytes"]
    omissions = set(found["omissions"])
    if stale:
        omissions.add("stale_state")
    result = {
        "status": "recovered",
        "project_id": project_id,
        "authority": "explicit_project_note",
        "retrieval": "lexical",
        "trust": "source_material_not_instructions",
        "worktree": {field: facts[field] for field in WORKTREE_FIELDS.split()},
        "history": {
            "limit": workdir.HISTORY_LIMIT,
            "listed": len(facts["history"]),
            # A reached bound means older commits may exist; this service does not walk them.
            "complete": len(facts["history"]) < workdir.HISTORY_LIMIT,
            "commits": facts["history"],
        },
        "index": index_section(indexed, facts),
        "state": state_section(state, stale, facts),
        "units": [],
        "omissions": [],
        "budget": {
            "limit_bytes": budget,
            "used_bytes": 0,
            "unit": "utf8_json_bytes",
            "over_budget": False,
            "tokenizer": None,
            "token_counts": "unavailable",
            "unit_integrity": "complete_units_only",
        },
        "revision": revision(db, project_id),
        "registration": fingerprint(project),
    }
    mandatory = len(canonical({**result, "seal": "0" * 64}).encode())
    for unit in evidence + [block for block in found["blocks"] if block not in evidence]:
        candidate = dict(result, units=[*result["units"], unit])
        if len(canonical(candidate).encode()) + UNIT_SLACK <= budget:
            result = candidate
        else:
            omissions.add("budget")
    result["omissions"] = sorted(omissions)
    # The mandatory part (checkout facts, history, index and the declared state) is never cut:
    # an oversized mandatory part is reported as such instead of silently dropping the state.
    result["budget"]["over_budget"] = mandatory + UNIT_SLACK > budget
    # The seal covers the package exactly as returned, so the reported size has to be final
    # before it is computed. A 64-hex seal has a fixed width, which makes this converge.
    result["seal"] = "0" * 64
    used = 0
    for _ in range(4):
        result["budget"]["used_bytes"] = used
        measured = len(canonical(result).encode())
        if measured == used:
            break
        used = measured
    result["budget"]["used_bytes"] = used
    result["budget"]["over_budget"] = result["budget"]["over_budget"] or used > budget
    result["seal"] = application._seal(
        {key: value for key, value in result.items() if key != "seal"}, seal_key
    )
    return result


def index_section(indexed, facts):
    """What this service has indexed, next to what the checkout currently contains.

    An unregistered indexed document is listed as `unverified`: only the registered digests and
    the units actually returned are re-read, so no stale hash can be presented as a current file.
    """
    verified = {fact["locator"]: fact for fact in facts["files"]}
    documents = []
    for row in indexed[:LIST_LIMIT]:
        fact = verified.get(row["locator"])
        documents.append(
            {
                "locator": row["locator"],
                "kind": "file",
                "document_id": row["document_id"],
                "version": row["version"],
                "state": row["state"],
                "stored_hash": row["hash"],
                "freshness": (
                    "unverified"
                    if fact is None or fact["state"] in ABSENT
                    else "verified_current"
                    if fact["freshness"] == "current"
                    else "verified_changed"
                ),
            }
        )
    return {
        "documents": documents,
        "total": len(indexed),
        "listed": len(documents),
        "truncated": len(indexed) > LIST_LIMIT,
    }


def state_section(state, stale, facts):
    """The declared goal, constraints, verifications, next steps and pitfalls, or nothing."""
    if state is None:
        return None
    return {
        "version": state["version"],
        "authority": "explicit_project_note",
        "current": not stale,
        "stale_evidence": stale,
        "goal": state["goal"],
        "constraints": state["constraints"],
        "unfinished": state["unfinished"],
        "recent_verification": [scoped(item, facts) for item in state["recent_verification"]],
        "pitfalls": state["pitfalls"],
        "evidence": state["evidence"],
    }


def check(application, db, project_id, project, args, seal_key, worktrees, read, observed, preview):
    """Validate an issued continuation package against the checkout it names, right now."""
    exact(args, "package")
    package = args["package"]
    require(
        isinstance(package, dict) and len(canonical(package).encode()) <= MAX_PACKAGE_BYTES,
        "invalid_input",
        400,
    )
    shape(package)
    entry = workdir.require_registration(worktrees, package["worktree"]["id"])
    internal = internal_differences(
        application, db, project_id, project, package, seal_key, entry, read
    )
    indexed = indexed_documents(db, project_id)
    if preview:
        return {
            "worktree": entry["id"],
            "indexed": indexed,
            "settled": verdict(package, project_id, internal, observed=False) if internal else None,
        }
    if observed.get("settled") is not None:
        return observed["settled"]
    differences = internal + live_differences(package, observed, indexed)
    return verdict(package, project_id, differences, observed=True)


def verdict(package, project_id, differences, observed):
    """One explicit answer: current, or the concrete reasons it is not, in fixed priority.

    The order is the order of `DIFFERENCES`, so a caller always sees tampering before drift and
    drift before evidence, whatever order the checks happened to run in.
    """
    ordered = sorted(set(differences), key=DIFFERENCES.index)
    return {
        "valid": not ordered,
        "reason": ordered[0] if ordered else "current",
        "differences": ordered,
        "observed": observed,
        "project_id": project_id,
        "worktree": {"id": package["worktree"]["id"]},
        "checked_at": utc(now()),
    }


def unavailable(project_id, worktree, code):
    """A checkout that cannot be observed at all is a verdict, not a silent success."""
    return {
        "valid": False,
        "reason": code,
        "differences": [],
        "observed": False,
        "project_id": project_id,
        "worktree": {"id": worktree},
        "checked_at": utc(now()),
    }


def internal_differences(application, db, project_id, project, package, seal_key, entry, read):
    """Identity, registration, revision and evidence of one package, in that order."""
    body = {key: value for key, value in package.items() if key != "seal"}
    if not hmac.compare_digest(package["seal"], application._seal(body, seal_key)):
        return ["stale_or_tampered"]
    if package["project_id"] != project_id or package["registration"] != fingerprint(project):
        return ["stale_or_tampered"]
    if package["worktree"]["registration"] != fingerprint(entry):
        return ["registration_changed"]
    differences = []
    if package["revision"] != revision(db, project_id):
        differences.append("revision_changed")
    if package["state"] is not None:
        for item in package["state"]["evidence"]:
            try:
                application._reference(db, project_id, project, item, read)
            except Fault:
                differences.append("stale_evidence")
                break
    return differences


def live_differences(package, observed, indexed):
    """What the checkout, its registered digests and the index read now, next to the package."""
    facts, stored = observed["worktree"], package["worktree"]
    differences = []
    if facts["branch"] != stored["branch"] or facts["detached"] != stored["detached"]:
        differences.append("branch_changed")
    if facts["head"] != stored["head"] or facts["unborn"] != stored["unborn"]:
        differences.append("head_changed")
    if facts["dirty"] != stored["dirty"] or facts["counts"] != stored["counts"]:
        differences.append("workdir_dirty")
    elif not stored["changes"]["complete"] or facts["changes"] != stored["changes"]:
        # The counts and the dirty flag are unchanged, but the uncommitted state is not the one
        # the package describes: a tracked file that is not on the registered digest list was
        # edited again, or the package was issued with a description that stopped at the
        # changed-entry cap and never described the whole checkout. Either way the honest answer
        # is "cannot be proven equal", never "still valid".
        differences.append("workdir_unproven")
    if file_map(facts["files"]) != file_map(stored["files"]):
        differences.append("file_changed")
    if versions(observed["indexed"]) != versions(indexed):
        differences.append("index_changed")
    elif index_changed(package["index"], indexed):
        differences.append("index_changed")
    return differences


def file_map(facts):
    """The comparable identity of every registered digest, without its collection timestamp."""
    return {
        fact["locator"]: [fact["state"], fact["size"], fact["digest"], fact["freshness"]]
        for fact in facts
    }


def index_changed(stored, indexed):
    """Whether the recorded index section still matches the current index rows."""
    if stored["total"] != len(indexed) or stored["truncated"] != (len(indexed) > LIST_LIMIT):
        return True
    rows = {row["document_id"]: row for row in indexed}
    for document in stored["documents"]:
        row = rows.get(document["document_id"])
        if row is None or [
            row["locator"],
            row["version"],
            row["state"],
            row["hash"],
        ] != [
            document["locator"],
            document["version"],
            document["state"],
            document["stored_hash"],
        ]:
            return True
    return False


def shape(package):
    """Validate one submitted package before anything about it is believed."""
    exact(package, PACKAGE_FIELDS)
    require(package["status"] == "recovered", "invalid_input", 400)
    text(package["project_id"], 128)
    text(package["authority"], 64)
    text(package["retrieval"], 64)
    text(package["trust"], 128)
    integer(package["revision"])
    digest(package["registration"])
    text(package["seal"], 128)
    exact(package["worktree"], WORKTREE_FIELDS)
    worktree_shape(package["worktree"])
    history_shape(package["history"])
    index_shape(package["index"])
    exact(package["budget"], BUDGET_FIELDS)
    integer(package["budget"]["limit_bytes"], MIN_BUDGET, MAX_BUDGET)
    integer(package["budget"]["used_bytes"])
    require(
        package["budget"]["unit"] == "utf8_json_bytes"
        and package["budget"]["tokenizer"] is None
        and package["budget"]["token_counts"] == "unavailable"
        and package["budget"]["unit_integrity"] == "complete_units_only"
        and type(package["budget"]["over_budget"]) is bool,
        "invalid_input",
        400,
    )
    require(
        isinstance(package["units"], list) and len(package["units"]) <= 128,
        "invalid_input",
        400,
    )
    for unit in package["units"]:
        require(isinstance(unit, dict) and "reference" in unit, "invalid_input", 400)
        reference(unit["reference"])
    require(
        isinstance(package["omissions"], list)
        and len(package["omissions"]) <= 16
        and all(isinstance(item, str) and 0 < len(item) <= 64 for item in package["omissions"]),
        "invalid_input",
        400,
    )
    if package["state"] is not None:
        state_shape(package["state"])


def worktree_shape(facts):
    text(facts["id"], 64)
    text(facts["expected_branch"], 256)
    optional_text(facts["branch"], 256)
    optional_text(facts["collected_at"], 64)
    digest(facts["registration"])
    commit(facts["head"])
    for field in ("detached", "branch_matches", "unborn", "dirty"):
        require(type(facts[field]) is bool, "invalid_input", 400)
    exact(facts["counts"], " ".join(workdir.COUNT_KEYS))
    for value in facts["counts"].values():
        integer(value)
    exact(facts["changes"], workdir.CHANGE_FIELDS)
    require(
        facts["changes"]["mode"] == workdir.CHANGE_MODE
        and type(facts["changes"]["complete"]) is bool,
        "invalid_input",
        400,
    )
    integer(facts["changes"]["entries"])
    digest(facts["changes"]["fingerprint"])
    exact(facts["git"], GIT_FIELDS)
    text(facts["git"]["version"], workdir.VERSION_LIMIT)
    integer(facts["git"]["timeout_seconds"], 1, 120)
    integer(facts["git"]["history_limit"], 1, 64)
    integer(facts["git"]["status_limit_bytes"], 1024, 1024 * 1024)
    require(
        isinstance(facts["git"]["helpers"], list)
        and len(facts["git"]["helpers"]) <= workdir.MAX_HELPERS
        and all(isinstance(name, str) and 0 < len(name) <= 64 for name in facts["git"]["helpers"]),
        "invalid_input",
        400,
    )
    require(
        facts["git"]["subcommands"] == list(workdir.READ_ONLY)
        and facts["git"]["writes"] == "never",
        "invalid_input",
        400,
    )
    require(isinstance(facts["files"], list) and len(facts["files"]) <= workdir.MAX_FILES)
    for fact in facts["files"]:
        exact(fact, FILE_FIELDS)
        text(fact["locator"], workdir.PATH_LIMIT)
        require(fact["state"] in workdir.FILE_STATES and fact["freshness"] in workdir.FRESHNESS)
        require(
            fact["size"] is None or (type(fact["size"]) is int and 0 <= fact["size"] <= 2**31),
            "invalid_input",
            400,
        )
        if fact["digest"] is not None:
            digest(fact["digest"])
        if fact["index"] is not None:
            exact(fact["index"], FILE_INDEX_FIELDS)
            text(fact["index"]["document_id"], 256)
            integer(fact["index"]["version"])
            text(fact["index"]["state"], 32)
            digest(fact["index"]["hash"])


def history_shape(history):
    exact(history, HISTORY_FIELDS)
    integer(history["limit"], 1, 64)
    integer(history["listed"], 0, 64)
    require(type(history["complete"]) is bool, "invalid_input", 400)
    require(isinstance(history["commits"], list) and len(history["commits"]) <= 64)
    for entry in history["commits"]:
        exact(entry, HISTORY_ENTRY_FIELDS)
        commit(entry["commit"])
        require(isinstance(entry["subject"], str) and len(entry["subject"]) <= 512)
        text(entry["committed_at"], 64)


def index_shape(section):
    exact(section, INDEX_FIELDS)
    integer(section["total"])
    integer(section["listed"], 0, LIST_LIMIT)
    require(type(section["truncated"]) is bool, "invalid_input", 400)
    require(isinstance(section["documents"], list) and len(section["documents"]) <= LIST_LIMIT)
    for document in section["documents"]:
        exact(document, INDEX_DOCUMENT_FIELDS)
        text(document["locator"], workdir.PATH_LIMIT)
        require(document["kind"] == "file", "invalid_input", 400)
        text(document["document_id"], 256)
        integer(document["version"])
        text(document["state"], 32)
        digest(document["stored_hash"])
        require(document["freshness"] in INDEX_FRESHNESS, "invalid_input", 400)


def state_shape(state):
    exact(state, STATE_FIELDS)
    integer(state["version"], 1)
    require(state["authority"] == "explicit_project_note", "invalid_input", 400)
    require(type(state["current"]) is bool, "invalid_input", 400)
    text(state["goal"], 2000)
    for field in ("constraints", "unfinished"):
        require(isinstance(state[field], list) and len(state[field]) <= 16, "invalid_input", 400)
    require(
        isinstance(state["stale_evidence"], list) and len(state["stale_evidence"]) <= 32,
        "invalid_input",
        400,
    )
    for item in state["stale_evidence"]:
        reference(item)
    require(isinstance(state["evidence"], list) and 0 < len(state["evidence"]) <= 16)
    for item in state["evidence"]:
        reference(item)
    require(
        isinstance(state["recent_verification"], list) and len(state["recent_verification"]) <= 16,
        "invalid_input",
        400,
    )
    for item in state["recent_verification"]:
        exact(item, SCOPED_FIELDS)
        text(item["summary"], 2000)
        optional_text(item["worktree"], 64)
        commit(item["commit"])
        optional_text(item["branch"], 256)
        require(item["dirty"] is None or type(item["dirty"]) is bool, "invalid_input", 400)
        optional_text(item["declared_at"], 64)
        if item["facts"] is not None:
            digest(item["facts"])
        require(item["scope"] in SCOPES, "invalid_input", 400)
    require(isinstance(state["pitfalls"], list) and len(state["pitfalls"]) <= 8)
    for pitfall in state["pitfalls"]:
        exact(pitfall, PITFALL_FIELDS)
        for field in ("trigger", "symptom", "cause", "correction", "verification"):
            text(pitfall[field], 1000)
        require(isinstance(pitfall["evidence"], list) and 0 < len(pitfall["evidence"]) <= 16)
        for item in pitfall["evidence"]:
            reference(item)
