"""Explicit addon tables; every authority mutation joins the source checkpoint."""

VERSION = "1"
TRACKED = (
    "relationship_pairs",
    "relationship_events",
    "relationship_commands",
    "relationship_legacy",
    "relationship_sources",
    "relationship_freezes",
)
SCHEMA = """
CREATE TABLE relationship_pairs (
 actor_id TEXT NOT NULL, person_id TEXT NOT NULL REFERENCES people(id),
 relationship_type TEXT NOT NULL, display_label TEXT NOT NULL,
 score INTEGER NOT NULL, stage TEXT NOT NULL, frozen INTEGER NOT NULL,
 frozen_since TEXT, version INTEGER NOT NULL, policy TEXT NOT NULL,
 activity_at TEXT NOT NULL, decay_days INTEGER NOT NULL, decay_cursor TEXT NOT NULL,
 clock_head TEXT NOT NULL, ready INTEGER NOT NULL,
 PRIMARY KEY(actor_id,person_id)
);
CREATE TABLE relationship_events (
 event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, actor_id TEXT NOT NULL,
 person_id TEXT NOT NULL, kind TEXT NOT NULL, delta INTEGER NOT NULL,
 outcome TEXT NOT NULL, settled_at TEXT NOT NULL, budget_day TEXT,
 result TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(actor_id,person_id) REFERENCES relationship_pairs(actor_id,person_id)
);
CREATE INDEX relationship_event_page ON relationship_events(actor_id,person_id,settled_at,event_id);
CREATE INDEX relationship_daily_budget ON relationship_events(actor_id,person_id,budget_day);
CREATE TABLE relationship_sources (
 event_id TEXT NOT NULL REFERENCES relationship_events(event_id),
 source_key TEXT NOT NULL REFERENCES sources(key), revision INTEGER NOT NULL,
 epoch INTEGER NOT NULL, scope TEXT NOT NULL,
 PRIMARY KEY(event_id,source_key)
);
CREATE INDEX relationship_source_lookup ON relationship_sources(source_key,event_id);
CREATE TABLE relationship_commands (
 operator TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
 actor_id TEXT NOT NULL, person_id TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(operator,request_id),
 FOREIGN KEY(actor_id,person_id) REFERENCES relationship_pairs(actor_id,person_id)
);
CREATE TABLE relationship_legacy (
 group_id TEXT PRIMARY KEY REFERENCES relationship_entries(group_id),
 actor_id TEXT, person_id TEXT, amount INTEGER NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE relationship_freezes (
 freeze_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, person_id TEXT NOT NULL,
 started_at TEXT NOT NULL, ended_at TEXT,
 FOREIGN KEY(actor_id,person_id) REFERENCES relationship_pairs(actor_id,person_id)
);
CREATE UNIQUE INDEX relationship_active_freeze ON relationship_freezes(actor_id,person_id) WHERE ended_at IS NULL;
"""


def installed(db):
    row = db.execute("SELECT value FROM metadata WHERE key='relationships_schema'").fetchone()
    return row is not None and row[0] == VERSION


def install(db):
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)
    for table in TRACKED:
        for action in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER source_revision_{table}_{action} AFTER {action} ON {table} "
                "BEGIN UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='source_revision'; END"
            )
    db.execute("INSERT INTO metadata VALUES ('relationships_schema',?)", (VERSION,))
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='source_revision'")
