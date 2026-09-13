"""Explicit schema 2 -> 3 migration; no inferred owner attestations."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .domain import admission_key, canonical, source_key, source_selector

SCHEMA = """
CREATE TABLE physical_sources (
  key TEXT PRIMARY KEY, payload TEXT, revision INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE source_admissions (
  key TEXT PRIMARY KEY REFERENCES sources(key), physical_key TEXT NOT NULL REFERENCES physical_sources(key),
  actor_id TEXT NOT NULL, selector TEXT NOT NULL UNIQUE, payload TEXT, verified INTEGER NOT NULL DEFAULT 0);
CREATE INDEX admissions_physical ON source_admissions(physical_key);
CREATE TABLE suppression (source_key TEXT PRIMARY KEY REFERENCES sources(key), reason TEXT NOT NULL);
CREATE TABLE owner_heads (owner TEXT PRIMARY KEY, generation TEXT NOT NULL, sequence INTEGER NOT NULL);
CREATE TABLE source_observations (
  kind TEXT NOT NULL, key TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(kind,key));
ALTER TABLE confirmations ADD COLUMN binding_version INTEGER;
ALTER TABLE confirmations ADD COLUMN record_id TEXT;
ALTER TABLE confirmations ADD COLUMN expected_version INTEGER;
"""

# Every local dependency mutation participates in the barrier revision, including writes made
# by another service instance. metadata is deliberately excluded to avoid recursive triggers.
TRACKED = (
    "people accounts scopes requests sources groups records history lineage projections confirmations "
    "inbox turn_inputs aggregate_events jobs write_ledger source_writes relationship_entries "
    "outbox profile_shares profile_approvals physical_sources source_admissions suppression owner_heads source_observations"
).split()


def migrate(store, backup_path, contracts):
    backup = Path(backup_path).resolve()
    if str(backup).startswith("\\\\") or backup in {Path(store.path), store.recovery_path}:
        raise ValueError("Backup must be a distinct local file")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb"):
        pass
    with store.transaction() as db:
        if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != "2":
            raise ValueError("Source migration requires schema 2; migrate profiles first")
        with (
            closing(sqlite3.connect(store.path)) as reader,
            closing(sqlite3.connect(backup)) as dest,
        ):
            reader.backup(dest)
        # Validate every historical ownership edge before changing a key. Fail atomically on
        # mixed legacy ownership; the caller can retain/quarantine that DB for manual review.
        mapping = {}
        rows = db.execute("SELECT * FROM sources").fetchall()
        for row in rows:
            source, scope = json.loads(row["payload"]), json.loads(row["scope"])
            contracts.validate("common#source", source)
            contracts.validate("common#scope", scope)
            if (
                source_key(source) != row["key"]
                or not db.execute(
                    "SELECT 1 FROM people WHERE id=?", (scope["person_id"],)
                ).fetchone()
            ):
                raise ValueError("Unproven legacy source ownership")
            for edge in db.execute(
                "SELECT g.* FROM lineage l JOIN groups g ON g.id=l.group_id WHERE l.source_key=?",
                (row["key"],),
            ):
                target = json.loads(edge["scope"])
                if "profile_subject" not in target and target != scope:
                    raise ValueError("Ambiguous legacy text scope lineage")
                if "profile_subject" in target:
                    share = db.execute(
                        "SELECT * FROM profile_shares WHERE group_id=?", (edge["id"],)
                    ).fetchone()
                    proof = (
                        None
                        if share is None
                        else db.execute(
                            "SELECT * FROM profile_approvals WHERE ref=?", (share["approval_ref"],)
                        ).fetchone()
                    )
                    if (
                        share is None
                        or proof is None
                        or not proof["consumed"]
                        or not proof["result"]
                        or json.loads(proof["result"])["group_id"] != edge["id"]
                        or row["key"] not in json.loads(proof["source_snapshot"])
                    ):
                        raise ValueError("Unproven legacy profile approval")
                    subject = {
                        "kind": share["subject_kind"],
                        "person_id"
                        if share["subject_kind"] == "person"
                        else "conversation_id": share["subject_id"],
                    }
                    expected = {
                        "actor_id": share["actor_id"],
                        "profile_audience": share["sharing"],
                        "conversation_id": share["conversation_id"]
                        if share["sharing"] == "group_only"
                        else None,
                        "profile_subject": subject,
                    }
                    if target != expected:
                        raise ValueError("Ambiguous legacy profile scope")
                if target.get("actor_id") != scope["actor_id"]:
                    raise ValueError("Ambiguous legacy actor lineage")
                person = target.get("person_id", target.get("profile_subject", {}).get("person_id"))
                if person is not None and person != scope["person_id"]:
                    raise ValueError("Ambiguous legacy person lineage")
            for edge in db.execute(
                "SELECT scope FROM source_writes WHERE source_key=?", (row["key"],)
            ):
                if json.loads(edge[0]) != scope:
                    raise ValueError("Ambiguous legacy source write ownership")
            mapping[row["key"]] = admission_key(source, scope)
        db.execute("PRAGMA defer_foreign_keys=ON")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        for row in rows:
            source, scope = json.loads(row["payload"]), json.loads(row["scope"])
            key = mapping[row["key"]]
            db.execute("UPDATE sources SET key=? WHERE key=?", (key, row["key"]))
            db.execute("UPDATE lineage SET source_key=? WHERE source_key=?", (key, row["key"]))
            db.execute(
                "UPDATE source_writes SET source_key=? WHERE source_key=?", (key, row["key"])
            )
            db.execute(
                "INSERT INTO physical_sources VALUES (?,NULL,?,'unverified')",
                (row["key"], row["revision"]),
            )
            db.execute(
                "INSERT INTO source_admissions VALUES (?,?,?,?,NULL,0)",
                (key, row["key"], scope["actor_id"], canonical(source_selector(source, scope))),
            )
            if row["state"] != "active":
                db.execute("INSERT INTO suppression VALUES (?, 'legacy_withdrawn')", (key,))
        for job in db.execute("SELECT * FROM jobs").fetchall():
            event, snapshot = json.loads(job["event"]), json.loads(job["source_snapshot"])
            for old in snapshot:
                source = next((r for r in rows if r["key"] == old), None)
                if source is None or json.loads(source["scope"]) != event["scope"]:
                    raise ValueError("Ambiguous legacy job ownership")
            db.execute(
                "UPDATE jobs SET source_snapshot=? WHERE id=?",
                (canonical({mapping[k]: v for k, v in snapshot.items()}), job["id"]),
            )
        # Profile approvals bind source keys too. Old confirmations lack binding_version and
        # remain deliberately unusable by TrustedWorkflow / production revise.
        for approval in db.execute("SELECT ref,source_snapshot FROM profile_approvals").fetchall():
            snapshot = json.loads(approval["source_snapshot"])
            db.execute(
                "UPDATE profile_approvals SET source_snapshot=? WHERE ref=?",
                (canonical({mapping[k]: v for k, v in snapshot.items()}), approval["ref"]),
            )
        db.execute("INSERT INTO metadata VALUES ('source_revision','0')")
        db.execute("INSERT INTO metadata VALUES ('source_instance',?)", (uuid4().hex,))
        db.execute("INSERT INTO metadata VALUES ('source_recovery','ready')")
        for table in TRACKED:
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} "
                    "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                    "WHERE key='source_revision'; END"
                )
        db.execute("UPDATE metadata SET value='3' WHERE key='schema'")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Source migration foreign key mismatch")
    return {"schema": 3, "backup": str(backup), "unverified_admissions": len(rows)}
