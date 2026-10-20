"""Bounded-domain reads for one PostgreSQL source synchronization transaction.

This object lives only inside SourceAuthority._apply, after its revision/coverage
CAS. It supplies existing rows to the original authority rules; it never accepts
owner facts, skips their validation or retains a grant between operations.
"""

from .domain import canonical, fingerprint

ADMISSIONS_SQL = (
    "SELECT a.*,payload::jsonb #>> '{source,receipt_id}' AS receipt "
    "FROM source_admissions a WHERE physical_key=ANY(?::text[]) "
    "OR key=ANY(?::text[]) "
    "OR (payload IS NOT NULL AND (payload::jsonb #>> '{source,receipt_id}')=ANY(?::text[]))"
)


class SourceSnapshot:
    def __init__(self, db, snapshot, access):
        physicals = [fingerprint(value["key"]) for value in snapshot["physicals"]]
        admissions = [fingerprint(value["selector"]) for value in snapshot["admissions"]]
        receipts = [value["source"]["receipt_id"] for value in snapshot["admissions"]]
        authors = list({canonical(value["author"]) for value in snapshot["physicals"]})
        observations = [
            {"kind": kind, "key": key}
            for kind, keys in (
                ("physical", physicals),
                ("admission", admissions),
                ("turn", [value["turn_id"] for value in snapshot["turns"]]),
                ("access", [fingerprint(value["selector"]) for value in access["grants"]]),
            )
            for key in keys
        ]
        self.heads = self._rows(db, "owner_heads", ["core", "platform"], "owner")
        self.observations = {
            (row["kind"], row["key"]): row
            for row in db.native(
                "SELECT s.* FROM source_observations s JOIN jsonb_to_recordset(?::jsonb) "
                "AS wanted(kind text,key text) USING(kind,key)",
                (canonical(observations),),
            )
        }
        self.physicals = self._rows(db, "physical_sources", physicals)
        self.bindings = self._rows(db, "accounts", authors, "account_key")
        self.sources = self._rows(db, "sources", admissions)
        self.suppressed = set(self._rows(db, "suppression", admissions, "source_key"))
        # Physical aliases include other actors for negative broadcast. Receipt
        # collisions must also include unrelated physicals, not just this scope.
        self.admissions = {
            row["key"]: row
            for row in db.native(
                ADMISSIONS_SQL,
                (physicals, admissions, receipts),
            )
        }
        self.receipts = {}
        self.aliases = {}
        for key, row in self.admissions.items():
            self.aliases.setdefault(row["physical_key"], set()).add(key)
            if row["receipt"] is not None:
                self.receipts.setdefault(row["receipt"], set()).add(key)

    @staticmethod
    def _rows(db, table, keys, column="key"):
        if not keys:
            return {}
        # The identifiers are fixed internal call sites above, never owner input.
        return {
            row[column]: row
            for row in db.native(f"SELECT * FROM {table} WHERE {column}=ANY(?::text[])", (keys,))
        }

    def withdraw(self, key):
        # _apply broadcasts a physical negative before evaluating this actor's
        # admissions. Its later reads must observe that update, including epoch.
        if key in self.sources:
            row = dict(self.sources[key])
            row.update(state="withdrawn", epoch=row["epoch"] + 1)
            self.sources[key] = row
