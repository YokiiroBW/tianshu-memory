"""Independent, fail-closed checkpoint. No backup restore approval is implemented."""

import logging
import os
from pathlib import Path
from uuid import uuid4

from .domain import Fault, canonical, strict_json


def checkpoint(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='metadata' AND type='table'"
    ).fetchone():
        return None
    metadata = dict(db.execute("SELECT key,value FROM metadata"))
    if metadata.get("schema") != "3":
        return None
    try:
        return {
            "schema": 3,
            "instance": metadata["source_instance"],
            "revision": int(metadata["source_revision"]),
            "recovery": metadata["source_recovery"],
        }
    except (KeyError, ValueError):
        raise Fault("dependency_unavailable", 503) from None


def verify(path, expected):
    try:
        actual = strict_json(path.read_bytes())
    except FileNotFoundError:
        logging.getLogger(__name__).error("source_checkpoint_missing: recovery review required")
        raise Fault("dependency_unavailable", 503) from None
    except (OSError, ValueError):
        logging.getLogger(__name__).error("source_checkpoint_unreadable: recovery review required")
        raise Fault("dependency_unavailable", 503) from None
    if canonical(actual) != canonical(expected):
        logging.getLogger(__name__).error("source_checkpoint_mismatch: recovery review required")
        raise Fault("dependency_unavailable", 503)


def persist(path, value, *, initialize=False):
    """Persist BEFORE SQLite commits. An interrupted commit may require recovery review,
    but cannot silently accept an older DB. BEGIN IMMEDIATE serializes all checkpoint writers.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if initialize:
        with path.open("xb") as file:
            file.write(canonical(value).encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        return
    temporary = path.with_name(path.name + "." + uuid4().hex + ".pending")
    try:
        with temporary.open("xb") as file:
            file.write(canonical(value).encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
