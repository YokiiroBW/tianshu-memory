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
        requests = []
        heads = self._request(requests, "owner_heads", ["core", "platform"], "owner")
        observations_index = len(requests)
        requests.append(
            (
                "SELECT s.* FROM source_observations s JOIN jsonb_to_recordset(?::jsonb) "
                "AS wanted(kind text,key text) USING(kind,key)",
                (canonical(observations),),
            )
        )
        physicals_index = self._request(requests, "physical_sources", physicals)
        bindings = self._request(requests, "accounts", authors, "account_key")
        sources = self._request(requests, "sources", admissions)
        suppressed = self._request(requests, "suppression", admissions, "source_key")
        # Physical aliases include other actors for negative broadcast. Receipt
        # collisions must also include unrelated physicals, not just this scope.
        admissions_index = len(requests)
        requests.append((ADMISSIONS_SQL, (physicals, admissions, receipts)))
        # All SELECTs execute after the caller's fresh revision/coverage CAS and
        # before its first mutation. read_batch confirms every result before it
        # returns; no setup or authority lock is moved into this read pipeline.
        cursors = db.read_batch(requests)
        self.heads = self._rows(cursors, heads, "owner")
        self.observations = {(row["kind"], row["key"]): row for row in cursors[observations_index]}
        self.physicals = self._rows(cursors, physicals_index)
        self.bindings = self._rows(cursors, bindings, "account_key")
        self.sources = self._rows(cursors, sources)
        self.suppressed = set(self._rows(cursors, suppressed, "source_key"))
        self.admissions = self._rows(cursors, admissions_index)
        self.receipts = {}
        self.aliases = {}
        for key, row in self.admissions.items():
            self.aliases.setdefault(row["physical_key"], set()).add(key)
            if row["receipt"] is not None:
                self.receipts.setdefault(row["receipt"], set()).add(key)

    @staticmethod
    def _request(requests, table, keys, column="key"):
        if not keys:
            return None
        # The identifiers are fixed internal call sites above, never owner input.
        index = len(requests)
        requests.append((f"SELECT * FROM {table} WHERE {column}=ANY(?::text[])", (keys,)))
        return index

    @staticmethod
    def _rows(cursors, index, column="key"):
        return {} if index is None else {row[column]: row for row in cursors[index]}

    def withdraw(self, key):
        # _apply broadcasts a physical negative before evaluating this actor's
        # admissions. Its later reads must observe that update, including epoch.
        if key in self.sources:
            row = dict(self.sources[key])
            row.update(state="withdrawn", epoch=row["epoch"] + 1)
            self.sources[key] = row
