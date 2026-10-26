# Actor episode domain

The actor domain is part of Memory, with independent storage identity `actor-memory`, logical
database `main`, metadata, restore guard, SQL lock, source watermarks, receipts and admission.
It does not create a bot person or use the user Store, records, jobs or source revision.
The first slice implements the six episode operations from actor-self-memory candidate.2.
The candidate also describes later query/derived operations; those are not yet implemented by
this slice and must not be granted in a caller's `actor_memory_operations`.

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

This records that the actor domain was absent and initializes only the new logical source.
An already installed domain is verified without recreating it. A missing or mismatching guard,
lost tables or a partially initialized binding fails closed; this command is not approval to
restore an old backup. SQLite and PG use the same actor state and receipt rules. Referenced
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
