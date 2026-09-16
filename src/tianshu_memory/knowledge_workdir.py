"""Registered working directories: read-only Git and restricted file facts for continuation.

A continuation package must describe the checkout an agent is working in right now, and it
must never let an older indexed digest stand in for the current file. This is the only code
path that reads a registered working directory: a fixed, bounded set of read-only Git
commands (never fetch, checkout, reset, clean or diff; never a repository hook or a
repository-configured helper; never an untracked file's content) plus the digests of a small
operator-registered file list. Nothing here writes to the working directory, every read stays
inside the registered root, and the whole collection runs outside any database transaction, so
slow Git or disk I/O never holds the shared writer lock.

Read-only is not a promise about intent, it is a property of the call: a repository can name a
program for Git to run while *reading* (a `filter` clean/process driver chosen by
`.gitattributes`, a `diff` textconv or external command, a filesystem monitor). Every configured
driver name is therefore discovered from the merged configuration and overridden on the command
line, so no repository, user or system configuration can make a collection execute code; the
child also receives a fixed environment instead of the service's, so no `GIT_*` variable can
inject one. Output is capped *while* the child runs rather than after it exits, and the size of
each registered file is charged to the cumulative read budget before it is read.
"""

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event, Thread

from .domain import Fault, canonical, fingerprint, now, require, utc
from .knowledge_sources import (
    DENIED,
    SUFFIXES,
    content_hash,
    file_path,
    read_file,
    refused_component,
)

# The Git executable is operator environment, exactly like the private configuration file: it
# can be named explicitly for a host where Git is not on the service account's PATH.
GIT_ENVIRONMENT_VARIABLE = "TIANSHU_GIT"
MAX_WORKTREES = 16
MAX_CLIENTS = 16
MAX_FILES = 16
MAX_HELPERS = 32
MIN_BYTES, MAX_BYTES = 1024, 8 * 1024 * 1024
PATH_LIMIT = 1024
HISTORY_LIMIT = 8
SUBJECT_LIMIT = 200
TIMEOUT_SECONDS = 10
STDERR_LIMIT = 8192
STATUS_LIMIT = 262144
LOG_LIMIT = 65536
VERSION_LIMIT = 256
HELPER_LIMIT = 65536
# A checkout with more changed entries than this is described by counts alone: the fingerprint
# stops and says so instead of walking an unbounded working tree.
CHANGE_LIMIT = 512
CHANGE_MODE = "stat_only"
FIELDS = "id path branch clients files max_bytes"
# The child environment is an allowlist, not the service environment: a `GIT_CONFIG_COUNT`,
# `GIT_CONFIG_KEY_*`, `GIT_DIR`, `GIT_EXTERNAL_DIFF` or `GIT_ASKPASS` in the service environment
# could otherwise define a program for Git to run. Git still finds its own system and user
# configuration relative to the executable and the user profile, so readings stay comparable to
# the agent's own `git status`.
INHERITED = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SystemDrive",
    "COMSPEC",
    "WINDIR",
    "OS",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "USERNAME",
    "USERDOMAIN",
    "LANG",
    "LC_ALL",
    "TZ",
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# Every Git subcommand (and the one global flag) this service may run, and nothing else. A
# collection never mutates a repository, never contacts a remote and never runs repository code.
READ_ONLY = ("--version", "config", "log", "rev-parse", "status", "symbolic-ref")
# Configuration keys Git consults while *reading* that can name a program. `config` is only ever
# called with `--list`, which reads configuration and runs nothing.
PINNED = (
    ("core.fsmonitor", "false"),
    ("status.submoduleSummary", "false"),
    ("log.showSignature", "false"),
)
UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
COUNT_KEYS = ("staged", "modified", "deleted", "renamed", "untracked", "conflicted", "entries")
CHANGE_FIELDS = "mode entries complete fingerprint"
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
    """A fixed Git environment that cannot prompt, write, page or run helper code.

    `GIT_OPTIONAL_LOCKS=0` keeps `status` from refreshing (and therefore writing) the index,
    `GIT_TERMINAL_PROMPT=0` removes any credential prompt, and no pager can be started. The
    host configuration itself is deliberately *kept*: dropping it (for example with
    `GIT_CONFIG_NOSYSTEM`) makes a freshly committed checkout read as modified whenever the
    host defines line-ending or filter settings, and a package that disagrees with the agent's
    own `git status` is worse than useless. Repository-configurable helpers stay pinned off on
    the command line instead, and this child environment is built from an allowlist so that no
    inherited `GIT_*` variable (a `GIT_CONFIG_COUNT` entry, `GIT_EXTERNAL_DIFF`, `GIT_DIR` or
    `GIT_ASKPASS`) can define a program for a read-only call to run.
    """
    return {
        **{key: os.environ[key] for key in INHERITED if key in os.environ},
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "LANG": "C",
    }


