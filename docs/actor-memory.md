# Actor episode domain

The actor domain is part of Memory, with independent storage identity `actor-memory`, logical
database `main`, metadata, restore guard, SQL lock, source watermarks, receipts and admission.
It does not create a bot person or use the user Store, records, jobs or source revision.
The domain implements the six episode operations and the four query/derived operations from
actor-self-memory candidate.4, plus the AL13 timeline and TS394 lifecycle operations.
Each granted operation must appear explicitly in the caller's
`actor_memory_operations`.

An actor endpoint is installed at `POST /internal/v1/actor-self-memory/operate`. Existing
service credentials are authenticated normally. A caller additionally needs the exact operation
in `actor_memory_operations`, and the actor in `allowed_actors` or the existing explicit
`allow_runtime_roles` setting. These are transport restrictions, not proof that the actor or
source currently authorizes use. The current Platform actor authority remains mandatory.

The explicit `actor_memory` section supplies `database_path`, `contract_directory` and
`authorities`. Optional `state_directory` and `guard_path` default to the actor database path
plus `.storage` and `.guard.json`. These paths must be separate from the user authority. The
existing storage settings/environment select the backend; the new service identity separates
its PG schemas and pool. A configured actor domain failing to open makes that endpoint
unavailable; user endpoints continue. No actor configuration does not initialize a domain.

`authorities.actor_check` contains the fixed HTTPS `url`, `token` and optional absolute
`ca_file`. `authorities.producers` maps the closed producer names to fixed
`source_facts_url`, `source_check_url`, `token` and optional absolute `ca_file`. Facts and check
may share one URL; their operation values are exactly `source_facts` and `source_check`.
No source address or service credential is accepted from a request. Each call reads current
configuration and trust; it uses the existing OwnerHttpPool without caching responses.

Platform's actor authority handles `actor_check` and, for an external audience, `context_check`.
The actor-only context needs no external assertion but never grants publication. Group/private
assertions are current observations supplied by the trusted producer/context owner. Persistent
source constraints are separate and cannot substitute for that assertion.

Initialize a new isolated domain explicitly while its writers are stopped:

```console
tianshu-memory --config <private-config.json> migrate-actor-memory --backup <new-local-file>
```

For a new domain this records absence and initializes only the new logical source. For an
existing schema2 domain the same explicit command writes an exclusive, complete actor-only
logical JSON backup (seven authoritative tables, column names, rows, backend and old guard),
then installs schema3 and rebuilds its separate `actor_search_terms` index. Schema2 withdrawal
bodies are erased from current actor copies after the backup; body-free identity tombstones
and receipts remain. An installed schema3 domain is verified without recreating it.
A missing or mismatching guard,
lost tables or a partially initialized binding fails closed; this command is not approval to
restore an old backup. Startup does not silently upgrade schema2; stop actor writers and run
the command before starting schema3 application code. Backup files are never overwritten.
DDL/checkpoint failure rolls back SQL or fails closed on checkpoint mismatch. Restoring a
backup requires a separately coordinated actor recovery; never replace the live guard with
the backup guard to bypass newer suppression. User-domain files/tables are not backed up,
migrated or restored by this operation. The unpublished schema1 draft remains unsupported.
SQLite and PG use the same actor state and receipt rules. Referenced
content and source owners are checked over the real configured ports; SQLite component peers
do not constitute actual PG or cross-product acceptance.

Append receives an exact source reference and perspective. Summary, provenance, timestamps,
outcome, restrictions and reading coverage come only from the owner. Both the episode source
and each reading `content_ref` participate in complete before/after checks. Requests and
responses are bounded; batches of at most 64 are checked in full. No network work occurs inside
a SQL transaction. A local revision change rejects the collected proof rather than rebasing it.
Per-source owner state determines invalidation: an unrelated owner sequence/access-policy
advance does not erase a still-current exact source. Same-generation current watermarks cannot
move backward. Transient unavailability is not converted into a durable withdrawal.

First command receipts are stored unchanged. A semantic write replay may echo its new
`request_id`, while episode/revision/domain version/result facts remain the original ones.
`get_receipt` returns the first stored packet, including its first request ID. A different key
finding the same active episode returns `duplicate`. A forgotten or invalidated identity is
never recreated by a late append; completing an old receipt does not reactivate its episode.
Unknown write responses must be reconciled by the original key, not treated as permission to
generate a new command. HTTP timeout does not release admission while its worker is running;
shutdown joins those actual workers before closing this domain.

