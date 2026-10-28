"""Small actor authority with its own backend, lock and restore checkpoint."""

import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from tianshu_storage import StorageOptions, StorageRuntime

from .. import source_recovery
from ..domain import canonical, require
from . import search

TABLES = (
    "actor_metadata",
    "actor_episodes",
    "actor_dependencies",
    "actor_sources",
    "actor_receipts",
    "actor_derived",
    "actor_derived_basis",
)
DDL = (
    "CREATE TABLE actor_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE actor_episodes (id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,identity TEXT NOT NULL UNIQUE,fact TEXT NOT NULL,recorded_at TEXT NOT NULL)",
    "CREATE INDEX actor_episode_owner ON actor_episodes(actor_id,state,id)",
    "CREATE TABLE actor_dependencies (episode_id TEXT NOT NULL REFERENCES actor_episodes(id),actor_id TEXT NOT NULL,producer TEXT NOT NULL,object_id TEXT NOT NULL,source TEXT NOT NULL,generation TEXT NOT NULL,sequence INTEGER NOT NULL,access_version TEXT NOT NULL,PRIMARY KEY(episode_id,source))",
    "CREATE INDEX actor_dependency_source ON actor_dependencies(actor_id,producer,object_id)",
    "CREATE TABLE actor_sources (actor_id TEXT NOT NULL,producer TEXT NOT NULL,object_id TEXT NOT NULL,source TEXT NOT NULL,generation TEXT NOT NULL,sequence INTEGER NOT NULL,access_version TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(actor_id,producer,object_id))",
    "CREATE TABLE actor_receipts (caller TEXT NOT NULL,actor_id TEXT NOT NULL,command TEXT NOT NULL,idempotency_key TEXT NOT NULL,digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(caller,actor_id,command,idempotency_key))",
    "CREATE TABLE actor_derived (id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,identity TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,recorded_at TEXT NOT NULL,expires_at TEXT NOT NULL)",
    "CREATE INDEX actor_derived_owner ON actor_derived(actor_id,state,recorded_at)",
    "CREATE TABLE actor_derived_basis (derived_id TEXT NOT NULL REFERENCES actor_derived(id),actor_id TEXT NOT NULL,episode_id TEXT NOT NULL REFERENCES actor_episodes(id),episode_revision INTEGER NOT NULL,PRIMARY KEY(derived_id,episode_id))",
    "CREATE INDEX actor_derived_episode ON actor_derived_basis(episode_id,derived_id)",
)


def checkpoint(db):
    metadata = dict(db.execute("SELECT key,value FROM actor_metadata"))
    require(metadata.get("schema") in {"2", "3"}, "dependency_unavailable", 503)
    try:
        return {
            "domain": "actor-memory",
            "schema": int(metadata["schema"]),
            "instance": metadata["instance"],
            "revision": int(metadata["revision"]),
            "recovery": metadata["recovery"],
        }
    except (KeyError, ValueError):
        from ..domain import Fault

        raise Fault("dependency_unavailable", 503) from None


