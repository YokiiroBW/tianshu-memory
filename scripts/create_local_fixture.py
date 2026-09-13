"""Create a reviewable synthetic memory demo, with private runtime files only."""

import argparse
import secrets
from datetime import timedelta
from pathlib import Path

from tianshu_memory.contracts import Contracts
from tianshu_memory.domain import canonical, now, utc
from tianshu_memory.service import MemoryService
from tianshu_memory.sources import LocalFixtureSources
from tianshu_memory.store import Store
from tianshu_memory.workflow import LocalWorkflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    directory = Path(args.output).resolve()
    repo = Path(__file__).resolve().parents[1]
    if not directory.is_relative_to(repo / ".runtime"):
        parser.error("Synthetic runtime must stay under this checkout's .runtime directory")
    if (directory / "config.json").exists() or (directory / "memory.sqlite").exists():
        parser.error("Refusing to overwrite an existing fixture")
    directory.mkdir(parents=True, exist_ok=True)
    contracts = Contracts(args.contracts)
    service = MemoryService(
        Store(directory / "memory.sqlite"), contracts, source_authority=LocalFixtureSources()
    )
    workflow = LocalWorkflow(service)
    account = {"namespace": "qq", "immutable_account_id": "synthetic-person-only"}
    channel = {
        "namespace": "qq",
        "binding_id": "synthetic-binding",
        "channel_conversation_id": "synthetic-private",
        "thread_id": None,
    }
    first = {
        "actor_id": "actor-demo",
        "person_id": None,
        "audience": "self_private",
        "conversation_id": "conversation-private",
    }
    expires = utc(now() + timedelta(days=1))
    context = {
        "issuer": "nonebot",
        "authenticated_service": "companion",
        "audience_service": "memory",
        "assertion_ref": "origin-private",
        "verified_account": account,
        "principal_id": None,
        "allowed_scope": first,
        "expires_at": expires,
        "revoked": False,
        "verified_channel": channel,
    }
    command = {
        "schema_version": 1,
        "request_id": "demo-register",
        "origin": {"assertion_ref": "origin-private"},
        "idempotency_key": "demo-register",
        "deadline_at": expires,
    }
    registered = service.register({"command": command, "account": account}, context)
    private = dict(first, person_id=registered["person_id"])
    group = dict(private, audience="group", conversation_id="conversation-group")
    context["allowed_scope"] = private
    source = {
        "message_key": {"channel": channel, "message_id": "synthetic-message", "revision": 1},
        "receipt_id": "synthetic-receipt",
        "archive_state": "pending",
        "locator": None,
    }
    workflow.observe_source(source, private)
    event = {
        "schema_version": 1,
        "event_id": "demo-event",
        "event_type": "conversation.turn_committed",
        "owner": "companion",
        "aggregate_id": "demo-turn",
        "aggregate_version": 1,
        "occurred_at": utc(now()),
        "causation_id": "demo-request",
        "conversation_id": private["conversation_id"],
        "turn_sequence": 1,
        "scope": private,
        "scope_version": 1,
        "input_revision": 1,
        "sources": [source],
        "reality": "real",
        "confirmed_user_correction": False,
        "delivery_state": "not_required",
        "reply_ids": [],
    }
    job = service.consume(
        event, {"authenticated_service": "companion", "allowed_scopes": [private]}
    )
    unit = {
        "statement": "晚上不喝咖啡，白天偶尔可以",
        "conditions": ["白天偶尔可以"],
        "negations": ["晚上不喝咖啡"],
        "valid_time": "current",
        "uncertainty": "confirmed",
        "reality": "real",
        "sources": [source],
    }
    workflow.commit_candidate(
        job["candidate_job_ref"],
        [
            {"scope": scope, "category": "evidence", "field_key": "coffee", "units": [unit]}
            for scope in [private, group]
        ],
    )
    config = {
        "mode": "local_fixture",
        "database_path": service.store.path,
        "contract_directory": str(contracts.directory),
        "callers": {
            "companion": {
                "token": secrets.token_urlsafe(32),
                "issuer": "nonebot",
                "allowed_actors": ["actor-demo"],
                "operations": ["resolve", "register", "link", "select", "revise", "consume"],
                "event_scopes": [private, group],
            }
        },
        "origins": {
            "origin-private": context,
            "origin-group": dict(context, assertion_ref="origin-group", allowed_scope=group),
        },
    }
    (directory / "config.json").write_text(canonical(config), encoding="utf-8")
    for label, scope in [("private", private), ("group", group)]:
        request = {
            "query": {
                "schema_version": 1,
                "request_id": "select-" + label,
                "origin": {"assertion_ref": "origin-" + label},
            },
            "requested_scope": scope,
            "query_text": "咖啡",
            "selection": ["evidence"],
            "known_scope_version": None,
            "budget": {"tokens": 4096, "bytes": 4096},
        }
        (directory / f"select-{label}.json").write_text(canonical(request), encoding="utf-8")
    print(f"Created synthetic fixture: {directory / 'config.json'}")
    print("Source state pending; no external issuer, channel, model or Chat Audit connected.")


if __name__ == "__main__":
    main()
