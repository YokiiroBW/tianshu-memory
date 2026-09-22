"""Complete semantic groups and project evidence; no application imports."""

import json

from .domain import canonical, require
from .validate import exact, integer


def blocks(text, groups):
    """Explicit reviewed ranges partition all lines. Dependency closure is indivisible.

    No model guesses boundaries: the fallback is the entire source. Citations use
    decoded text lines (HTML: visible-text lines, with original bytes retained separately).
    """
    lines = text.splitlines(keepends=True)
    if groups is None:
        return [{"spans": [[1, len(lines)]], "text": text}]
    require(isinstance(groups, list) and 0 < len(groups) <= 128, "invalid_groups", 400)
    covered, nodes = set(), []
    for index, group in enumerate(groups):
        exact(group, "start end depends_on")
        integer(group["start"], 1, len(lines))
        integer(group["end"], group["start"], len(lines))
        span = set(range(group["start"], group["end"] + 1))
        require(not span & covered, "invalid_groups", 400)
        covered |= span
        require(isinstance(group["depends_on"], list), "invalid_groups", 400)
        for dependency in group["depends_on"]:
            integer(dependency, 0, len(groups) - 1)
        nodes.append({index, *group["depends_on"]})
    require(covered == set(range(1, len(lines) + 1)), "invalid_groups", 400)
    components = []
    for node in nodes:
        merged = set(node)
        for other in components[:]:
            if merged & other:
                merged |= other
                components.remove(other)
        components.append(merged)
    result = []
    for component in components:
        spans = sorted([groups[i]["start"], groups[i]["end"]] for i in component)
        result.append(
            {
                "spans": spans,
                "text": "\n".join("".join(lines[start - 1 : end]) for start, end in spans),
            }
        )
    return result


class Evidence:
    """Per-operation proof that a referenced project object is still current.

    Reuses the project-scoped document lookup, state check and double file hash of the
    knowledge operations, so lesson evidence is exactly the evidence a project query
    returns. It never inspects another project's rows.
    """

    def __init__(self, application, db, project_id, project, read):
        self.application = application
        self.db = db
        self.project_id = project_id
        self.project = project
        self.read = read

    def reference(self, reference):
        require(isinstance(reference, dict), "invalid_input", 400)
        require(len(canonical(reference).encode()) <= 2048, "request_too_large", 413)
        document = self.application.document(self.db, self.project_id, reference["document_id"])
        current = self.application.current(self.db, self.project, document, self.read)
        require(
            document["version"] == reference["version"] and current,
            "stale_evidence",
            409,
        )
        row = self.db.execute(
            "SELECT b.payload,v.hash,v.provenance FROM knowledge_blocks b "
            "JOIN knowledge_versions v ON v.document_id=b.document_id "
            "AND v.version=b.version WHERE b.id=? AND b.document_id=? AND b.version=?",
            (reference["block_id"], document["id"], document["version"]),
        ).fetchone()
        require(row is not None and row["hash"] == reference["hash"], "stale_evidence", 409)
        return {
            "reference": reference,
            **json.loads(row["payload"]),
            "source_id": document["source_id"],
            "provenance": json.loads(row["provenance"]),
        }
