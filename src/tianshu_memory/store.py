import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4


class LocalConnection(sqlite3.Connection):
    """SQLite connection with this operation's explicit Knowledge file owner."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS people (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS accounts (
  account_key TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  version INTEGER NOT NULL, display_name TEXT);
CREATE TABLE IF NOT EXISTS scopes (key TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
  service TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
  digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(service,operation,key));
CREATE TABLE IF NOT EXISTS sources (
  key TEXT PRIMARY KEY, revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
  state TEXT NOT NULL, scope TEXT NOT NULL, reality TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, state TEXT NOT NULL, members TEXT NOT NULL,
  category TEXT NOT NULL, field_key TEXT, item_key TEXT);
CREATE INDEX IF NOT EXISTS groups_scope ON groups(scope,state,category);
CREATE INDEX IF NOT EXISTS groups_field ON groups(scope,field_key,state,category);
CREATE INDEX IF NOT EXISTS groups_item ON groups(scope,item_key,state,category);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id),
  version INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS records_group ON records(group_id);
CREATE TABLE IF NOT EXISTS history (
  record_id TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY(record_id,version));
CREATE TABLE IF NOT EXISTS lineage (
  group_id TEXT NOT NULL REFERENCES groups(id), source_key TEXT NOT NULL REFERENCES sources(key),
  revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
  PRIMARY KEY(group_id,source_key));
CREATE TABLE IF NOT EXISTS projections (
  ref TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES records(id),
  version INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confirmations (
  ref TEXT PRIMARY KEY, digest TEXT NOT NULL, account_key TEXT NOT NULL,
  scope TEXT NOT NULL, expires_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS inbox (
  event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_inputs (
  turn_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
  result TEXT NOT NULL, PRIMARY KEY(turn_id,revision));
CREATE TABLE IF NOT EXISTS aggregate_events (
  owner TEXT NOT NULL, aggregate_id TEXT NOT NULL, version INTEGER NOT NULL,
  digest TEXT NOT NULL, PRIMARY KEY(owner,aggregate_id,version));
CREATE TABLE IF NOT EXISTS conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL,
  request_id TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, state TEXT NOT NULL, event TEXT NOT NULL, source_snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS write_ledger (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id), digest TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_writes (
  source_key TEXT NOT NULL, revision INTEGER NOT NULL, scope TEXT NOT NULL,
  job_id TEXT NOT NULL, PRIMARY KEY(source_key,revision,scope));
CREATE TABLE IF NOT EXISTS relationship_entries (
  group_id TEXT PRIMARY KEY REFERENCES groups(id), amount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
  position INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL, payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
  record_id UNINDEXED, record_version UNINDEXED, scope UNINDEXED, text);
"""

PROFILE_SCHEMA = """
CREATE TABLE profile_shares (
  group_id TEXT PRIMARY KEY REFERENCES groups(id), actor_id TEXT NOT NULL,
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, sharing TEXT NOT NULL,
  conversation_id TEXT, approval_ref TEXT NOT NULL UNIQUE);
CREATE INDEX profile_audience ON profile_shares(
  actor_id,subject_kind,subject_id,sharing,conversation_id);
CREATE TABLE profile_approvals (
  ref TEXT PRIMARY KEY, digest TEXT NOT NULL, source_snapshot TEXT NOT NULL,
  expires_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, result TEXT);
"""


