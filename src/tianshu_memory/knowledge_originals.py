"""The single Knowledge original/version writer, shared by project and life content."""

from .domain import canonical, now, utc


def store_version(db, project_id, document_id, source_id, kind, locator, prepared, old, version):
    if old:
        db.execute(
            "UPDATE knowledge_documents SET version=?,state='ready' WHERE id=?",
            (version, document_id),
        )
    else:
        db.execute(
            "INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?)",
            (document_id, project_id, source_id, kind, locator, version, "ready"),
        )
    provenance = {
        "kind": kind,
        "locator": locator,
        "resolved": prepared["resolved"],
        "imported_at": utc(now()),
        "processing": "verbatim",
        "citation_space": "visible_text_lines"
        if prepared["media"] == "text/html"
        else "text_lines",
    }
    provenance.update(prepared.get("provenance", {}))
    db.execute(
        "INSERT INTO knowledge_versions VALUES (?,?,?,?,?,?,?)",
        (
            document_id,
            version,
            prepared["digest"],
            prepared["raw"],
            prepared["text"],
            prepared["media"],
            canonical(provenance),
        ),
    )
    db.execute(
        "DELETE FROM knowledge_index WHERE block_id IN (SELECT id FROM knowledge_blocks WHERE document_id=?)",
        (document_id,),
    )
    for index, unit in enumerate(prepared["units"]):
        block_id = f"{document_id}:{version}:{index:03d}"
        db.execute(
            "INSERT INTO knowledge_blocks VALUES (?,?,?,?)",
            (block_id, document_id, version, canonical(unit)),
        )
        db.execute(
            "INSERT INTO knowledge_index VALUES (?,?,?)",
            (block_id, project_id, prepared["index"][index]),
        )
    if project_id is not None:
        db.execute("UPDATE knowledge_projects SET revision=revision+1 WHERE id=?", (project_id,))