Source negatives commit independently of a subsequently rejected positive command. Explicit
actor forgetting is local to that actor episode and never deletes a user's original source.
Current role, caller and external context are rechecked before return/commit. No check is a
future authorization lease.

Derived memories are one level of understanding, preference, reflection or autobiography over
exact current episode revisions. Their own text is a derived interpretation; the underlying
episode fact, provenance, outcome and coverage remain unchanged. Every episode and content
source is rechecked before append/read, using the same checkpoint as the episode operations.
Replacing only explicitly named current derived records retires those records; it never
rewrites or retires an episode. Forgetting an episode or learning a stable source withdrawal
invalidates its derived records in the same local transaction. Explicitly forgetting a derived
record leaves the underlying episode available.

Query uses the existing NFKC/CJK lexical tokenizer and an independent rebuildable inverted
index. Actor, current basis, local audience, requested kind and expiry filters precede the
bounded candidate selection. Term-match ordering selects at most 257 episodes and 257 derived
records before local retention-age fading; unrelated recent records cannot hide an older match.
Candidate/output limits set `omitted`; this is not exhaustive semantic or vector search.
It ranks locally, admits whole items within the requested
limit and byte budget, and proves only the selected exact episode/source closure. After that
checkpoint it may remove an invalid selected item, and never adds unproved replacement text.
The response exposes actual episode/source refs. External disclosure requires every basis to
permit the current audience; a public basis cannot broaden another private basis.

Importance is stored separately from retention. Short/medium/long derived retention uses the
existing product defaults of 7/30/180 days; reads do not renew it. Expiry omits a derived record
and makes its explicit check unavailable, without suppressing the original episode or content.

## Lifecycle and owner management

`append_derived(kind=preference)` may include `interest={topic,stance}`, where stance is
like/dislike/mixed/curious. A tag requires original digital-action or observation evidence;
fictional/reported material cannot gain one. `interests` rebuilds a versioned projection and
bounded change history from the existing authoritative records, proves the requested audience,
and counts distinct original content/observation identities. Repeated reading of the same
original, repeated self-expression and receipt replay do not increase support. Untagged legacy
preferences remain visible with `topic=null, stance=unspecified`. At most 256 representative
and history records are proved; representatives precede histories, with incompleteness explicit.
This count is evidence availability, not confidence, affection or a permission grant.

The existing Companion background cycle calls `maintain` with a unique idempotency key and
limit up to 128. It selects expired active/historical derived records before LIMIT, and visits
at most 256 unexpired records using an actor-local rotating metadata cursor for exact redundant
payload compaction. It never invents a summary. Semantic autobiography replacement uses the
existing sourced `append_derived` operation. Idle scans/receipts do not advance the content
domain version or invalidate management/timeline cursors. Actual expiry/compaction does.

Owner pages use the existing Platform service caller, its allowed actor/operation configuration,
and current actor authority. `manage_list`, `memory_impact`, `revise_memory` and `forget_memory`
accept only actor_only management context. List cursors bind role, filters and domain version.
The existing `get_episode` provides full source fact and coverage detail. Revisions edit only
derived interpretations, retaining their exact source basis and creating an explicit replacement;
episode facts remain owned by the producer. Writes require both the exact ref revision and
the preview's domain version. Impact lists replacement history and dependent derived references.
An oversized preview reports `omitted`; forgetting still processes the full dependency closure.

Forgetting overwrites current actor fact/payload bodies and removes search terms, including
replacement history and episode-dependent interpretations. It retains body-free IDs, original
request digests/receipts and suppression evidence. Reworded or split-basis late proposals of the
same derived kind cannot restore the forgotten interpretation; new independent real evidence is
required. Source withdrawal likewise clears current dependent bodies. Tombstone responses carry
no text, interest, source or basis payload. Existing check ports report withdrawn/false, and
historic accepted receipts never prove current availability.

This erases the current logical actor copies, not original Knowledge/Companion content, user
chat records, old backups, database WAL or storage media remnants. Original owner retention and
backup lifecycle remain with their owners; no cross-product deletion is claimed.
