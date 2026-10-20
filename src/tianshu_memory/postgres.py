"""Memory's PostgreSQL schema, import and the finite product SQL dialect.

The shared driver owns connections and backend binding. This module owns every native
table/index, the authority mutation trigger and retrieval over an authorized temporary
corpus. JSON remains canonical text at the application boundary; PG operates on JSONB.
"""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

from . import knowledge_files, source_recovery
from .domain import canonical

SCHEMA = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
TABLES = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
SEARCH_TABLES = {
    "search_index",
    "knowledge_index",
    "lesson_index",
    "experience_index",
    "research_note_index",
}
ORDERED = {"groups", "jobs", "automatic_evidence", "relationship_events"}

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION memory_source_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 UPDATE metadata SET value=(value::bigint+1)::text WHERE key='source_revision';
 RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION memory_bm25(document text, expression text) RETURNS double precision
LANGUAGE plpgsql AS $$
DECLARE
 term text; total double precision; average_length double precision;
 length double precision; frequency double precision; documents double precision;
 score double precision := 0;
BEGIN
 SELECT count(*), avg((SELECT coalesce(sum(cardinality(positions)),0)
   FROM unnest(to_tsvector('simple',a.text)))) INTO total,average_length
 FROM pg_temp.allowed_search a;
 SELECT coalesce(sum(cardinality(positions)),0) INTO length
 FROM unnest(to_tsvector('simple',document));
 FOR term IN SELECT unnest(tsvector_to_array(to_tsvector('simple',
   regexp_replace(expression,'"|\mOR\M',' ','g')))) LOOP
   SELECT coalesce(sum(cardinality(positions)),0) INTO frequency
   FROM unnest(to_tsvector('simple',document)) WHERE lexeme=term;
   SELECT count(*) INTO documents FROM pg_temp.allowed_search
   WHERE to_tsvector('simple',text) @@ plainto_tsquery('simple',term);
   IF frequency>0 AND average_length>0 THEN
     score := score + greatest(0.000001,ln((total-documents+0.5)/(documents+0.5)))
       * frequency*2.2/(frequency+1.2*(0.25+0.75*length/average_length));
   END IF;
 END LOOP;
 RETURN -score;
