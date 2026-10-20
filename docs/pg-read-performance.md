# PostgreSQL recall and Knowledge reads

TS-248 starts from production Memory/Knowledge `e1da946ae534c1b2ce4365a97ee38b2260b96c65`.
It changes product read paths, with no schema migration, shared-driver change, Redis
result cache or production operation. SQLite remains the default backend.

## Memory

`recall_index.eligible_groups` reuses the whole-group validation introduced in Q15
`58a5a1bee4565f83ba31e8531df1f9a936f12567`, shared by `MemoryService.select` and the
memory-context query. PostgreSQL reads lineage for at most 128 groups, checks current
admissions and suppression in batches of 256 source keys, then reads records and
projections only for currently authorized groups. It preserves candidate order, exact
scope/person, active state, exact members, payload record ID/version/group, shared
projection ownership/version and complete-group budgets. Missing or inconsistent
members omit the whole group.

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
The following **new statement** reads metadata to validate the PG marker and derive
the before-checkpoint together. After the body, a fresh metadata read derives the
after-checkpoint. This replaces five reads with two; it never uses a snapshot from
before the lock or combines the lock with the read. The pure checkpoint conversion
is shared with SQLite, whose catalog/metadata SQL and persistence order are unchanged.

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

The final aggregate joins only query-term frequencies, while document lengths,
corpus size/average and document frequencies still use the complete authorized
corpus. A left join retains matching documents whose terms contribute zero score,
including the literal `OR` edge case. SQL returns only group IDs and scores; Python
reattaches the exact text from this call's authorized input instead of receiving
and grouping the full body again. PG lineage reads likewise return only group ID,
source key/state and current/required revision/epoch, which are the fields consumed
by both `SourceAuthority.current_many` and the fixture authority fallback.

PostgreSQL also batches automatic-fact metadata for the complete candidate set.
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

`tests/test_postgres_source_snapshot.py` adds real PG stable-probe/no-op and source
negative cases, owner rollback/generation, local CAS retry, an unrequested-physical
receipt collision, marker/checkpoint corruption and a 4,000-row synthetic index
plan. Ordinary ANALYZE, without planner setting changes, verifies the physical,
primary-key and partial receipt indexes while retaining a NULL-payload alias.
Stable synchronization over 30 historical sources executes 13 `_apply` statements
with zero mutations and leaves guard bytes/mtime unchanged. These are scoped test
observations, not production latency claims.

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