class Store:
    """One selected SQL backend and one atomic, independently checkpointed authority."""

    def __init__(
        self,
        path: str | Path,
        *,
        recovery_path=None,
        settings=None,
        service="memory",
        storage_environ=None,
    ):
        from tianshu_storage import FileStore, StorageOptions, StorageRuntime

        from .postgres import dialect, prepare, prepare_grants

        path = Path(path).resolve()
        if str(path).startswith("\\\\"):
            raise ValueError("SQLite requires a local file, not a network share")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.recovery_path = Path(recovery_path or (str(path) + ".source-guard.json")).resolve()
        if self.recovery_path == path or str(self.recovery_path).startswith("\\\\"):
            raise ValueError("Recovery checkpoint must be a distinct local file")
        settings = settings or {}
        state_directory = Path(str(path) + ".storage")

        def check_authority():
            # SQLite historically allowed both entrypoints to share a file. PG
            # must not clone that one authority into two service schemas.
            if service not in {"memory", "knowledge"}:
                return
            from .domain import strict_json

            other = "knowledge" if service == "memory" else "memory"
            marker = state_directory / f".tianshu-storage-{other}.json"
            if marker.exists():
                state = strict_json(marker.read_bytes())
                if not isinstance(state, dict):
                    raise ValueError("Shared storage authority state is invalid")
                if state.get("backend") == "postgresql" and state.get("state") in {
                    "pending",
                    "active",
                }:
                    raise ValueError(
                        "Shared Memory/Knowledge database path already has another PostgreSQL "
                        "authority; use distinct database_path/state directories"
                    )

        check_authority()
        self.files = FileStore(settings.get("knowledge_files_directory") or (str(path) + ".files"))
        self.runtime = StorageRuntime(
            StorageOptions.from_settings(settings, environ=storage_environ),
            service=service,
            state_directory=state_directory,
        )
        self.role_database = None
        try:
            self.runtime.start()
            check_authority()
            self.database = self.runtime.database(
                "main",
                path,
                prepare=lambda db, source: prepare(
                    db,
                    source,
                    files=self.files,
                    recovery_path=self.recovery_path,
                    state_directory=self.runtime.state_directory,
                    epoch=self.runtime.epoch,
                ),
                dialect=dialect,
            )
            grant_path = settings.get("role_grants_database_path")
            if grant_path:
                if not Path(grant_path).is_absolute():
                    raise ValueError("role_grants_database_path must be absolute")
                self.role_database = self.runtime.database(
                    "role_grants",
                    grant_path,
                    prepare=prepare_grants,
                    dialect=dialect,
                )
            self.backend = self.database.backend
            self._initialize()
            if self.backend == "sqlite":
                self._migrate_files()
            self.runtime.finalize()
        except BaseException:
            self.runtime.close()
            raise

    @classmethod
    def from_config(cls, config, *, service="memory", storage_environ=None):
        return cls(
            config["database_path"],
            recovery_path=config.get("source_sync", {}).get("recovery_path"),
            settings=config,
            service=service,
            storage_environ=storage_environ,
        )

    def _initialize(self):
        with self.transaction() as db:
            existing = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if existing:
                version = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if not version or version[0] not in {"1", "2", "3"}:
                    raise ValueError("Database schema mismatch; explicit migration required")
            # Statements are fixed, no user SQL and no executescript implicit transaction commit.
            if self.backend == "sqlite":
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema','1')")

    def _connect(self):
        options = {"isolation_level": None, "timeout": 5}
        if self.backend == "sqlite":
            options["factory"] = LocalConnection
        db = self.database.connect(**options)
        db.row_factory = sqlite3.Row
        db.knowledge_files = self.files
        return db

    def readonly(self):
        if self.backend == "sqlite":
            db = sqlite3.connect(Path(self.path).as_uri() + "?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            return db
        db = self.database.connect(timeout=2, isolation_level=None, read_only=True)
        db.execute("BEGIN READ ONLY")
        return db

    def close(self):
        self.runtime.close()

    def backup(self, path, *, db=None):
        """Consistent migration restore evidence, never connect old SQLite after PG activation."""
        if self.backend == "sqlite":
            with (
                closing(sqlite3.connect(self.path)) as reader,
                closing(sqlite3.connect(path)) as target,
            ):
                reader.backup(target)
            return
        from .domain import canonical
        from .postgres import installed_tables

        if db is None:
            with self.transaction() as connection:
                return self.backup(path, db=connection)
        snapshot = {"format": "tianshu-memory-postgresql-backup/v1", "tables": {}}
        for table in installed_tables(db):
            cursor = db.execute(f"SELECT * FROM {table}")
            names = [c[0] for c in cursor.description]
            # Binary vectors are a rebuildable copy of the retained JSON column.
            # Keep backups JSON-compatible and independently restorable without a codec.
            rows = [
                [
                    None if table == "memory_vectors" and name == "vector_bytes" else value
                    for name, value in zip(names, row, strict=True)
                ]
                for row in cursor
            ]
            snapshot["tables"][table] = {"columns": names, "rows": rows}
        from .source_recovery import checkpoint

        snapshot["guard"] = checkpoint(db)
        with Path(path).open("wb") as output:
            output.write(canonical(snapshot).encode())
            output.flush()
            os.fsync(output.fileno())

    def _migrate_files(self):
        from .knowledge_files import externalize
        from .source_recovery import checkpoint, persist

        with self.transaction() as db:
            installed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='knowledge_versions' AND type='table'"
            ).fetchone()
            if (
                not installed
                or db.execute(
                    "SELECT 1 FROM metadata WHERE key='knowledge_files_schema'"
                ).fetchone()
            ):
                return
            backup = Path(self.path + ".pre-knowledge-files-" + uuid4().hex + ".bak")
            with backup.open("xb"):
                pass
            self.backup(backup, db=db)
            expected = checkpoint(db)
            if expected is not None:
                persist(Path(str(backup) + ".source-guard.json"), expected, initialize=True)
            externalize(db)

    def migrate_profiles(self, backup_path):
        """Explicit additive migration. Stop writers; retain the complete pre-migration DB.

        Rollback requires restoring this backup after stopping all writers. Never downgrade
        schema 2 in place: doing so would discard subsequently approved profile projections.
        """
        backup = Path(backup_path).resolve()
        if str(backup).startswith("\\\\") or backup == Path(self.path):
            raise ValueError("Backup must be a distinct local file")
        backup.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive reservation prevents accidental overwrite of a prior rollback artifact.
        with backup.open("xb"):
            pass
        with self.transaction() as db:
            if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != "1":
                raise ValueError("Profile migration requires schema 1")
            # A separate reader sees the committed snapshot while BEGIN IMMEDIATE blocks writers.
            self.backup(backup, db=db)
            for statement in PROFILE_SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("UPDATE metadata SET value='2' WHERE key='schema'")
        return {"schema": 2, "backup": str(backup)}

    @staticmethod
    def require_profiles(db):
        if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] not in {
            "2",
            "3",
        }:
            from .domain import Fault

            raise Fault("dependency_unavailable", 503)

    def migrate_sources(self, backup_path, contracts):
        from .source_migration import migrate

        return migrate(self, backup_path, contracts)

    def migrate_observations(self, backup_path):
        """Explicit guarded observation ledger migration; never auto-open old data."""
        from uuid import uuid4

        backup = Path(backup_path).resolve()
        if str(backup).startswith("\\\\") or backup in {Path(self.path), self.recovery_path}:
            raise ValueError("Backup must be a distinct local file")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        with self.transaction() as db:
            if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != "3":
                raise ValueError("Observations require guarded schema 3")
            if db.execute("SELECT 1 FROM metadata WHERE key='observation_schema'").fetchone():
                raise ValueError("Observation ledger already migrated")
            self.backup(backup, db=db)
            db.execute("""CREATE TABLE observation_sources (
                source_ref TEXT PRIMARY KEY, digest TEXT NOT NULL,
                instance_id TEXT NOT NULL, self_id TEXT NOT NULL,
                conversation TEXT NOT NULL, author TEXT NOT NULL,
                event_id TEXT NOT NULL, scope_version INTEGER NOT NULL,
                archive_epoch INTEGER NOT NULL,
                sent_at TEXT NOT NULL, content_state TEXT NOT NULL,
                text TEXT NOT NULL, mentioned INTEGER NOT NULL,
                state TEXT NOT NULL, received_at REAL NOT NULL,
                UNIQUE(instance_id,self_id,conversation,author,event_id)
            )""")
            db.execute(
                "CREATE INDEX observation_page ON observation_sources("
                "instance_id,self_id,conversation,received_at,source_ref)"
            )
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_observation_sources_{action} "
                    f"AFTER {action} ON observation_sources "
                    "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                    "WHERE key='source_revision'; END"
                )
            db.execute("INSERT INTO metadata VALUES ('observation_schema','1')")
            db.execute("INSERT INTO metadata VALUES ('observation_instance',?)", (uuid4().hex,))
        return {"schema": 3, "observation_schema": 1, "backup": str(backup)}

    def migrate_source_lookup(self, backup_path):
        from .source_lookup_migration import migrate

        return migrate(self, backup_path)

    def migrate_users(self, backup_path):
        from .user_migration import migrate

        return migrate(self, backup_path)

    def migrate_facts(self, backup_path):
        from .fact_migration import migrate

        return migrate(self, backup_path)

    def migrate_memory(self, backup_path):
        from .memory_migration import migrate

        return migrate(self, backup_path)

    def migrate_segments(self, backup_path):
        from .segment_migration import migrate

        return migrate(self, backup_path)

    def migrate_context(self, backup_path):
        from .memory_context.migration import migrate

        return migrate(self, backup_path)

    def migrate_retrieval(self, backup_path):
        from .retrieval_migration import migrate

        return migrate(self, backup_path)

    def migrate_partitions(self, backup_path):
        from .partition_migration import migrate

        return migrate(self, backup_path)

    def migrate_rotation(self, backup_path):
        from .target_rotation import migrate

        return migrate(self, backup_path)

    def migrate_merge_candidates(self, backup_path):
        from .merge_candidates import migrate

        return migrate(self, backup_path)

    def migrate_events(self, backup_path):
        from .memory_events import migrate

        return migrate(self, backup_path)

    def migrate_qq_aliases(self, backup_path):
        from .qq_alias_migration import migrate

        return migrate(self, backup_path)

    def migrate_relationships(self, backup_path, *, clock, policy=None):
        from .relationship_migration import migrate
        from .relationships.policy import Policy

        return migrate(self, backup_path, clock=clock, policy=policy or Policy())

    def migrate_lessons(self, backup_path):
        from .lessons_migration import migrate as migrate_lessons

        return migrate_lessons(self, backup_path)

    def migrate_research_notes(self, backup_path):
        from .research_notes_migration import migrate as migrate_research_notes

        return migrate_research_notes(self, backup_path)

    @staticmethod
    def require_user_actions(db):
        from .domain import require

        row = db.execute("SELECT value FROM metadata WHERE key='local_users_schema'").fetchone()
        require(row is not None and row[0] == "1", "dependency_unavailable", 503)

    @contextmanager
    def transaction(self):
        from . import source_recovery

        db = self._connect()
        prepared = False
        try:
            if self.backend == "sqlite":
                db.execute("PRAGMA foreign_keys=ON")
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise RuntimeError("SQLite WAL unavailable")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
            else:
                # Queue setup only. Exiting confirms BEGIN and the dialect's
                # authority lock before any metadata snapshot can be acquired.
                with db.pipeline():
                    db.native("SET synchronous_commit=on")
                    db.execute("BEGIN IMMEDIATE")
            if self.backend == "postgresql":
                from .domain import Fault

                # A separate statement AFTER the advisory lock has returned is
                # essential under READ COMMITTED: its snapshot must see the last
                # writer's commit, including the independently persisted guard.
                metadata = dict(db.native("SELECT key,value FROM metadata"))
                if metadata.get("postgres_memory_schema") != "1":
                    raise Fault("dependency_unavailable", 503)
                before = source_recovery.from_metadata(metadata)
            else:
                before = source_recovery.checkpoint(db)
            if self.backend == "postgresql" and before is None and self.recovery_path.exists():
                # Restoring a pre-guard schema cannot bypass an independently
                # retained checkpoint, even while this process is still alive.
                raise Fault("dependency_unavailable", 503)
            if before is not None:
                source_recovery.verify(self.recovery_path, before)
            yield db
            after = (
                source_recovery.from_metadata(dict(db.native("SELECT key,value FROM metadata")))
                if self.backend == "postgresql"
                else source_recovery.checkpoint(db)
            )
            if after is not None and after != before:
                prepared = True
                source_recovery.persist(self.recovery_path, after, initialize=before is None)
            db.commit()
        except BaseException:
            db.rollback()
            if prepared:
                logging.getLogger(__name__).error(
                    "source_checkpoint_commit_interrupted: checkpoint may be ahead; recovery review required"
                )
            raise
        finally:
            db.close()
