# PostgreSQL recall and Knowledge reads

TS-248 starts from production Memory/Knowledge `e1da946ae534c1b2ce4365a97ee38b2260b96c65`.
It changes product read paths, with no schema migration, Redis result cache or
production operation. Shared-driver packaging remains coordinator-owned; SQLite
remains the default backend.

TS-256 follows the locally integrated `84c2f56`/shared 0.1.2 candidate in a new
worktree. Its source is `45d80619803a145bfa4c94108bb990d6868a17a3`. This phase keeps
the same transaction/authority boundaries and default SQLite behavior; coordinated
native PG verification and the new comparison belong to TS-257.

## Memory

`recall_index.eligible_groups` reuses the whole-group validation introduced in Q15
`58a5a1bee4565f83ba31e8531df1f9a936f12567`, shared by `MemoryService.select` and the
memory-context query. PostgreSQL reads lineage for at most 512 groups, checks current
admissions and suppression in batches of 256 source keys, then reads records and
projections only for currently authorized groups. It preserves candidate order, exact
scope/person, active state, exact members, payload record ID/version/group, shared
projection ownership/version and complete-group budgets. Missing or inconsistent
members omit the whole group.
Both backends serialize the requested scope once per nonempty validation call and
pass that exact value to the private unit validator; every group's scope equality,
record identity/version/group/person and shared projection check still runs. Empty
candidate lists do not serialize a scope or read content.

`SourceAuthority.current_many` applies the existing `current` rule to rows belonging
only to the current transaction. It does not retain a positive grant or replace the
owner barrier. `SourceAuthority.operation`, its owner lock, complete source coverage,
remote double collection, negative-fact commit, local revision comparison and
`Store.transaction`/independent source-guard ordering are unchanged.

The barrier's PG `_apply` reads observations, physicals, admissions, bindings,
sources, suppression and owner heads into a transaction-local `SourceSnapshot`.
All original validation branches still run. Receipt lookup covers matching receipts
across the entire local ledger, including unrelated actors and physicals; physical
aliases with NULL legacy payloads remain in negative broadcasts. Only writes that
the original `ON CONFLICT ... WHERE` would leave unchanged are skipped. A physical
negative updates prefetched current source state/epoch before the admission loop,
matching the database update. No observation or grant survives this transaction.

PG transaction setup still acquires the authority advisory lock in its own statement.
The SET/BEGIN/advisory-lock statements are queued in the shared 0.1.2 connection's
explicit `pipeline()` context, whose successful exit confirms all setup commands
and the lock. No setup result is consumed within it. SQLite does not enter a pipeline.
The following **new statement** reads metadata to validate the PG marker and derive
the before-checkpoint together. After the body, a fresh metadata read derives the
after-checkpoint. This replaces five reads with two; it never uses a snapshot from
before the lock or combines the lock with the read. The pure checkpoint conversion
is shared with SQLite, whose catalog/metadata SQL and persistence order are unchanged.

TS-256 also folds each PG `SourceAuthority.revision` check's four read statements
into one fresh statement. It reads `source_lookup_schema`, `source_revision` and
`source_recovery`, plus the two required index names in the current logical schema.
Validation still runs in marker, index-count, revision/recovery and integer-conversion
order. All six normal checks remain, including the separate coverage checks and
the post-`_apply` read; no Store-start metadata or earlier result replaces them.
Index-name queries use `pg_class` with the current namespace and index relation
kinds, so an index in another schema or an ordinary table cannot satisfy readiness.
They add no new index-validity/definition rule. No catalog or authority result is
cached, including across two calls after DML/DDL in the same transaction. SQLite
keeps its original four read statements per revision check.

SQLite keeps the earlier lazy order: source check, record read, projection read. It
does not prefetch invalid sources' payloads, add admission batch queries or replace
its FTS5 ranking. Zero-budget and exact-field/item filtering still happen before
materialization. The common integrity validator adds no SQL.

`postgres_recall.rank` sends the whole authorized corpus in one JSON parameter to a
native, parameterized SQL statement. Materialized CTEs compute each document vector,
term frequency, document length, corpus size/average length and query-term document
frequency once. The BM25 constants/formula match the existing PostgreSQL
`memory_bm25`, including zero scores. Hidden or stale persistent search rows do not
contribute to these statistics. Python coverage, clause specificity, tie breaking
and whole-group fitting remain in their existing owner.
The materialized query terms now parse each `plainto_tsquery` once per term rather
than inside the document-frequency join. Compound lexemes keep that same parser
and matching predicate. The document CTE retains its ID/vector and no unused full
text column; every authorized input text still contributes to corpus statistics.

