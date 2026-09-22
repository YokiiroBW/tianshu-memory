"""Explicit local user operations; deployment credentials are separate from service tokens.

The request file is the full operation the user elects to execute. It cannot supply a
trusted context, a boolean consent flag, executable policy, or a credential mapping.
"""

import hashlib
import hmac

from .auth import Authenticator
from .domain import canonical, fingerprint, new_id, require, strict_json


def credential_digest(credential):
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


class LocalUserApproval:
    """One authenticated explicit operation, created only by the same-product application."""

    def __init__(self, service, config_path, principal, credential, action):
        self.service = service
        self.auth = Authenticator(config_path, service.contracts, service.clock)
        self.principal, self.credential = principal, credential
        self.action = strict_json(canonical(action))
        action = self.action
        self.registration = self.authenticate()
        operation = action.get("operation")
        keys = {
            "confirm_revision": {"operation", "request", "expires_at"},
            "approve_profile": {"operation", "draft", "origin", "expires_at"},
            "publish_profile": {"operation", "draft", "origin", "approval_ref"},
            "revoke_profile": {"operation", "draft", "origin", "approval_ref"},
        }
        require(operation in keys and set(action) == keys[operation], "invalid_input", 400)
        if operation == "confirm_revision":
            service.contracts.validate("identity-memory#revise_request", action["request"])
            origin = action["request"]["command"]["origin"]
        else:
            origin = action["origin"]
        require(isinstance(origin, dict) and set(origin) == {"assertion_ref"}, "invalid_input", 400)
        caller = self.auth.config().get("callers", {}).get("companion")
        require(isinstance(caller, dict), "dependency_unavailable", 503)
        self.context = self.auth.resolve(
            "companion", caller, origin["assertion_ref"], new_id("user-action")
        )
        with service.store.transaction() as db:
            self.binding_version = self.recheck(db, self.context)["version"]
        if operation == "confirm_revision":
            self.payload = dict(
                request=action["request"],
                verified_context=self.context,
                binding_version=self.binding_version,
                expires_at=action["expires_at"],
            )
            scope = self.context["allowed_scope"]
            require(scope in self.registration.get("revision_scopes", []))
        else:
            self.payload = dict(draft=action["draft"], context=self.context)
            key = "expires_at" if operation == "approve_profile" else "approval_ref"
            self.payload[key] = action[key]
        self.payload = strict_json(canonical(self.payload))

    def authenticate(self):
        config = self.auth.config()
        require(config.get("mode") == "source_sync", "dependency_unavailable", 503)
        registrations = config.get("local_users", {})
        require(bool(registrations), "dependency_unavailable", 503)
        registration = registrations.get(self.principal)
        require(isinstance(registration, dict), "unauthorized", 401)
        digest = registration.get("credential_sha256")
        require(isinstance(digest, str) and len(digest) == 64, "dependency_unavailable", 503)
        require(
            isinstance(self.credential, str) and len(self.credential) >= 32, "unauthorized", 401
        )
        require(
            hmac.compare_digest(credential_digest(self.credential), digest), "unauthorized", 401
        )
        # A mistakenly registered inbound/outbound service token must never become user consent.
        tokens = [
            v.get(key)
            for v in config.get("callers", {}).values()
            for key in ("token", "issuer_token")
        ]
        tokens += [
            v.get("token") for v in config.get("source_sync", {}).values() if isinstance(v, dict)
        ]
        require(self.credential not in tokens, "unauthorized", 401)
        require(not registration.get("disabled", False))
        return registration

    def verify(self, operation, payload):
        require(
            operation == self.action["operation"] and canonical(payload) == canonical(self.payload)
        )
        require(self.authenticate() == self.registration)

    def recheck(self, db, context):
        require(self.authenticate() == self.registration)
        require(context["verified_account"] == self.registration.get("account"))
        require(context["allowed_scope"]["actor_id"] in self.registration.get("actors", []))
        binding = self.service._authorize(db, context, scope=context["allowed_scope"])
        require(binding is not None)
        if hasattr(self, "binding_version"):
            require(binding["version"] == self.binding_version)
        return binding

    def authority(self, db, draft, context):
        binding = self.recheck(db, context)
        scope, source_scope = context["allowed_scope"], draft["source_scope"]
        # Source synchronization owns the author binding. A curator's current group scope
        # can differ in person, but never actor/audience/conversation from the source group.
        require(source_scope["actor_id"] == scope["actor_id"])
        subject = draft["subject"]
        own_interest = (
            subject == {"kind": "person", "person_id": binding["person_id"]}
            and draft["category"] == "interest"
        )
        if own_interest:
            require(source_scope["person_id"] == binding["person_id"])
            role = "owner"
        else:
            role = "curator"
            require(draft["sharing"] == "group_only")
            require(
                draft["category"]
                in ({"topic", "style"} if subject["kind"] == "group" else {"style"})
            )
        if draft["sharing"] == "group_only":
            require(
                scope["audience"] == "group"
                and scope["conversation_id"] == draft["conversation_id"]
            )
            # Only the owner may explicitly share their private interest into this group.
            # Curated style/topic must originate in the current group.
            if not own_interest or source_scope["audience"] == "group":
                require(
                    source_scope["audience"] == "group"
                    and source_scope["conversation_id"] == scope["conversation_id"]
                )
        else:
            require(own_interest and source_scope == scope)
        permission = dict(
            role=role,
            actor_id=scope["actor_id"],
            subject_kind=subject["kind"],
            category=draft["category"],
            sharing=draft["sharing"],
            conversation_id=draft["conversation_id"],
        )
        require(permission in self.registration.get("profile_permissions", []))
        return dict(
            principal=self.principal,
            account=context["verified_account"],
            binding_version=binding["version"],
            scope=scope,
            registration_digest=fingerprint(self.registration),
            permission=permission,
        )
