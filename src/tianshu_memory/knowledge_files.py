"""Immutable Knowledge originals and derived text, owned by this service's file root.

SQL retains version/authority/citation metadata. Files are committed before references and
are never deleted automatically: a failed transaction can leave an unreferenced file, never
a committed reference to a partially written object. Reads always verify the shared FileRef.
"""

import hashlib
import json

from .domain import Fault, canonical

FORMAT = "tianshu-knowledge-file/v1"


def reference(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("format") == FORMAT:
        if not isinstance(parsed.get("file"), dict):
            raise Fault("dependency_unavailable", 503)
        return parsed["file"]
    return None


def put(db, value, *, media_type="application/octet-stream", text=False):
    files = db.knowledge_files
    ref = files.put_text(value) if text else files.put_bytes(value, media_type=media_type)
    return canonical({"format": FORMAT, "file": ref.as_dict()})


def read(db, value, *, text=False):
    from tianshu_storage import StorageError

    ref = reference(value)
    if ref is None:
        # Legacy rows are only accepted until the startup file migration commits.
        return value
    try:
        return db.knowledge_files.read_text(ref) if text else db.knowledge_files.read_bytes(ref)
    except (OSError, ValueError, KeyError, StorageError) as error:
        raise Fault("dependency_unavailable", 503) from error


def block(db, payload):
    value = json.loads(payload) if isinstance(payload, str) else dict(payload)
    if "text_file" in value:
        value["text"] = read(db, value.pop("text_file"), text=True)
    return value


def pack_block(db, value):
    value = dict(value)
    if "text" in value:
        value["text_file"] = put(db, value.pop("text"), text=True)
    return canonical(value)


def externalize(db):
    """Move installed legacy originals/uploads/blocks without changing logical versions."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "knowledge_versions" not in tables:
        return False
    if db.execute("SELECT 1 FROM metadata WHERE key='knowledge_files_schema'").fetchone():
        return False
    for row in db.execute(
        "SELECT document_id,version,raw,text,media_type,hash FROM knowledge_versions"
    ):
        raw = bytes(read(db, row[2]))
        text = read(db, row[3], text=True)
        if hashlib.sha256(raw).hexdigest() != row[5]:
            raise ValueError("Knowledge original failed migration hash validation")
        db.execute(
            "UPDATE knowledge_versions SET raw=?,text=? WHERE document_id=? AND version=?",
            (
                put(db, raw, media_type=row[4]),
                put(db, text, text=True),
                row[0],
                row[1],
            ),
        )
    for row in db.execute("SELECT id,payload FROM knowledge_blocks"):
        value = json.loads(row[1])
        if "text" in value:
            db.execute(
                "UPDATE knowledge_blocks SET payload=? WHERE id=?", (pack_block(db, value), row[0])
            )
    if "knowledge_content_uploads" in tables:
        for row in db.execute(
            "SELECT id,raw,descriptor FROM knowledge_content_uploads WHERE raw IS NOT NULL"
        ):
            raw = bytes(read(db, row[1]))
            descriptor = json.loads(row[2])
            if (
                len(raw) != descriptor["size"]
                or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]
            ):
                raise ValueError("Knowledge upload failed migration hash validation")
            db.execute(
                "UPDATE knowledge_content_uploads SET raw=? WHERE id=?",
                (put(db, raw, media_type=descriptor["media_type"]), row[0]),
            )
    db.execute("INSERT INTO metadata VALUES ('knowledge_files_schema','1')")
    return True
