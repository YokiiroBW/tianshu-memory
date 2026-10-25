"""Complete local window dependencies inside one final source-read transaction.

The caller has already synchronized the owners and holds the authority lock.
Nothing here is retained after window validation or used after invalidation.
"""

from collections import defaultdict

from .domain import canonical

_READ_BATCH = 256


class WindowSnapshot:
    def __init__(self, service, db, manifests, *, consolidated):
        sources, accounts, groups, records, episodes = set(), set(), set(), set(), set()
        for manifest in manifests:
            for member in manifest["members"]:
                if member["role"] == "user":
                    sources.add(
                        service.source_identity(
                            member["reference"]["source"], member["source_scope"]
                        )
                    )
                    accounts.add(canonical(member["speaker"]["account"]))
                dependencies = member["memory_dependencies"]
                sources.update(item["source_key"] for item in dependencies["source_dependencies"])
                groups.update(
                    item["group"]["semantic_group_id"]
                    for item in dependencies["group_dependencies"]
                )
                records.update(
                    ref["record_id"]
                    for item in dependencies["group_dependencies"]
                    for ref in item["records"]
                )
                episodes.update(item["episode_id"] for item in dependencies["episode_dependencies"])

        requests, destinations = [], []

        def add(bucket, keys, sql):
            keys = sorted(keys)
            for start in range(0, len(keys), _READ_BATCH):
                selected = keys[start : start + _READ_BATCH]
                placeholders = ",".join("?" for _ in selected)
                requests.append((sql.replace("{keys}", placeholders), selected))
                destinations.append(bucket)

        add(
            "sources",
            sources,
            "SELECT s.*,p.payload AS physical,"
            "EXISTS(SELECT 1 FROM suppression w WHERE w.source_key=s.key) AS suppressed "
            "FROM sources s JOIN source_admissions a ON a.key=s.key AND a.verified=1 "
            "LEFT JOIN physical_sources p ON p.key=a.physical_key WHERE s.key IN ({keys})",
        )
        add("bindings", accounts, "SELECT * FROM accounts WHERE account_key IN ({keys})")
        add("groups", groups, "SELECT * FROM groups WHERE id IN ({keys})")
        add("records", records, "SELECT * FROM records WHERE id IN ({keys})")
        add("episodes", episodes, "SELECT * FROM memory_documents WHERE id IN ({keys})")
        add("lineage", groups, "SELECT group_id,source_key FROM lineage WHERE group_id IN ({keys})")
        if consolidated:
            add(
                "kinds",
                groups,
                "SELECT group_id,kind FROM memory_summaries WHERE group_id IN ({keys})",
            )
            add(
                "lineage",
                groups,
                "SELECT group_id,source_key FROM memory_summary_sources WHERE group_id IN ({keys})",
            )
        self.sources, self.bindings, self.groups = {}, {}, {}
        self.records, self.episodes, self.kinds = {}, {}, {}
        self.lineage = defaultdict(set)
        # The existing PG driver confirms the full read pipeline. SQLite runs
        # these same bounded queries locally; neither path owns a transaction.
        cursors = (
            db.read_batch(requests)
            if hasattr(db, "read_batch")
            else [db.execute(sql, parameters) for sql, parameters in requests]
        )
        identity = dict(
            sources="key", bindings="account_key", groups="id", records="id", episodes="id"
        )
        for bucket, cursor in zip(destinations, cursors, strict=True):
            for row in cursor:
                if bucket == "lineage":
                    self.lineage[row["group_id"]].add(row["source_key"])
                elif bucket == "kinds":
                    self.kinds[row["group_id"]] = row["kind"]
                else:
                    getattr(self, bucket)[row[identity[bucket]]] = row
