"""Standalone Knowledge assembly; identities remain with Memory and its live issuer."""

from .auth import Authenticator
from .chat_requests import Requests
from .contracts import Contracts
from .domain import now, parse_time, require
from .knowledge_content import KnowledgeContent
from .knowledge_content_http import mount as mount_content
from .knowledge_content_migration import ready
from .role_grants_http import mount as mount_roles
from .service import MemoryService
from .store import Store


def mount(app, config_path, config, *, body_timeout, execute_timeout):
    callers = config.get("callers", {})
    enabled = any(
        any(op.startswith("content_") for op in caller.get("operations", []))
        for caller in callers.values()
    )
    if not enabled and not any(caller.get("role_admin") is True for caller in callers.values()):
        return
    contracts = Contracts(config["contract_directory"])
    if enabled:
        contracts.load_content()
    auth = Authenticator(config_path, contracts, now)
    store = Store(
        config["database_path"], recovery_path=config.get("source_sync", {}).get("recovery_path")
    )
    service = MemoryService(store, contracts)

    def authorize_scope(db, context, *, scope):
        # Authenticator.resolve has just re-resolved this account against the real issuer.
        # Knowledge never copies or creates Memory's account/person bindings.
        require(context["allowed_scope"] == scope)
        require(not context["revoked"] and parse_time(context["expires_at"]) > now())
        require(scope["person_id"] is not None and scope["conversation_id"] is not None)

    def validate_reader(db, scope):
        contracts.validate("common#scope", scope)
        require(
            scope["person_id"] is not None and scope["conversation_id"] is not None,
            "invalid_input",
            400,
        )
        # An explicit owner grant cannot manufacture a reader: every read still resolves
        # the reader's exact current issuer scope and is checked against version/hash.

    admission = Requests(4)
    content = (
        KnowledgeContent(
            service, auth, authorize_scope=authorize_scope, validate_reader=validate_reader
        )
        if enabled
        else None
    )
    app.state.knowledge_content = content
    app.state.content_auth = auth
    app.state.content_requests = admission
    mount_roles(app, auth, admission, body_timeout=body_timeout)
    if enabled:
        with store.transaction() as db:
            ready(db)
        mount_content(
            app,
            content,
            auth,
            admission,
            body_timeout=body_timeout,
            execute_timeout=execute_timeout,
        )
