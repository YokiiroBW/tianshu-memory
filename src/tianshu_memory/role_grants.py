"""Exact persisted actor admissions for the registered Platform role administrator."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .domain import require


class RoleGrants:
    def __init__(self, path):
        require(Path(path).is_absolute(), "invalid_input", 400)
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS grants (actor_id TEXT PRIMARY KEY, version INTEGER NOT NULL, enabled INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations (request_id TEXT PRIMARY KEY, signature TEXT NOT NULL, result TEXT NOT NULL)"
            )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def active(self, actor_id):
        return self.decision(actor_id) is True

    def decision(self, actor_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT enabled FROM grants WHERE actor_id=?", (actor_id,)).fetchone()
        return None if row is None else row[0] == 1

    def inactive_ids(self):
        with closing(self._connect()) as db:
            return [row[0] for row in db.execute(
                "SELECT actor_id FROM grants WHERE enabled=0 ORDER BY actor_id"
            )]

    def status(self, actor_id):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT version,enabled FROM grants WHERE actor_id=?", (actor_id,)
            ).fetchone()
        return {
            "actor_id": actor_id,
            "version": row[0] if row else 0,
            "enabled": bool(row and row[1] == 1),
        }

    def active_ids(self):
        with closing(self._connect()) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT actor_id FROM grants WHERE enabled=1 ORDER BY actor_id"
                )
            ]

    def apply(self, body, static_actors):
        require(
            isinstance(body, dict)
            and set(body) == {"request_id", "actor_id", "expected_version", "enabled", "legacy"},
            "invalid_input",
            400,
        )
        actor = body["actor_id"]
        require(
            isinstance(actor, str)
            and re.fullmatch(r"actor:[A-Za-z0-9._:-]{1,122}", actor) is not None
            and type(body["legacy"]) is bool
            and body["legacy"] is (actor in static_actors)
            and isinstance(body["request_id"], str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", body["request_id"]) is not None
            and type(body["expected_version"]) is int
            and body["expected_version"] >= 0
            and type(body["enabled"]) is bool,
            "invalid_input",
            400,
        )
        semantic = json.dumps(body, sort_keys=True, separators=(",", ":"))
        signature = hashlib.sha256(semantic.encode()).hexdigest()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT signature,result FROM operations WHERE request_id=?", (body["request_id"],)
            ).fetchone()
            if prior:
                require(prior[0] == signature, "idempotency_conflict", 409)
                return json.loads(prior[1])
            row = db.execute("SELECT version FROM grants WHERE actor_id=?", (actor,)).fetchone()
            current = row[0] if row else 0
            require(current == body["expected_version"], "version_conflict", 409)
            result = {"actor_id": actor, "version": current + 1, "enabled": body["enabled"]}
            db.execute(
                "INSERT INTO grants VALUES (?,?,?) ON CONFLICT(actor_id) DO UPDATE SET version=excluded.version,enabled=excluded.enabled",
                (actor, current + 1, int(body["enabled"])),
            )
            db.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (body["request_id"], signature, json.dumps(result, sort_keys=True)),
            )
            db.commit()
        return result