The final aggregate joins only query-term frequencies, while document lengths,
corpus size/average and document frequencies still use the complete authorized
corpus. A left join retains matching documents whose terms contribute zero score,
including the literal `OR` edge case. SQL returns only group IDs and scores; Python
reattaches the exact text from this call's authorized input instead of receiving
and grouping the full body again. PG lineage reads likewise return only group ID,
source key/state and current/required revision/epoch, which are the fields consumed
by both `SourceAuthority.current_many` and the fixture authority fallback.

PostgreSQL also batches automatic-fact metadata for the complete candidate set.
Its group and raw fact reads share a 512-ID bound (previously 128 on PG). Source
admission/suppression and projection reads retain their independent 256-key bound;
current authority is still checked before any group's record body is fetched.
The candidate set, order and complete-group checks are unchanged. SQLite does not
take these batch paths.
Replacement families retain exact scope/fact identity, depth 16 and the existing
128-family overflow refusal. Old evidence still passes its original source and
projection checks before contributing terms. SQLite keeps its indexed lazy fact
lookups. Existing persistent search GIN indexes and schema versions are unchanged.
`MemoryService.select` and memory-context query explicitly share one PG raw/decoded
fact snapshot between their current-fact/object filtering and rank preparation.
It belongs only to that select transaction; no service, connection or global cache
retains it. Later verification transactions keep their fresh authority/group reads.
SQLite returns no snapshot and keeps the earlier lazy queries and decoding path.

## Knowledge

`KnowledgeStorage` owns reusable **resources**, not query results or authorization.
The key includes all earlier storage-registration fields and effective shared
`StorageOptions`, including environment overrides. Only its digest is retained.
Every dispatch still rereads the private configuration and authorizes both phases.
A changed registration during one dispatch fails `registration_changed`; a later
dispatch retires the old Store and opens the new registration.

The HTTP server owns this provider through its existing lifespan mechanism, while
each request still creates its own `KnowledgeApplication`. Direct/MCP invocations
copy request state before executing so project, client and domain flags cannot
cross concurrent requests. Each Store transaction gets its own backend connection;
the provider never shares a transaction. CLI and MCP explicitly close their owners.

A lease spans the complete `execute`, including filesystem work outside SQL and the
final transaction. Configuration replacement or lifespan shutdown retires a cached
owner; an active lease closes it only after its real work ends. A previously admitted
worker that first reaches storage after shutdown gets an uncached short-lived owner,
preserving late completion and idempotent replay. HTTP admission slots and timeout/
disconnect handling are unchanged.

PostgreSQL keyword search returns document/version/block/provenance proof in the
same ranked-row query. It still restricts the exact project, ready/current document
version and exact block/version/hash. Both phases call the existing live source
reader, and block text still goes through verified FileRef reads. There is no file
freshness, project revision, seal-key or result cache across dispatches. SQLite keeps
its existing lazy reference SQL. Imports, deletions, grant withdrawal, changed roots,
replaced files and derived-file corruption therefore remain current authority events.

PG's project snapshot reads only `knowledge_schema` and `knowledge_seal_key`, joined
to the current project registration/revision in one statement. The aggregate keeps
a row when either metadata key or the project is absent, so the existing dependency,
initialization and registration fault checks retain their order. Each capture and
serve phase issues this query again and derives the client-bound seal key again;
there is no snapshot cache. SQLite keeps its original metadata/project statements
and metadata-fault short circuit.

## Validation and comparison

`tests/test_pg_read_performance.py` checks SQLite stale-source short circuits,
admission/suppression equivalence, PG BM25 scores against the original native
function, bounded PostgreSQL statement counts, ten incomplete-group/projection
cases, hot-store revocation and file checks, configuration/database changes,
HTTP resource reuse, concurrent request fields and shutdown with a live query.
PostgreSQL cases require an explicit isolated `TIANSHU_TEST_POSTGRES_URL`; absence
skips them and is never reported as PostgreSQL verification.

`tests/test_recall_preparation.py` locally checks the final aggregate against the
original formula, including unrelated/empty corpus rows and zero-score matches;
ID-only score results recover current input text. SQLite-backed branch doubles
check single-read/single-decode fact preparation, fresh subsequent captures and
both reduced-lineage authority paths. These tests do not execute native PG FTS,
driver adaptation or authority transactions and do not replace the native suite.
`tests/test_read_setup.py` likewise uses only local SQLite SQL doubles and a fake
pipeline: it checks snapshot result/fault equivalence, repeated revision/seal/
registration reads, setup-error rollback and fresh metadata strictly after pipeline
exit, with checkpoint persistence before commit. Native lock/wait/error behavior
and the fixed shared 0.1.2 package still need coordinator-run PostgreSQL validation.

