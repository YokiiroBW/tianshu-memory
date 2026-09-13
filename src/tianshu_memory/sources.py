"""Source verification port; the sole shipped backend is explicitly a local fixture.

A production adapter must verify current owner revision, scope, revocation and archive receipt,
and synchronize invalidation with the authority transaction. No guessed Chat Audit endpoint.
"""

from .domain import Fault, canonical, require, source_key


class LocalFixtureSources:
    """Synthetic, operator-loaded ledger, for isolated verification only."""

    def verify(self, db, sources, scope):
        for source in sources:
            row = db.execute("SELECT * FROM sources WHERE key=?", (source_key(source),)).fetchone()
            if row is None:
                raise Fault("dependency_unavailable", 503)
            require(row["scope"] == canonical(scope))

    def current(self, db, rows):
        return bool(rows) and all(
            row["state"] == "active"
            and row["revision"] == row["required_revision"]
            and row["epoch"] == row["required_epoch"]
            for row in rows
        )