def helpers(entry):
    """Every configured `filter`/`diff` driver name, discovered read-only and neutralized later.

    A driver only exists if some configuration file (system, user, repository or an included
    file) defines it, and `git config` reads exactly that merged set without touching the
    working tree, so this list is the complete set of programs a subsequent read-only call could
    otherwise be made to run. Discovery itself runs nothing, and the listing is filtered here
    rather than by a pattern argument: an argument full of regular-expression punctuation would
    be re-parsed by the command interpreter when the configured Git is a `cmd` shim, and a
    silently altered pattern is exactly the kind of quiet difference this guard exists to
    prevent. Values are dropped as they are read; only names are kept.
    """
    status, raw = run(entry, ("config", "--null", "--list"), HELPER_LIMIT)
    require(status in {0, 1}, "workdir_git_failed", 503)
    names = set()
    for record in raw.split(b"\x00"):
        if not record:
            continue
        key = record.split(b"\n", 1)[0].decode("utf-8", "replace")
        section, _, remainder = key.partition(".")
        name = remainder.split(".", 1)[0]
        if section in {"filter", "diff"} and name:
            names.add(name)
    require(len(names) <= MAX_HELPERS, "workdir_helpers_unbounded", 503)
    return tuple(sorted(names))


def command(entry, *arguments, guard=()):
    """One Git call with a fixed, read-only shape that cannot execute repository code.

    The subcommand is checked against the read-only allowlist; `--no-optional-locks` and
    `core.fsmonitor=false` keep the call from writing the index or running a
    repository-configured helper, submodule recursion and signature display are pinned off, and
    hooks are pointed at a directory that never exists. Every configured `filter` and `diff`
    driver is then overridden with an empty command of higher precedence than any configuration
    file, so a `filter=lfs`, `filter=probe` or `diff=x` attribute cannot make Git run a program
    while this service reads a checkout. Submodule recursion stays off, so a collection cannot
    reach outside the registered root either.
    """
    require(bool(arguments) and arguments[0] in READ_ONLY, "workdir_command_refused", 403)
    hooks = Path(entry["path"]) / ".git" / "dsh-no-hooks"
    pinned = ["-c", f"core.hooksPath={hooks}"]
    for key, value in PINNED:
        pinned += ["-c", f"{key}={value}"]
    for name in guard:
        pinned += [
            "-c",
            f"filter.{name}.clean=",
            "-c",
            f"filter.{name}.process=",
            "-c",
            f"filter.{name}.smudge=",
            "-c",
            f"filter.{name}.required=false",
            "-c",
            f"filter.{name}.clean.required=false",
            "-c",
            f"filter.{name}.smudge.required=false",
            "-c",
            f"diff.{name}.command=",
            "-c",
            f"diff.{name}.textconv=",
        ]
    return [
        git_executable(),
        "-C",
        entry["path"],
        "--no-optional-locks",
        *pinned,
        *arguments,
    ]


def _consume(pipe, sink, cap, overflow):
    """Read one child pipe into `sink`, stopping at its cap instead of buffering past it."""
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            if len(sink) + len(chunk) > cap:
                sink.extend(chunk[: cap - len(sink)])
                overflow.set()
                return
            sink.extend(chunk)
    except (OSError, ValueError):
        return