END $$;
"""


def is_postgres(db):
    return hasattr(db, "native")


def _json_path(match):
    column, path = match.groups()
    keys = re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\d+", path)
    return "(" + column + "::jsonb #>> '{" + ",".join(keys) + "}')"


def dialect(db, sql, parameters):
    """Adapt only syntax emitted by this product; unsupported SQL still fails in PG."""
    from tianshu_storage import MemoryCursor

    original = sql
    sql = sql.strip().rstrip(";")
    if sql.upper() == "BEGIN IMMEDIATE":
        db.native("BEGIN")
        # One schema-wide writer serializes the independent file checkpoint with all
        # authority changes, including a competing process on another connection.
        return db.native(
            "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':memory-authority',0))"
        )
    if sql.startswith("PRAGMA "):
        pragma = sql[7:].lower().replace(" ", "")
        if pragma in {"foreign_keys=on", "synchronous=full", "busy_timeout=5000"}:
            return MemoryCursor()
        if pragma == "defer_foreign_keys=on":
            return db.native("SET CONSTRAINTS ALL DEFERRED")
        if pragma == "journal_mode=wal":
            return MemoryCursor([("wal",)], columns=["journal_mode"])
        if pragma == "foreign_key_check":
            db.native("SET CONSTRAINTS ALL IMMEDIATE")
            return MemoryCursor()
        raise ValueError("Unsupported Memory PostgreSQL pragma")
    trigger = re.match(
        r"CREATE TRIGGER source_revision_\w+ AFTER (?:INSERT|UPDATE|DELETE) ON (\w+) ", sql
    )
    if trigger:
        table = trigger[1]
        if table not in TABLES:
            raise ValueError("Unknown PostgreSQL authority trigger")
        existing = db.native(
            "SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass(?) AND tgname=?",
            (table, "source_revision_" + table),
        ).fetchone()
        if not existing:
            db.native(
                f"CREATE TRIGGER source_revision_{table} AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION memory_source_revision()"
            )
        return MemoryCursor()
    created = re.match(r"CREATE (?:VIRTUAL )?TABLE (?:IF NOT EXISTS )?(\w+)\s*\(", sql)
    if created and created[1] in TABLES:
        native = next(
            s.strip()
            for s in SCHEMA.split(";")
            if re.match(r"CREATE TABLE IF NOT EXISTS " + created[1] + r"\s*\(", s.strip())
        )
        return db.native(native)
    if sql.startswith("CREATE VIRTUAL TABLE") and "temp.allowed_search" not in sql:
        table = re.search(r"TABLE (?:IF NOT EXISTS )?(\w+)", sql)[1]
        if table not in SEARCH_TABLES:
            raise ValueError("Unknown Memory PostgreSQL search corpus")
        return db.native(
            next(
                s.strip()
                for s in SCHEMA.split(";")
                if s.strip().startswith(f"CREATE TABLE IF NOT EXISTS {table} ")
            )
        )
    if sql.startswith("ALTER TABLE confirmations ADD COLUMN "):
        sql = sql.replace("ADD COLUMN ", "ADD COLUMN IF NOT EXISTS ").replace("INTEGER", "BIGINT")
    if "sqlite_master" in sql:
        if "type='table'" in sql:
            sql = sql.replace(
                "sqlite_master",
                "(SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema()) AS memory_catalog",
            )
            sql = sql.replace("type='table' AND ", "").replace("type='table'", "TRUE")
        elif "type='index'" in sql and "sql" not in sql.partition("FROM")[0].lower():
            sql = sql.replace(
                "sqlite_master",
                "(SELECT indexname AS name FROM pg_indexes WHERE schemaname=current_schema()) AS memory_catalog",
            )
            sql = sql.replace("type='index' AND ", "").replace("type='index'", "TRUE")
        else:
            raise ValueError("Unsupported Memory PostgreSQL catalog query")
    if sql.startswith("CREATE VIRTUAL TABLE temp.allowed_search USING fts5("):
        return db.native("CREATE TEMP TABLE allowed_search(group_id text,text text) ON COMMIT DROP")
    sql = sql.replace("temp.allowed_search", "pg_temp.allowed_search")
    sql = sql.replace("CAST(value AS INTEGER)", "CAST(value AS BIGINT)")
    sql = sql.replace(" IS NOT excluded.", " IS DISTINCT FROM excluded.")
    sql = sql.replace("COALESCE(SUM(MAX(delta,0)),0)", "COALESCE(SUM(GREATEST(delta,0)),0)::bigint")
    if "bm25(allowed_search)" in sql:
        sql = sql.replace("bm25(allowed_search)", "memory_bm25(text,?)")
        parameters = (parameters[0], *parameters)
    if "ORDER BY rank," in sql:
        corpus = re.search(
            r"FROM (knowledge_index|lesson_index|experience_index|research_note_index)\b", sql
        )[1]
        offset = sql.partition("ORDER BY rank,")[0].count("?")
        parameters = (*parameters[:offset], parameters[0], *parameters[offset:])
        sql = sql.replace(
            "ORDER BY rank,",
            f"ORDER BY -ts_rank_cd(to_tsvector('simple',{corpus}.text),websearch_to_tsquery('simple',?)),",
        )
    sql = re.sub(
        r"\b(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*) MATCH \?",
        lambda m: (
            "to_tsvector('simple',"
            + ((m[1] + ".") if m[1] else "")
            + (m[2] + ".text" if m[2] in SEARCH_TABLES | {"allowed_search"} else m[2])
            + ") @@ websearch_to_tsquery('simple',?)"
        ),
        sql,
    )
    sql = re.sub(r"json_extract\(([\w.]+),'\$([^']*)'\)", _json_path, sql)
    sql = re.sub(r"instr\((\([^)]*\)|[\w.]+),\?\)", r"strpos(\1,?)", sql)
    sql = sql.replace("json_each(?)", "jsonb_array_elements_text(?::jsonb) AS memory_values(value)")
    sql = sql.replace(
        "json_set(result,'$.audit',json(?))", "jsonb_set(result::jsonb,'{audit}',?::jsonb)::text"
    )
    sql = re.sub(r"\s+INDEXED BY \w+", "", sql)
    if sql.startswith("CREATE INDEX ") and "IF NOT EXISTS" not in sql:
        sql = sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
    if sql.startswith("CREATE UNIQUE INDEX ") and "IF NOT EXISTS" not in sql:
        sql = sql.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
    if sql.startswith("INSERT OR IGNORE INTO "):
        sql = sql.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1) + " ON CONFLICT DO NOTHING"
    if sql.startswith("INSERT INTO ") and "DO UPDATE SET" in sql:
        table = re.match(r"INSERT INTO (\w+)", sql)[1]
        sql = re.sub(
            r"\b(version|access_version)=\1\+1",
            lambda m: m[1] + "=" + table + "." + m[1] + "+1",
            sql,
        )
    if sql != original.strip().rstrip(";"):
        return db.native(sql, parameters)
    return None


def _rows_digest(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical(list(row)).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _migrate_row(db, table, columns, values):
    item = dict(zip(columns, values))
    if table == "knowledge_versions":
        raw = knowledge_files.read(db, item["raw"])
        text = knowledge_files.read(db, item["text"], text=True)
        if hashlib.sha256(bytes(raw)).hexdigest() != item["hash"]:
            raise ValueError("Knowledge original failed migration hash validation")
        item["raw"] = knowledge_files.put(db, bytes(raw), media_type=item["media_type"])
        item["text"] = knowledge_files.put(db, text, text=True)
    elif table == "knowledge_blocks":
        item["payload"] = knowledge_files.pack_block(db, knowledge_files.block(db, item["payload"]))
    elif table == "knowledge_content_uploads" and item["raw"] is not None:
        raw = knowledge_files.read(db, item["raw"])
        descriptor = json.loads(item["descriptor"])
        if (
            len(raw) != descriptor["size"]
            or hashlib.sha256(bytes(raw)).hexdigest() != descriptor["sha256"]
        ):
            raise ValueError("Knowledge upload failed migration hash validation")
        item["raw"] = knowledge_files.put(db, bytes(raw), media_type=descriptor["media_type"])
    elif table == "search_index":
        item["record_version"] = int(item["record_version"])
    return tuple(item[c] for c in columns)


def _fresh_instance(state_directory, epoch, recovery_path):
    """Resume a virgin, uncommitted target with the same independently reserved identity.

    Never infer an identity from a guard or overwrite a guard. The reservation belongs
    to the shared runtime's pending installation epoch, so an interrupted first commit
    can be retried while an unrelated or historically advanced guard still fails closed.
    """
    reservation = Path(state_directory) / "memory-authority-initialization.json"
    if reservation.exists():
        value = json.loads(reservation.read_bytes())
        if (
            set(value) != {"epoch", "instance"}
            or value["epoch"] != epoch
            or not re.fullmatch("[0-9a-f]{32}", value["instance"])
        ):
            raise ValueError("PostgreSQL authority initialization identity mismatch")
        return value["instance"]
    if Path(recovery_path).exists():
        raise ValueError("Existing authority checkpoint needs its initialization reservation")
    instance = uuid4().hex
    with reservation.open("xb") as output:
        output.write(canonical({"epoch": epoch, "instance": instance}).encode())
        output.flush()
        os.fsync(output.fileno())
    return instance


def prepare(db, source, *, files, recovery_path, state_directory, epoch):
    """Create/import one complete authority snapshot before shared activation can commit."""
    db.knowledge_files = files
    binding = db.native("SELECT state FROM __tianshu_storage_binding WHERE singleton=1").fetchone()
    exists = db.native("SELECT to_regclass(current_schema() || '.metadata')").fetchone()[0]
    if exists:
        version = db.native(
            "SELECT value FROM metadata WHERE key='postgres_memory_schema'"
        ).fetchone()
        if version is None or version[0] != "1":
            raise ValueError("PostgreSQL Memory schema is not owned by this build")
        installed = {
            r[0]
            for r in db.native("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")
        }
        if not set(TABLES) <= installed:
            raise ValueError("PostgreSQL Memory schema is incomplete; authority tables missing")
        return
    if binding[0] == "ready":
        raise ValueError(
            "PostgreSQL Memory authority metadata is missing; recovery review required"
        )
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.native(statement)
    db.native(FUNCTIONS)
    tables = set()
    if source is not None:
        source.row_factory = None
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "metadata" not in tables:
            raise ValueError("Memory source is not an initialized database")
        metadata = dict(source.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") not in {"1", "2", "3"}:
            raise ValueError("Unsupported Memory source schema")
        if metadata["schema"] == "3":
            source_recovery.verify(recovery_path, source_recovery.checkpoint(source))
        shadows = {
            index + suffix
            for index in SEARCH_TABLES
            for suffix in ("_data", "_idx", "_content", "_docsize", "_config")
        }
        unknown = tables - set(TABLES) - shadows - {"sqlite_sequence"}
        if unknown:
            raise ValueError("Unknown Memory source tables require a reviewed migration")
        for table in TABLES:
            if table not in tables:
                continue
            columns = [r[1] for r in source.execute(f"PRAGMA table_info({table})")]
            query = "SELECT " + ",".join('"' + c + '"' for c in columns)
            if table in ORDERED:
                columns.append("rowid")
                query += ",rowid"
            rows = [
                _migrate_row(db, table, columns, row)
                for row in source.execute(query + f' FROM "{table}" ORDER BY rowid')
            ]
            if rows:
                names = ",".join('"' + c + '"' for c in columns)
                db.executemany(
                    f'INSERT INTO "{table}"({names}) VALUES ({",".join("?" for _ in columns)})',
                    rows,
                )
                copied = db.native(
                    f'SELECT {names} FROM "{table}" ORDER BY '
                    + ("rowid" if table in ORDERED else ",".join('"' + c + '"' for c in columns))
                ).fetchall()
                # Compare the complete transformed rows, not merely counts. Sorting is in
                # Python too so database collation cannot make equivalent imports disagree.
                if _rows_digest(sorted(rows, key=canonical)) != _rows_digest(
                    sorted((tuple(r) for r in copied), key=canonical)
                ):
                    raise ValueError("Memory PostgreSQL import validation failed")
    else:
        metadata = {
            "schema": "3",
            "source_revision": "0",
            "source_instance": _fresh_instance(state_directory, epoch, recovery_path),
            "source_recovery": "ready",
            "source_lookup_schema": "1",
            "local_users_schema": "1",
            "observation_schema": "1",
            "observation_instance": uuid4().hex,
            "automatic_fact_schema": "1",
            "memory_context_schema": "1",
            "qq_alias_schema": "1",
            "relationships_schema": "1",
            "knowledge_schema": "1",
            "knowledge_content_schema": "1",
            "knowledge_catalog_schema": "1",
            "knowledge_directories_schema": "1",
            "lessons_schema": "1",
            "research_notes_schema": "1",
            "knowledge_seal_key": secrets.token_hex(32),
        }
        db.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
    db.native("INSERT INTO metadata VALUES ('postgres_memory_schema','1')")
    if "knowledge_files_schema" not in metadata:
        db.native("INSERT INTO metadata VALUES ('knowledge_files_schema','1')")
    for table in ("conflicts", "outbox", *ORDERED):
        column = "id" if table == "conflicts" else "position" if table == "outbox" else "rowid"
        db.native(
            f"SELECT setval(pg_get_serial_sequence('{table}','{column}'),coalesce(max({column}),0)+1,false) FROM {table}"
        )
    # Import does not execute mutation triggers, so source heads, consumed receipts,
    # suppression and logical versions remain exactly the original authority snapshot.
    for table in TABLES:
        if table not in {"metadata", "conflicts", *SEARCH_TABLES}:
            db.native(
                f"CREATE TRIGGER source_revision_{table} AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION memory_source_revision()"
            )
    if metadata.get("schema") == "3":
        expected = source_recovery.checkpoint(db)
        if source is None:
            if Path(recovery_path).exists():
                source_recovery.verify(recovery_path, expected)
            else:
                source_recovery.persist(recovery_path, expected, initialize=True)
        else:
            source_recovery.verify(recovery_path, expected)


def prepare_grants(db, source):
    binding = db.native("SELECT state FROM __tianshu_storage_binding WHERE singleton=1").fetchone()
    if binding[0] == "ready":
        # Revoked grants are authority. A missing table in an installed sidecar
        # must not be recreated as an empty book which would revive static roles.
        db.native("SELECT actor_id,version,enabled FROM grants LIMIT 0")
        db.native("SELECT request_id,signature,result FROM operations LIMIT 0")
        return
    db.native(
        "CREATE TABLE IF NOT EXISTS grants(actor_id text PRIMARY KEY,version bigint NOT NULL,enabled bigint NOT NULL)"
    )
    db.native(
        "CREATE TABLE IF NOT EXISTS operations(request_id text PRIMARY KEY,signature text NOT NULL,result text NOT NULL)"
    )
    if source is not None:
        for table in ("grants", "operations"):
            if source.execute(
                "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)
            ).fetchone():
                rows = [tuple(r) for r in source.execute(f"SELECT * FROM {table}")]
                db.executemany(f"INSERT INTO {table} VALUES (?,?,?)", rows)
                if sorted(rows) != sorted(tuple(r) for r in db.native(f"SELECT * FROM {table}")):
                    raise ValueError("Role admission import validation failed")
