"""Ordered evidence resolution over raw rows from one Knowledge transaction phase.

Only document/version metadata and block identities are read eagerly. A
document's requested block rows are loaded after its first real current-source
check; every reference still repeats that check and materializes its own evidence
in input order. Neither source decisions nor decoded evidence survive this
generator or enter another phase.
"""

from .postgres import is_postgres

BATCH = 256
FIELDS = {"block_id", "document_id", "version", "hash"}


def resolve(db, project_id, project, references, read, *, one):
    if not is_postgres(db) or not _batchable(references):
        for reference in references:
            yield one(db, project_id, project, reference, read)
        return
    rows = ReferenceRows(db, project_id, references)
    for reference in references:
        yield one(db, project_id, project, reference, read, rows=rows)


class ReferenceRows:
    """Only raw rows, scoped to one call of resolve within a transaction phase."""

    def __init__(self, db, project_id, references):
        self.db, self.references = db, references
        self.documents, self.present = _metadata(db, project_id, references)
        self.blocks = {}

    def block(self, document, reference):
        # The caller has already performed its real version/current-source check.
        # An otherwise rejected hash cannot enable a body read by this strategy.
        if (
            document["reference_hash"] != reference["hash"]
            or (document["id"], reference["block_id"]) not in self.present
        ):
            return None
        key = (document["id"], document["version"])
        if key not in self.blocks:
            self.blocks[key] = _blocks(self.db, document, self.references, self.present)
        return self.blocks[key].get(reference["block_id"])


def _batchable(references):
    # Unusual inputs keep the original lazy scalar validation/error order. This
    # selects a strategy; it adds no new public rejection or validation policy.
    return (
        type(references) is list
        and len(references) > 1
        and all(
            type(reference) is dict
            and set(reference) == FIELDS
            and type(reference["version"]) is int
            and all(
                type(reference[key]) is str
                and not any(
                    char == "\x00" or "\ud800" <= char <= "\udfff" for char in reference[key]
                )
                for key in ("block_id", "document_id", "hash")
            )
            for reference in references
        )
    )


def _read(db, requests):
    if len(requests) == 1:
        sql, parameters = requests[0]
        return [db.native(sql, parameters)]
    return db.read_batch(requests)


def _metadata(db, project_id, references):
    keys = list(
        dict.fromkeys((reference["document_id"], reference["block_id"]) for reference in references)
    )
    requests = []
    for start in range(0, len(keys), BATCH):
        batch = keys[start : start + BATCH]
        requests.append(
            (
                "WITH requested(document_id,block_id) AS (VALUES "
                + ",".join("(?,?)" for _ in batch)
                + ") SELECT d.*,v.hash AS reference_hash,b.id AS reference_block "
                "FROM requested r JOIN knowledge_documents d ON d.id=r.document_id "
                "LEFT JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.version "
                "LEFT JOIN knowledge_blocks b ON b.document_id=d.id AND b.version=d.version "
                "AND b.id=r.block_id WHERE d.project_id=?",
                [item for pair in batch for item in pair] + [project_id],
            )
        )
    documents, present = {}, set()
    for cursor in _read(db, requests):
        for row in cursor:
            documents[row["id"]] = row
            if row["reference_block"] is not None:
                present.add((row["id"], row["reference_block"]))
    return documents, present


def _blocks(db, document, references, present):
    keys = list(
        dict.fromkeys(
            reference["block_id"]
            for reference in references
            if reference["document_id"] == document["id"]
            and reference["version"] == document["version"]
            and reference["hash"] == document["reference_hash"]
            and (document["id"], reference["block_id"]) in present
        )
    )
    requests = []
    for start in range(0, len(keys), BATCH):
        batch = keys[start : start + BATCH]
        requests.append(
            (
                "SELECT b.id,b.payload,v.hash,v.provenance FROM knowledge_blocks b "
                "JOIN knowledge_versions v ON v.document_id=b.document_id AND v.version=b.version "
                "WHERE b.document_id=? AND b.version=? AND b.id IN ("
                + ",".join("?" for _ in batch)
                + ")",
                [document["id"], document["version"], *batch],
            )
        )
    return {row["id"]: row for cursor in _read(db, requests) for row in cursor}