def run(entry, arguments, limit, guard=()):
    """Run one bounded read-only Git command, outside any transaction.

    Both pipes are collected with hard caps *while* the child runs: an answer that grows past
    the accepted size, or one that does not finish inside the fixed deadline, has the child
    killed instead of buffered, so the advertised limits bound the collection rather than
    merely rejecting its result afterwards. Standard error is capped too and never returned, so
    no repository content or local path is echoed back to a client.
    """
    try:
        process = subprocess.Popen(
            command(entry, *arguments, guard=guard),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment(),
        )
    except OSError:
        raise Fault("git_unavailable", 503) from None
    output, errors, overflow = bytearray(), bytearray(), Event()
    readers = [
        Thread(target=_consume, args=(process.stdout, output, limit, overflow), daemon=True),
        Thread(target=_consume, args=(process.stderr, errors, STDERR_LIMIT, overflow), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while not overflow.is_set() and any(reader.is_alive() for reader in readers):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for reader in readers:
            reader.join(min(0.05, remaining))
    over = overflow.is_set()
    stuck = any(reader.is_alive() for reader in readers)
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for reader in readers:
        reader.join(1)
    for pipe in (process.stdout, process.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    if over:
        raise Fault("workdir_output_too_large", 413)
    if stuck:
        raise Fault("workdir_timeout", 408)
    return process.returncode, bytes(output)


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


def status_records(raw):
    """The changed records of one porcelain v1 `-z` status: `(code, path)` pairs, in Git order."""
    fields, records, index = raw.split(b"\x00"), [], 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        require(len(record) >= 3, "workdir_status_invalid", 503)
        code = record[:2].decode("ascii", "replace")
        path = record[3:].decode("utf-8", "replace")
        # A rename or copy record carries the original path as a second NUL-separated field.
        if code[0] in "RC" or code[1] in "RC":
            index += 1
        records.append((code, path))
    return records


def parse_status(raw):
    """Classified counts of one porcelain v1 `-z` status, never the dirty paths themselves.

    Only counts leave this function: a continuation package states that a checkout is dirty and
    how, without listing or reading files the operator never registered.
    """
    counts = dict.fromkeys(COUNT_KEYS, 0)
    for code, _ in status_records(raw):
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


def _change_stat(root, path):
    """Size, mtime and mode of one changed path, or nothing when it is gone or outside the root.

    Metadata only: a changed file that the operator never registered is never opened. A reported
    path that would leave the registered root is described without being touched at all.
    """
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        return [None, None, None]
    try:
        info = os.lstat(root / relative)
    except (OSError, ValueError):
        return [None, None, None]
    return [info.st_size, info.st_mtime_ns, info.st_mode]


def change_facts(entry, raw):
    """A bounded, content-free fingerprint of the current uncommitted state of one checkout.

    Counts and a dirty flag cannot tell a second edit from the first: a tracked file that is not
    on the registered digest list can be rewritten again to the same size with the same status
    counts, and an old package would still look current. Git already reports every changed path
    in the status output this collector reads, so each record is reduced to its status code, a
    digest of its path and its stat metadata. Two observations of one checkout can therefore be
    compared without reading any content, without reading an untracked file and without putting
    a path into the package. A checkout with more changed entries than the cap is reported as
    incomplete, which callers treat as "cannot be proven" instead of "unchanged".
    """
    records = status_records(raw)
    root = Path(entry["path"])
    bounded = records[:CHANGE_LIMIT]
    facts = [
        [code, hashlib.sha256(path.encode()).hexdigest(), *_change_stat(root, path)]
        for code, path in bounded
    ]
    facts.sort(key=lambda item: item[1])
    return {
        "mode": CHANGE_MODE,
        "entries": len(records),
        "complete": len(records) == len(bounded),
        "fingerprint": hashlib.sha256(canonical(facts).encode()).hexdigest(),
    }


def _head(entry, guard=()):
    """The current commit of one checkout, or an explicitly unborn branch."""
    status, raw = run(entry, ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"), 128, guard)
    require(status in {0, 1}, "workdir_git_failed", 503)
    value = raw.decode("ascii", "replace").strip()
    if status != 0 or not value:
        return None, True
    require(COMMIT.match(value) is not None, "workdir_git_failed", 503)
    return value, False


def history(entry, guard=()):
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
        guard,
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
    """Root, branch, HEAD, changed-state fingerprint and history of one checkout, read-only.

    The registered path must be the root of its own checkout: a subdirectory of somebody else's
    repository is refused instead of being reported as this agent's working directory. Branch
    and HEAD are read around the status call, so a checkout that moved while it was observed is
    refused instead of being reported half old and half new. The configured helper drivers are
    discovered once and neutralized on every call.
    """
    root = root_of(entry)
    guard = helpers(entry)
    status, raw = run(entry, ("rev-parse", "--show-toplevel"), PATH_LIMIT, guard)
    require(status == 0, "workdir_not_repository", 409)
    top = raw.decode("utf-8", "replace").strip()
    require(
        bool(top) and os.path.normcase(str(Path(top))) == os.path.normcase(str(root)),
        "workdir_not_root",
        409,
    )
    version_status, version_raw = run(entry, ("--version",), VERSION_LIMIT, guard)
    require(version_status == 0, "workdir_git_failed", 503)
    head, unborn = _head(entry, guard)
    branch_status, branch_raw = run(
        entry, ("symbolic-ref", "--short", "-q", "HEAD"), PATH_LIMIT, guard
    )
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
        guard,
    )
    require(counts_status == 0, "workdir_status_failed", 503)
    counts = parse_status(counts_raw)
    changes = change_facts(entry, counts_raw)
    again, _ = _head(entry, guard)
    require(again == head, "workdir_changed", 409)
    return {
        "root": root,
        "branch": branch,
        "detached": branch is None,
        "head": head,
        "unborn": unborn,
        "dirty": counts["entries"] > 0,
        "counts": counts,
        "changes": changes,
        "history": history(entry, guard) if head else [],
        "git": {
            "version": clean(version_raw.decode("utf-8", "replace").strip(), VERSION_LIMIT),
            "timeout_seconds": TIMEOUT_SECONDS,
            "history_limit": HISTORY_LIMIT,
            "status_limit_bytes": STATUS_LIMIT,
            "subcommands": list(READ_ONLY),
            "helpers": list(guard),
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
    try:
        target = file_path(project, locator)
    except Fault as error:
        if error.code == "unsupported":
            raise Fault("workdir_file_unsupported", 415) from None
        raise Fault("workdir_file_refused", 403) from None
    except FileNotFoundError:
        fact["state"] = "missing"
        return fact
    except OSError:
        fact["state"] = "unreadable"
        return fact
    # The cumulative read budget is charged with the size this file will contribute *before* it
    # is read, so the registered set can never exceed the configured maximum even by its last
    # member; the per-file limit is enforced by the reader itself.
    try:
        projected = target.stat().st_size
    except OSError:
        fact["state"] = "unreadable"
        return fact
    require(total + projected <= entry["max_bytes"], "workdir_byte_budget", 409)
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
        "changes": identity["changes"],
        "history": identity["history"],
        "git": identity["git"],
        "files": file_facts(entry, indexed) if digests else [],
        "collected_at": utc(now()),
        "registration": fingerprint(entry),
    }
