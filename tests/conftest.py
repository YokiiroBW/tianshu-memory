import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tianshu_memory.app import create_app
from tianshu_memory.auth import Authenticator
from tianshu_memory.contracts import Contracts
from tianshu_memory.domain import canonical
from tianshu_memory.service import MemoryService
from tianshu_memory.sources import LocalFixtureSources
from tianshu_memory.store import Store
from tianshu_memory.workflow import LocalWorkflow


def contract_directory():
    override = os.environ.get("TIANSHU_CONTRACT_DIRECTORY")
    if override:
        return Path(override)
    root = Path(__file__).resolve().parents[1]
    context = json.loads((root / ".runtime/workspace-context.json").read_text(encoding="utf-8"))
    return Path(context["workspace"]) / "contracts/text-dialogue/v1"


@pytest.fixture(scope="session")
def contracts():
    return Contracts(contract_directory())


class Harness:
    def __init__(self, directory, contracts):
        self.directory, self.contracts = directory, contracts
        self.examples = {
            d["id"]: d["document"]
            for d in json.loads(
                (contracts.directory / "examples/documents.json").read_text(encoding="utf-8")
            )
        }
        self.clock = lambda: datetime(2026, 9, 14, 1, tzinfo=UTC)
        self.store = Store(directory / "memory.sqlite")
        self.service = MemoryService(
            self.store, contracts, source_authority=LocalFixtureSources(), clock=self.clock
        )
        self.workflow = LocalWorkflow(self.service)
        self.account = {"namespace": "qq", "immutable_account_id": "10001"}
        self.channel = {
            "namespace": "qq",
            "binding_id": "synthetic-binding",
            "channel_conversation_id": "synthetic-private-channel",
            "thread_id": None,
        }
        self.config = {
            "mode": "local_fixture",
            "database_path": self.store.path,
            "contract_directory": str(contracts.directory),
            "callers": {
                "companion": {
                    "token": "test-only-companion-secret",
                    "issuer": "nonebot",
                    "allowed_actors": ["actor-fixture"],
                    "operations": ["resolve", "register", "link", "select", "revise", "consume"],
                    "event_scopes": [],
                }
            },
            "origins": {},
        }
        first = {
            "actor_id": "actor-fixture",
            "person_id": None,
            "audience": "self_private",
            "conversation_id": None,
        }
        self.add_origin("origin-first", first)
        self.config_path = directory / "config.json"
        self.save_config()
        self.auth = Authenticator(self.config_path, contracts, self.clock)
        self.client = TestClient(create_app(service=self.service, auth=self.auth))
        self.counter = 0
        request = self.command("origin-first")
        response = self.post("identity/register", {"command": request, "account": self.account})
        assert response.status_code == 200, response.text
        self.person = response.json()["person_id"]
        self.private = dict(first, person_id=self.person, conversation_id="conversation-private")
        self.group = dict(self.private, audience="group", conversation_id="conversation-group")
        self.add_origin("origin-private", self.private)
        self.add_origin("origin-group", self.group)
        self.config["callers"]["companion"]["event_scopes"] = [self.private, self.group]
        self.save_config()

    def add_origin(self, ref, scope, account=None):
        self.config["origins"][ref] = {
            "issuer": "nonebot",
            "authenticated_service": "companion",
            "audience_service": "memory",
            "assertion_ref": ref,
            "verified_account": account or self.account,
            "principal_id": None,
            "allowed_scope": scope,
            "expires_at": "2030-01-01T00:00:00Z",
            "revoked": False,
            "verified_channel": self.channel,
        }

    def save_config(self):
        self.config_path.write_text(canonical(self.config), encoding="utf-8")

    def command(self, origin="origin-private", key=None):
        self.counter += 1
        return {
            "schema_version": 1,
            "request_id": f"request-{self.counter}",
            "origin": {"assertion_ref": origin},
            "idempotency_key": key or f"key-{self.counter}",
            "deadline_at": "2030-01-01T00:00:00Z",
        }

    def query(self, origin="origin-private"):
        command = self.command(origin)
        return {k: command[k] for k in ("schema_version", "request_id", "origin")}

    def post(self, path, payload, **kwargs):
        return self.client.post(
            "/internal/v1/" + path,
            json=payload,
            headers={"Authorization": "Bearer test-only-companion-secret"},
            **kwargs,
        )

    def source(self, message="message-1", revision=1):
        return {
            "message_key": {"channel": self.channel, "message_id": message, "revision": revision},
            "receipt_id": "receipt-" + message,
            "archive_state": "pending",
            "locator": None,
        }

    def event(self, sources, scope=None, turn="turn-1", event_id="event-1", reality="real"):
        event = copy.deepcopy(self.examples["turn_committed"])
        event.update(
            event_id=event_id,
            aggregate_id=turn,
            scope=scope or self.private,
            conversation_id=(scope or self.private)["conversation_id"],
            sources=sources,
            reality=reality,
        )
        return event

    def candidate(
        self, sources=None, turn="turn-1", event_id="event-1", scope=None, reality="real"
    ):
        sources = sources or [self.source()]
        scope = scope or self.private
        for source in sources:
            self.workflow.observe_source(source, scope, reality=reality)
        event = self.event(sources, scope, turn, event_id, reality)
        response = self.post("memory/turn-commits", event)
        assert response.status_code == 200, response.text
        return response.json()["candidate_job_ref"], event

    def unit(self, source=None, statement="晚上不喝咖啡，白天偶尔可以", **overrides):
        return dict(
            {
                "statement": statement,
                "conditions": ["白天偶尔可以"],
                "negations": ["晚上不喝咖啡"],
                "valid_time": "current",
                "uncertainty": "confirmed",
                "reality": "real",
                "sources": [source or self.source()],
            },
            **overrides,
        )

    def draft(self, units=None, scope=None, **overrides):
        return dict(
            {
                "scope": scope or self.private,
                "category": "evidence",
                "field_key": "coffee",
                "units": units or [self.unit()],
            },
            **overrides,
        )

    def seed(self, drafts=None):
        job, event = self.candidate()
        return self.workflow.commit_candidate(job, drafts or [self.draft()]), job, event

    def selection(self, query="咖啡", scope=None, budget=100000, known=None, selection=None):
        scope = scope or self.private
        return {
            "query": self.query(
                "origin-group" if scope["audience"] == "group" else "origin-private"
            ),
            "requested_scope": copy.deepcopy(scope),
            "query_text": query,
            "selection": selection or ["evidence"],
            "known_scope_version": known,
            "budget": {"tokens": budget, "bytes": budget},
        }

    def select(self, **kwargs):
        response = self.post("memory/select", self.selection(**kwargs))
        assert response.status_code == 200, response.text
        return response.json()

    def revision(self, record_id, kind="correct", replacement="只在上午喝茶"):
        request = {
            "command": self.command(),
            "record_id": record_id,
            "expected_version": 1,
            "revision_kind": kind,
            "confirmation_ref": "confirmation-test",
            "evidence_refs": [self.source()],
            "replacement_statement": replacement if kind == "correct" else None,
        }
        self.workflow.confirm_revision(request, self.account, self.private, "2027-01-01T00:00:00Z")
        return request


@pytest.fixture
def h(tmp_path, contracts):
    fixture = Harness(tmp_path, contracts)
    yield fixture
    fixture.client.close()
