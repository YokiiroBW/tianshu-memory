# Memory role admission v1 candidate

Schema and examples mirror the Platform candidate in this isolated task. They
are proposed exchange contracts, not published root contracts. Memory owns
the exact actor grant and version. The authenticated `platform` caller needs
`role_admin:true` for `/internal/v1/role-runtime/authorize`. A Companion
caller needs `allow_runtime_roles:true` to consume a grant. Its static
`allowed_actors` and all existing origin and scoped data checks remain in
force. A disabled grant refuses new role memory calls; retained records stay
in the same actor scope.

`memory_apply.legacy` must equal whether the actor is already in the static
deployment allowlist. This permits explicit adoption of a static role. Once
adopted, a disabled grant overrides that static allowlist; an old enable
receipt cannot lift the newer denial. Unmanaged static actors keep their
existing authorization until explicitly adopted.

Set `role_grants_database_path` to an absolute path on durable local storage.
Back up this sidecar together with the main Memory database and coordinated
Platform and Companion units. A missing sidecar at rollout creates an empty
grant catalog; it does not grant access to historical memory or static actors.
