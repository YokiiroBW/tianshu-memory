"""Registered working directories: read-only Git and restricted file facts for continuation.

A continuation package must describe the checkout an agent is working in right now, and it
must never let an older indexed digest stand in for the current file. This is the only code
path that reads a registered working directory: a fixed, bounded set of read-only Git
commands (never fetch, checkout, reset, clean or diff; never a repository hook or a
repository-configured helper; never an untracked file's content) plus the digests of a small
operator-registered file list. Nothing here writes to the working directory, every read stays
inside the registered root, and the whole collection runs outside any database transaction, so
slow Git or disk I/O never holds the shared writer lock.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .domain import Fault, fingerprint, now, require, utc
from .knowledge_sources import DENIED, SUFFIXES, content_hash, read_file, refused_component

# The Git executable is operator environment, exactly like the private configuration file: it
# can be named explicitly for a host where Git is not on the service account's PATH.
GIT_ENVIRONMENT_VARIABLE = "TIANSHU_GIT"
MAX_WORKTREES = 16
MAX_CLIENTS = 16
MAX_FILES = 16
MIN_BYTES, MAX_BYTES = 1024, 8 * 1024 * 1024
PATH_LIMIT = 1024
HISTORY_LIMIT = 8
SUBJECT_LIMIT = 200
TIMEOUT_SECONDS = 10
STATUS_LIMIT = 262144
LOG_LIMIT = 65536
VERSION_LIMIT = 256
FIELDS = "id path branch clients files max_bytes"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# Every Git subcommand (and the one global flag) this service may run, and nothing else. A
# collection never mutates a repository, never contacts a remote and never runs repository code.
READ_ONLY = ("rev-parse", "symbolic-ref", "status", "log", "--version")
UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
COUNT_KEYS = ("staged", "modified", "deleted", "renamed", "untracked", "conflicted", "entries")
FILE_STATES = ("present", "missing", "unreadable", "changed", "too_large")
FRESHNESS = ("current", "changed", "unindexed")
HISTORY_FIELDS = "commit subject committed_at"


def text(value, maximum=2048):
    require(isinstance(value, str) and 0 < len(value) <= maximum, "invalid_input", 400)


def configuration(condition, code="invalid_configuration"):
    require(condition, code, 503)


def refused_root_component(name):
    """The part of the source name rule that also applies to a registered checkout root.

    A checkout may live under a hidden directory, so the leading-dot rule is not applied to the
    root itself; a credentials, secrets, token, runtime, model, build or dependency directory is
    never a working directory, at any depth.
    """
    folded = name.casefold()
    return folded in DENIED or any(word in folded for word in ("credential", "secret", "token"))


def registration(knowledge, project_id, client):
    """The working directories one client may continue in, validated but not resolved.

    An absent section means this project has no registered working directory, so nothing can be
    continued anywhere: a checkout never becomes readable by default and is never inferred from
    the project root. `clients` is part of the registration, so two coding agents can register
    different checkouts of one project and neither can name the other's.
    """
    section = knowledge.get("worktrees", {})
    configuration(isinstance(section, dict))
    entries = section.get(project_id, [])
    configuration(isinstance(entries, list) and len(entries) <= MAX_WORKTREES)
    result, seen = [], set()
    for entry in entries:
        configuration(isinstance(entry, dict) and set(entry) == set(FIELDS.split()))
        identifier = entry["id"]
        configuration(isinstance(identifier, str) and IDENTIFIER.match(identifier) is not None)
        configuration(identifier not in seen)
        seen.add(identifier)
        path = entry["path"]
        configuration(isinstance(path, str) and 0 < len(path) <= PATH_LIMIT)
        parts = Path(path).parts
        configuration(
            Path(path).is_absolute()
            and bool(parts)
            and all(part not in {".", ".."} for part in parts)
            and not any(refused_root_component(part) for part in parts)
        )
        branch = entry["branch"]
        configuration(
            isinstance(branch, str)
            and BRANCH.match(branch) is not None
            and ".." not in branch
            and not branch.endswith("/")
        )
        clients = entry["clients"]
        configuration(
            isinstance(clients, list)
            and 0 < len(clients) <= MAX_CLIENTS
            and len(set(clients)) == len(clients)
        )
        for name in clients:
            configuration(isinstance(name, str) and 0 < len(name) <= 128)
        paths = entry["files"]
        configuration(isinstance(paths, list) and 0 < len(paths) <= MAX_FILES)
        configuration(len(set(paths)) == len(paths))
        for locator in paths:
            configuration(isinstance(locator, str) and 0 < len(locator) <= PATH_LIMIT)
            relative = Path(locator)
            configuration(
                not relative.is_absolute()
                and not relative.drive
                and ":" not in locator
                and all(part not in {".", ".."} for part in relative.parts)
            )
            # Credential, runtime, hidden and unsupported sources are refused at registration
            # instead of being skipped silently at collection time.
            configuration(not any(refused_component(part) for part in relative.parts))
            configuration(relative.suffix.lower() in SUFFIXES)
        configuration(
            type(entry["max_bytes"]) is int and MIN_BYTES <= entry["max_bytes"] <= MAX_BYTES
        )
        if client in clients:
            result.append(
                {
                    "id": identifier,
                    "path": path,
                    "branch": branch,
                    "clients": list(clients),
                    "files": list(paths),
                    "max_bytes": entry["max_bytes"],
                }
            )
    return result


def require_registration(registered, worktree):
    """One registered working directory; anything else is refused before any path is read.

    A directory registered to another client is refused exactly like one that does not exist,
    so a refused caller cannot learn whether somebody else's checkout is registered here.
    """
    text(worktree, 64)
    for entry in registered:
        if entry["id"] == worktree:
            return entry
    raise Fault("worktree_unregistered", 403)


def git_executable():
    """The Git executable this host uses, or an explicit unavailable state."""
    candidate = os.environ.get(GIT_ENVIRONMENT_VARIABLE) or "git"
    resolved = shutil.which(candidate)
    require(bool(resolved), "git_unavailable", 503)
    return resolved


def environment():
    """A fixed Git environment that cannot prompt, write or page.

    `GIT_OPTIONAL_LOCKS=0` keeps `status` from refreshing (and therefore writing) the index,
    `GIT_TERMINAL_PROMPT=0` removes any credential prompt, and no pager can be started. The
    host configuration itself is deliberately *kept*: dropping it (for example with
    `GIT_CONFIG_NOSYSTEM`) makes a freshly committed checkout read as modified whenever the
    host defines line-ending or filter settings, and a package that disagrees with the agent's
    own `git status` is worse than useless. Repository-configurable helpers stay pinned off on
    the command line instead.
    """
    return {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "LANG": "C",
    }


def command(entry, *arguments):
    """One Git call with a fixed, read-only shape.

    The subcommand is checked against the read-only allowlist; `--no-optional-locks` and
    `core.fsmonitor=false` keep the call from writing the index or running a
    repository-configured helper, and submodule recursion and signature display are pinned off,
    so a collection cannot reach outside the registered root or execute repository code.
    """
    require(bool(arguments) and arguments[0] in READ_ONLY, "workdir_command_refused", 403)
    return [
        git_executable(),
        "-C",
        entry["path"],
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "status.submoduleSummary=false",
        "-c",
        "log.showSignature=false",
        *arguments,
    ]


def run(entry, arguments, limit):
    """Run one bounded read-only Git command, outside any transaction.

    The deadline is fixed and the accepted output is capped: an oversized answer is refused
    rather than silently truncated. Standard error stays out of the result, so no repository
    content or local path is echoed back to a client.
    """
    try:
        completed = subprocess.run(
            command(entry, *arguments),
            capture_output=True,
            env=environment(),
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise Fault("workdir_timeout", 408) from None
    except OSError:
        raise Fault("git_unavailable", 503) from None
    require(len(completed.stdout) <= limit, "workdir_output_too_large", 413)
    return completed.returncode, completed.stdout


def clean(value, limit=SUBJECT_LIMIT):
    """Repository text is untrusted data: one line, no control characters, bounded length."""
    printable = "".join(
        character for character in value if character >= " " and character != "\x7f"
    )
    return printable[:limit]


def root_of(entry):
    """Resolve the registered working directory, refusing a missing or unreadable root."""
    try:
        root = Path(entry["path"]).resolve(strict=True)
    except (OSError, ValueError):
        raise Fault("workdir_unavailable", 503) from None
    require(root.is_dir(), "workdir_unavailable", 503)
    return root


def parse_status(raw):
    """Classified counts of one porcelain v1 `-z` status, never the dirty paths themselves.

    Only counts leave this function: a continuation package states that a checkout is dirty and
    how, without listing or reading files the operator never registered.
    """
    fields, counts, index = raw.split(b"\x00"), dict.fromkeys(COUNT_KEYS, 0), 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        require(len(record) >= 3, "workdir_status_invalid", 503)
        code = record[:2].decode("ascii", "replace")
        # A rename or copy record carries the original path as a second NUL-separated field.
        if code[0] in "RC" or code[1] in "RC":
            index += 1
        counts["entries"] += 1
        if code == "??":
            counts["untracked"] += 1
        elif code in UNMERGED:
            counts["conflicted"] += 1
        else:
            if code[0] in "MARC":
                counts["staged"] += 1
            if code[1] in "MD":
                counts["modified"] += 1
            if "D" in code:
                counts["deleted"] += 1
            if "R" in code:
                counts["renamed"] += 1
    return counts


def _head(entry):
    """The current commit of one checkout, or an explicitly unborn branch."""
    status, raw = run(entry, ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"), 128)
    require(status in {0, 1}, "workdir_git_failed", 503)
    value = raw.decode("ascii", "replace").strip()
    if status != 0 or not value:
        return None, True
    require(COMMIT.match(value) is not None, "workdir_git_failed", 503)
    return value, False


def history(entry):
    """The bounded commit history of one checkout, newest first and size limited."""
    status, raw = run(
        entry,
        (
            "log",
            "-n",
            str(HISTORY_LIMIT),
            "--no-show-signature",
            "--no-decorate",
            "--format=%H%x00%s%x00%cI%x00",
        ),
        LOG_LIMIT,
    )
    require(status == 0, "workdir_git_failed", 503)
    fields = list(raw.split(b"\x00"))
    commits = []
    while len(commits) < HISTORY_LIMIT and len(fields) >= 3:
        commit, subject, committed_at = (fields.pop(0) for _ in range(3))
        if not commit:
            continue
        value = commit.decode("ascii", "replace").strip()
        require(COMMIT.match(value) is not None, "workdir_git_failed", 503)
        commits.append(
            {
                "commit": value,
                "subject": clean(subject.decode("utf-8", "replace")),
                "committed_at": clean(committed_at.decode("ascii", "replace").strip(), 64),
            }
        )
    return commits


def git_identity(entry):
    """Root, branch, HEAD and history of one registered checkout, read-only.

    The registered path must be the root of its own checkout: a subdirectory of somebody else's
    repository is refused instead of being reported as this agent's working directory. Branch
    and HEAD are read around the status call, so a checkout that moved while it was observed is
    refused instead of being reported half old and half new.
    """
    root = root_of(entry)
    status, raw = run(entry, ("rev-parse", "--show-toplevel"), PATH_LIMIT)
    require(status == 0, "workdir_not_repository", 409)
    top = raw.decode("utf-8", "replace").strip()
    require(
        bool(top) and os.path.normcase(str(Path(top))) == os.path.normcase(str(root)),
        "workdir_not_root",
        409,
    )
    version_status, version_raw = run(entry, ("--version",), VERSION_LIMIT)
    require(version_status == 0, "workdir_git_failed", 503)
    head, unborn = _head(entry)
    branch_status, branch_raw = run(entry, ("symbolic-ref", "--short", "-q", "HEAD"), PATH_LIMIT)
    require(branch_status in {0, 1}, "workdir_git_failed", 503)
    branch = clean(branch_raw.decode("utf-8", "replace").strip(), PATH_LIMIT) or None
    counts_status, counts_raw = run(
        entry,
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ),
        STATUS_LIMIT,
    )
    require(counts_status == 0, "workdir_status_failed", 503)
    counts = parse_status(counts_raw)
    again, _ = _head(entry)
    require(again == head, "workdir_changed", 409)
    return {
        "root": root,
        "branch": branch,
        "detached": branch is None,
        "head": head,
        "unborn": unborn,
        "dirty": counts["entries"] > 0,
        "counts": counts,
        "history": history(entry) if head else [],
        "git": {
            "version": clean(version_raw.decode("utf-8", "replace").strip(), VERSION_LIMIT),
            "timeout_seconds": TIMEOUT_SECONDS,
            "history_limit": HISTORY_LIMIT,
            "status_limit_bytes": STATUS_LIMIT,
            "subcommands": list(READ_ONLY),
            "writes": "never",
        },
    }


def file_facts(entry, indexed):
    """Digest the registered file list inside the registered root, outside any transaction.

    A registered path that became a link out of the checkout, a credential name or an
    unsupported type is refused as a fault (this service must not read it); a file that is
    simply gone, unreadable, changed while it was read or grew past the single-source limit is
    an honest per-file state instead.
    """
    rows = {row["locator"]: row for row in indexed}
    project = {"root": entry["path"]}
    result, total = [], 0
    for locator in entry["files"]:
        fact = _file_fact(project, entry, locator, rows.get(locator), total)
        total += fact["size"] or 0
        result.append(fact)
    return result


def _file_fact(project, entry, locator, row, total):
    fact = {
        "locator": locator,
        "state": "present",
        "size": None,
        "digest": None,
        "index": None,
        "freshness": "unindexed",
    }
    if row is not None:
        fact["index"] = {
            "document_id": row["document_id"],
            "version": row["version"],
            "state": row["state"],
            "hash": row["hash"],
        }
    if total >= entry["max_bytes"]:
        raise Fault("workdir_byte_budget", 409)
    try:
        raw, _ = read_file(project, locator)
    except Fault as error:
        if error.code == "source_changed":
            fact["state"] = "changed"
            return fact
        if error.code == "source_too_large":
            fact["state"] = "too_large"
            return fact
        if error.code == "unsupported":
            raise Fault("workdir_file_unsupported", 415) from None
        raise Fault("workdir_file_refused", 403) from None
    except FileNotFoundError:
        fact["state"] = "missing"
        return fact
    except OSError:
        fact["state"] = "unreadable"
        return fact
    fact["size"] = len(raw)
    fact["digest"] = content_hash(raw)
    if row is not None and row["state"] == "ready" and row["hash"] == fact["digest"]:
        fact["freshness"] = "current"
    elif row is not None:
        # An indexed digest never overrides the current file: both are reported side by side.
        fact["freshness"] = "changed"
    return fact


def observe(entry, indexed=(), digests=True):
    """Read-only facts of one registered working directory, outside any transaction."""
    identity = git_identity(entry)
    return {
        "id": entry["id"],
        "expected_branch": entry["branch"],
        "branch": identity["branch"],
        "detached": identity["detached"],
        "branch_matches": identity["branch"] == entry["branch"],
        "head": identity["head"],
        "unborn": identity["unborn"],
        "dirty": identity["dirty"],
        "counts": identity["counts"],
        "history": identity["history"],
        "git": identity["git"],
        "files": file_facts(entry, indexed) if digests else [],
        "collected_at": utc(now()),
        "registration": fingerprint(entry),
    }