class Store:
    def __init__(self, config, *, initialize=False, storage_environ=None, allow_legacy=False):
        self.allow_legacy = allow_legacy
        section = config["actor_memory"]
        path = Path(section["database_path"])
        require(
            path.is_absolute() and path != Path(config["database_path"]),
            "dependency_unavailable",
            503,
        )
        self.path = path.resolve()
        self.state_directory = Path(section.get("state_directory", str(self.path) + ".storage"))
        self.guard = Path(section.get("guard_path", str(self.path) + ".guard.json"))
        require(
            self.state_directory.is_absolute()
            and self.guard.is_absolute()
            and self.guard != self.path,
            "dependency_unavailable",
            503,
        )
        protected = {
            Path(config["database_path"]).resolve(),
            Path(
                config.get("source_sync", {}).get(
                    "recovery_path", str(config["database_path"]) + ".source-guard.json"
                )
            ).resolve(),
        }
        require(
            self.path not in protected
            and self.guard.resolve() not in protected
            and self.state_directory.resolve()
            != Path(str(config["database_path"]) + ".storage").resolve(),
            "dependency_unavailable",
            503,
        )
        self.gate = threading.RLock()
        self.runtime = StorageRuntime(
            StorageOptions.from_settings(config, environ=storage_environ),
            service="actor-memory",
            state_directory=self.state_directory,
        )
        if not initialize:
            require(self.guard.is_file(), "dependency_unavailable", 503)
        try:
            self.runtime.start()
            self.database = self.runtime.database(
                "main", self.path, prepare=lambda db, source: self._prepare(db, source, initialize)
            )
            if self.database.backend == "sqlite":
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(self.database.connect(isolation_level=None)) as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("PRAGMA foreign_keys=ON")
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        self._prepare(db, None, initialize)
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
            self.runtime.finalize()
            with self.transaction():
                pass
        except BaseException:
            self.runtime.close()
            raise

    def _prepare(self, db, source, initialize):
        postgres = hasattr(db, "native")
        exists = (
            db.native("SELECT to_regclass(current_schema() || '.actor_metadata')").fetchone()[0]
            if postgres
            else db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='actor_metadata'"
            ).fetchone()
        )
        if exists:
            if postgres:
                db.native(
                    "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':actor-memory',0))"
                )
            for table in TABLES:
                db.execute("SELECT * FROM " + table + " LIMIT 0")
            source_recovery.verify(self.guard, checkpoint(db))
            require(
                self.allow_legacy or checkpoint(db)["schema"] == 3, "dependency_unavailable", 503
            )
            if checkpoint(db)["schema"] == 3:
                db.execute("SELECT * FROM actor_search_terms LIMIT 0")
            return
        require(
            initialize and not self.guard.exists() and source is None, "dependency_unavailable", 503
        )
        for statement in (*DDL, *search.DDL):
            db.execute(statement)
        for key, value in {
            "schema": "3",
            "instance": uuid4().hex,
            "revision": "1",
            "recovery": uuid4().hex,
        }.items():
            db.execute("INSERT INTO actor_metadata VALUES (?,?)", (key, value))
        source_recovery.persist(self.guard, checkpoint(db), initialize=True)

    @contextmanager
    def transaction(self, *, write=False):
        with self.gate, closing(self.database.connect(isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            postgres = hasattr(db, "native")
            if postgres:
                db.native("BEGIN")
                db.native(
                    "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':actor-memory',0))"
                )
            else:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
            try:
                source_recovery.verify(self.guard, checkpoint(db))
                if write:
                    db.execute(
                        "UPDATE actor_metadata SET value=CAST(value AS BIGINT)+1 WHERE key='revision'"
                    )
                yield db
                if write:
                    source_recovery.persist(self.guard, checkpoint(db))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def version(db):
        return int(
            db.execute("SELECT value FROM actor_metadata WHERE key='revision'").fetchone()[0]
        )

    def record_change(self, db):
        """Mark an actual maintenance mutation inside its existing transaction."""
        db.execute("UPDATE actor_metadata SET value=CAST(value AS BIGINT)+1 WHERE key='revision'")
        source_recovery.persist(self.guard, checkpoint(db))

    @classmethod
    def initialize(cls, config, backup):
        section = config["actor_memory"]
        path = Path(section["database_path"]).resolve()
        state = Path(section.get("state_directory", str(path) + ".storage"))
        guard = Path(section.get("guard_path", str(path) + ".guard.json"))
        if (
            path.exists()
            or (state / ".tianshu-storage-actor-memory.json").exists()
            or guard.exists()
        ):
            with closing(cls(config, allow_legacy=True)) as store:
                return store.migrate(backup)
        backup = Path(backup).resolve()
        require(backup not in {path, guard}, "invalid_input", 400)
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb") as stream:
            stream.write(
                canonical(
                    {"format": "actor-memory-initialization/v1", "previously_installed": False}
                ).encode()
            )
            stream.flush()
            os.fsync(stream.fileno())
        with closing(cls(config, initialize=True)) as store:
            return {
                "actor_memory_schema": 3,
                "backup": str(backup),
                "backend": store.database.backend,
            }

    def migrate(self, backup):
        """Explicit actor-only logical backup and atomic schema2 index installation."""
        backup = Path(backup).resolve()
        require(backup not in {self.path, self.guard.resolve()}, "invalid_input", 400)
        with self.transaction() as db:
            if checkpoint(db)["schema"] == 3:
                return {"actor_memory_schema": 3, "already_installed": True}
            snapshot = {
                "format": "actor-memory-backup/v1",
                "backend": self.database.backend,
                "guard": checkpoint(db),
                "tables": {},
            }
            for table in TABLES:
                cursor = db.execute("SELECT * FROM " + table)
                snapshot["tables"][table] = {
                    "columns": [part[0] for part in cursor.description],
                    "rows": [list(row) for row in cursor],
                }
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("xb") as stream:
                stream.write(canonical(snapshot).encode())
                stream.flush()
                os.fsync(stream.fileno())
            for statement in search.DDL:
                db.execute(statement)
            from . import lifecycle

            # Schema2 withdrawal retained its body. Upgrade the current actor
            # copies after the complete backup, preserving identity tombstones.
            for row in list(
                db.execute(
                    "SELECT * FROM actor_episodes WHERE state IN ('withdrawn','unavailable') AND fact!='{}'"
                )
            ):
                lifecycle.forget(
                    db,
                    row["actor_id"],
                    dict(kind="episode", id=row["id"], revision=row["revision"]),
                )
                db.execute(
                    "UPDATE actor_episodes SET state=? WHERE id=?", (row["state"], row["id"])
                )
            for row in list(
                db.execute(
                    "SELECT * FROM actor_derived WHERE state IN ('withdrawn','unavailable') AND payload!='{}'"
                )
            ):
                lifecycle.erase_derived(db, row, state=row["state"])
            search.rebuild(db)
            db.execute("UPDATE actor_metadata SET value='3' WHERE key='schema'")
            db.execute(
                "UPDATE actor_metadata SET value=CAST(value AS BIGINT)+1 WHERE key='revision'"
            )
            source_recovery.persist(self.guard, checkpoint(db))
        return {"actor_memory_schema": 3, "backup": str(backup), "backend": self.database.backend}

    def close(self):
        self.runtime.close()