`tests/test_postgres_source_snapshot.py` adds real PG stable-probe/no-op and source
negative cases, owner rollback/generation, local CAS retry, an unrequested-physical
receipt collision, marker/checkpoint corruption and a 4,000-row synthetic index
plan. Ordinary ANALYZE, without planner setting changes, verifies the physical,
primary-key and partial receipt indexes while retaining a NULL-payload alias.
The earlier TS-248 synchronization measurement over 30 historical sources used
13 `_apply` statements
with zero mutations and leaves guard bytes/mtime unchanged. These are scoped test
observations, not production latency claims.

`tests/test_pg_followup.py` checks combined readiness with local SQLite SQL/catalog
doubles, fault precedence, DML/DDL/rollback freshness, the unchanged SQLite query
shape, and 513-group/fact batch boundaries. Its additional native PG case verifies
the group boundary using a real selected backend. The source-snapshot suite adds
native checks for all six fresh probes, trigger-driven revision changes, a dropped/
renamed index, an index in another schema, an ordinary same-name table, readiness
damage and rollback. The BM25 oracle also covers compounds, quoted multiword terms,
Unicode and OR. Skipped native cases or local doubles are not native PG evidence.

Use the same synthetic seed, contract bytes, file tree, budgets and query sequence
for baseline SQLite, baseline PostgreSQL, candidate SQLite and candidate PostgreSQL.
Compare cold application/server creation separately from repeated calls. Check
result IDs, citations, hashes, version, complete groups and budget before recording
latency. Keep persistence, connection limits and resources equal, alternate order,
and report per-round p50/p95/throughput across three rounds. Include real
`SourceAuthority` with synthetic owner responses as well as the fixture-source
workload; the latter omits production barrier cost.

Statement-count/score tests are correctness evidence, not a speed claim. Same-NAS
measurements and SQLite non-regression acceptance belong to the coordinator's
TS-249 report. Production data, media, configuration and running services are
outside this implementation's validation scope.

TS-256 source `45d8061` passed its full local SQLite collection with **1540 passed,
97 skipped, one inherited account-authority 403/400 mismatch and no setup errors**.
The coordinator's fixed-0.1.2 PG-enabled related collection passed **159/159**:
96 native PG cases and 63 local/SQL-double cases, rather than 159 native cases.
The separate TS-257 three-round comparison was still running at the TS-256 handoff;
statement counts and those correctness results establish no performance-parity claim.

## TS-264 short reads, synchronization and writes

`Contracts` prepares the official referencing registry with `crawl()` and retains
one official Draft 2020-12 validator per qualified reference in that registry.
Every call still validates the complete current input. The cache contains rules,
format-checker preparation and validators only. Verified schema trees are frozen
after loading; supported `load_*` calls replace the immutable registry and invalidate
preparation. In-place edits to those verified schema dictionaries are not a hot
contract-update API. A registry replaced during preparation cannot be mislabeled
as the registry that was crawled. Validation uses the same installed format checkers
as a fresh validator, including the environment's optional-checker availability.

The PostgreSQL scope bump reads current content/history/input-history state in one
statement and obtains the new scope version from `UPSERT RETURNING`. It keeps the
same two mutations and per-row revision triggers, but uses three product statements
instead of seven. SQLite retains its existing scope/history SQL. Retained history
still fails conservatively after an unclassified write; missing or invalid history
has the same fallback/error behavior, and the next bump reads again after DML or
rollback. Index writing reads the current automatic-fact/evidence text once per
`_index` call instead of twice. It does not retain that text across calls or writes.

`SourceSnapshot` submits its existing independent SELECTs together through the
TS-269 shared `read_batch` API. This requires that API in the assembled storage
dependency; pin, lock and vendor changes belong to the coordinator. All results
are confirmed before consumption, after the existing fresh revision/coverage CAS
and before the first mutation. The seven original SQL statements, their domains
and order remain; empty-key lookups keep their original skips. Full receipt-collision
coverage, NULL-payload aliases, cross-actor withdrawal and in-transaction epoch
updates are unchanged. Setup pipelining, six fresh revision checks, lock ordering,
guard-before-commit, durability, and the after-checkpoint are unchanged.

`tests/test_contract_preparation.py` compares published documents and invalid inputs
against fresh official validators, including error paths, installed formats,
missing references, registry/load replacement and concurrent validation.
`tests/test_write_preparation.py` compares history epochs/errors with the original
SQLite SQL path and checks per-call fact/evidence text freshness. Its one native
PG test requires the explicit isolated test database. `tests/test_source_read_batch.py`
uses a confirmed-result double; it is not PG transaction evidence. The existing
native source-snapshot suite now counts statements inside read batches too.

Performance parity and SQLite write non-regression require the separate TS-265
three-round comparison and paired real-commit write control. Local SQL doubles and
fewer statements establish neither latency nor PostgreSQL correctness by themselves.
