"""Synthetic owner services over real loopback TLS; no Core/Platform implementation.

Memory uses its deployment composition, actual authentication and SQLite transactions.
Only the owner responses and explicit test user approval issuer are synthesized.
"""

import copy
import json
import ssl
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tianshu_memory.app import configured_app
from tianshu_memory.domain import canonical, fingerprint
from tianshu_memory.store import Store
from tianshu_memory.workflow import TrustedWorkflow


class SyncHarness:
    def __init__(self, directory, contracts, examples, certificates, domain="self_private"):
        self.directory, self.contracts = directory, contracts
        self.examples = copy.deepcopy(examples)
        self.certificates = certificates
        self.physicals = copy.deepcopy(examples[f"{domain}/facts"]["physicals"])
        self.admissions = copy.deepcopy(examples[f"{domain}/facts"]["admissions"])
        self.turns = [copy.deepcopy(examples[f"{domain}/turn-{i}"]) for i in range(2)]
        self.grants = copy.deepcopy(examples[f"{domain}/access"]["grants"])
        self.account = self.physicals[0]["author"]
        self.core_head = {"generation": "synthetic-core", "sequence": 10}
        self.platform_head = {"generation": "synthetic-platform", "sequence": 10}
        self.calls, self.counter = [], 0
        self.mutate = lambda kind, body, response: None
        self.status = {}
        self.contexts = {}
        template = examples[f"{domain}/viewer-response"]["viewer_context"]
        for i, admission in enumerate(self.admissions):
            context = copy.deepcopy(template)
            context.update(
                assertion_ref=f"synthetic-viewer:{i}",
                allowed_scope=copy.deepcopy(admission["scope"]),
                expires_at="2030-01-01T00:00:00Z",
            )
            self.contexts[context["assertion_ref"]] = context
        first = copy.deepcopy(self.contexts["synthetic-viewer:0"])
        first.update(assertion_ref="synthetic-first")
        first["allowed_scope"].update(person_id=None, conversation_id=None)
        self.contexts["synthetic-first"] = first
        self.store = Store(directory / "sync.sqlite")
        self.store.migrate_profiles(directory / "schema1-backup.sqlite")
        self.store.migrate_sources(directory / "schema2-backup.sqlite", contracts)
        self.config_path = directory / "sync-config.json"
        self.config = {
            "mode": "source_sync",
            "database_path": self.store.path,
            "contract_directory": str(contracts.directory),
            "callers": {
                "companion": {
                    "token": "test-only-companion-secret",
                    "issuer": "platform",
                    "issuer_token": "synthetic-origin-token",
                    "issuer_ca_file": str(certificates / "ca.pem"),
                    "allowed_actors": ["actor:a", "actor:b"],
                    "operations": [
                        "register",
                        "resolve",
                        "select",
                        "select_profiles",
                        "consume",
                        "revise",
                        "check_sources",
                    ],
                    "event_scopes": [],
                }
            },
        }

    def save(self):
        self.config_path.write_text(canonical(self.config), encoding="utf-8")

    def command(self, actor=0, first=False):
        self.counter += 1
        return {
            "schema_version": 1,
            "request_id": f"synthetic-request:{self.counter}",
            "idempotency_key": f"synthetic-command:{self.counter}",
            "origin": {
                "assertion_ref": "synthetic-first" if first else f"synthetic-viewer:{actor}"
            },
            "deadline_at": "2030-01-01T00:00:00Z",
        }

    def scope(self, actor=0):
        return copy.deepcopy(self.admissions[actor]["scope"])

    def event(self, actor=0):
        return copy.deepcopy(self.turns[actor]["committed_event"])

    def selection(self, actor=0, budget=100000, known=None):
        command = self.command(actor)
        return {
            "query": {k: command[k] for k in ("schema_version", "request_id", "origin")},
            "requested_scope": self.scope(actor),
            "query_text": "咖啡",
            "selection": ["identity"],
            "known_scope_version": known,
            "budget": {"tokens": budget, "bytes": budget},
        }

    def check(self, actor=0):
        return dict(
            schema_version=1,
            request_id=self.command(actor)["request_id"],
            turn_id=self.turns[actor]["turn_id"],
            input_revision=1,
            scope=self.scope(actor),
            sources=copy.deepcopy(self.turns[actor]["input_sources"]),
        )

    def post(self, suffix, body):
        return self.client.post("/internal/v1/" + suffix, json=body)

    def select(self, **kwargs):
        response = self.post("memory/select", self.selection(**kwargs))
        assert response.status_code == 200, response.text
        return response.json()

    def seed(self, actor=0):
        event = self.event(actor)
        response = self.post("memory/turn-commits", event)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "accepted", response.text
        job = response.json()["candidate_job_ref"]
        draft = copy.deepcopy(self.examples["memory/candidate-commit"]["drafts"][0])
        draft["scope"] = self.scope(actor)
        draft["units"][0]["sources"] = event["sources"]
        result = TrustedWorkflow(self.service).commit_candidate({"job_id": job, "drafts": [draft]})
        assert result["state"] == "committed"
        return result, job, event

    def revision(self, record_id, actor=0, kind="forget"):
        request = dict(
            command=self.command(actor),
            record_id=record_id,
            expected_version=1,
            revision_kind=kind,
            confirmation_ref=f"synthetic-user-confirmation:{self.counter}",
            evidence_refs=self.event(actor)["sources"],
            replacement_statement="只在上午喝茶" if kind == "correct" else None,
        )
        # Explicit test approval issuer only; this is not a real user confirmation claim.
        workflow = TrustedWorkflow(
            self.service, SimpleNamespace(verify_approval=lambda input: True)
        )
        workflow.confirm_revision(
            {
                "request": request,
                "verified_context": copy.deepcopy(self.contexts[f"synthetic-viewer:{actor}"]),
                "binding_version": 1,
                "expires_at": "2030-01-01T00:00:00Z",
            }
        )
        return request

    def bind_person(self, person):
        self.person = person
        for admission in self.admissions:
            admission["scope"]["person_id"] = person
        for turn in self.turns:
            turn["scope"]["person_id"] = person
            turn["committed_event"]["scope"]["person_id"] = person
        for ref, context in self.contexts.items():
            if ref != "synthetic-first":
                context["allowed_scope"]["person_id"] = person
        self.config["callers"]["companion"]["event_scopes"] = [self.scope(0), self.scope(1)]
        self.save()

    def profile_fixture(self):
        """Install an explicit stored projection fixture; no production approval is minted."""
        seeded, _, _ = self.seed()
        group_id, record_id = seeded["group_ids"][0], seeded["record_ids"][0]
        subject = {"kind": "person", "person_id": self.person}
        scope = {
            "actor_id": "actor:a",
            "profile_audience": "public_preference",
            "conversation_id": None,
            "profile_subject": subject,
        }
        with self.store.transaction() as db:
            row = db.execute("SELECT payload FROM records WHERE id=?", (record_id,)).fetchone()
            unit = json.loads(row[0])
            unit.pop("subject_person_id")
            unit.update(
                subject=subject,
                category="interest",
                field_key="interest.coffee",
                sharing="public_preference",
                visibility="shared_projection",
                sources=[
                    {
                        "kind": "shareable_projection",
                        "owner": "memory",
                        "projection_ref": "synthetic-profile-projection",
                        "projection_version": 1,
                    }
                ],
            )
            self.contracts.validate("profiles#unit", unit)
            db.execute(
                "UPDATE groups SET scope=?,category='interest',field_key='interest.coffee' WHERE id=?",
                (canonical(scope), group_id),
            )
            db.execute("UPDATE records SET payload=? WHERE id=?", (canonical(unit), record_id))
            db.execute(
                "UPDATE history SET payload=? WHERE record_id=?", (canonical(unit), record_id)
            )
            db.execute("DELETE FROM projections WHERE record_id=?", (record_id,))
            db.execute(
                "INSERT INTO projections VALUES (?, ?, 1, 'active')",
                ("synthetic-profile-projection", record_id),
            )
            db.execute(
                "INSERT INTO profile_shares VALUES (?, 'actor:a', 'person', ?, 'public_preference', NULL, ?)",
                (group_id, self.person, "synthetic-stored-profile-fixture"),
            )
            db.execute(
                "UPDATE search_index SET scope=? WHERE record_id=?", (canonical(scope), record_id)
            )
        reader = copy.deepcopy(self.contexts["synthetic-first"])
        reader.update(assertion_ref="synthetic-profile-reader")
        reader["verified_account"] = {
            "namespace": "web",
            "immutable_account_id": "synthetic-profile-reader",
        }
        self.contexts[reader["assertion_ref"]] = reader
        command = self.command()
        command["origin"]["assertion_ref"] = reader["assertion_ref"]
        response = self.post(
            "identity/register", {"command": command, "account": reader["verified_account"]}
        )
        assert response.status_code == 200, response.text
        reader["allowed_scope"] = dict(self.scope(), person_id=response.json()["person_id"])
        return reader

    def profile_request(self, reader, *, budget=100000, known=None, target=None):
        command = self.command()
        command["origin"]["assertion_ref"] = reader["assertion_ref"]
        return {
            "query": {k: command[k] for k in ("schema_version", "request_id", "origin")},
            "requester_scope": copy.deepcopy(reader["allowed_scope"]),
            "target": target or {"kind": "person", "person_id": self.person},
            "query_text": "咖啡",
            "selection": ["interest"],
            "known_scope_version": known,
            "budget": {"tokens": budget, "bytes": budget},
        }

    def response(self, kind, body):
        response = {
            "schema_version": 1,
            "request_id": body["request_id"],
            "request_digest": fingerprint(body),
        }
        if kind == "origin":
            return {
                "schema_version": 1,
                "request_id": body["request_id"],
                "context": copy.deepcopy(self.contexts[body["assertion_ref"]]),
            }
        if kind in {"snapshot", "head"}:
            requested = {canonical(selector) for selector in body["selectors"]}
            keys = {canonical(selector["key"]) for selector in body["selectors"]}
            response.update(
                head=copy.deepcopy(self.core_head),
                physicals=[copy.deepcopy(p) for p in self.physicals if canonical(p["key"]) in keys],
                admissions=[
                    copy.deepcopy(a)
                    for a in self.admissions
                    if canonical(a["selector"]) in requested
                ],
                turns=[copy.deepcopy(t) for t in self.turns if t["turn_id"] in body["turn_ids"]],
            )
        else:
            grants = []
            for admission in body["admissions"]:
                grant = copy.deepcopy(
                    next(g for g in self.grants if g["selector"] == admission["selector"])
                )
                grant.update(scope=admission["scope"], admission_digest=fingerprint(admission))
                grants.append(grant)
            viewer = body["viewer"]
            response.update(
                operation="current",
                head=copy.deepcopy(self.platform_head),
                grants=grants,
                viewer_context=copy.deepcopy(self.contexts[viewer["origin"]["assertion_ref"]])
                if viewer
                else None,
            )
        return response

    @contextmanager
    def owners(self):
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                kind = (
                    body["mode"]
                    if self.path == "/internal/v1/source-facts/read"
                    else "current"
                    if self.path == "/internal/v1/source-access/read"
                    else "origin"
                )
                token = (
                    "synthetic-core-token"
                    if kind in {"snapshot", "head"}
                    else (
                        "synthetic-platform-token"
                        if kind == "current"
                        else "synthetic-origin-token"
                    )
                )
                harness.calls.append((kind, copy.deepcopy(body), self.headers.get("Authorization")))
                response = harness.response(kind, body)
                harness.mutate(kind, body, response)
                status = harness.status.get(kind, 200)
                if self.headers.get("Authorization") != f"Bearer {token}":
                    status = 401
                payload = canonical(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.certificates / "server.pem", self.certificates / "server.key")
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            base = f"https://127.0.0.1:{server.server_port}"
            self.config["callers"]["companion"]["issuer_url"] = (
                base + "/internal/v1/origins/resolve"
            )
            self.config["source_sync"] = {
                "core": {
                    "url": base + "/internal/v1/source-facts/read",
                    "token": "synthetic-core-token",
                    "ca_file": str(self.certificates / "ca.pem"),
                },
                "platform": {
                    "url": base + "/internal/v1/source-access/read",
                    "token": "synthetic-platform-token",
                    "ca_file": str(self.certificates / "ca.pem"),
                },
            }
            self.save()
            thread = Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            thread.start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join(timeout=5)
                assert not thread.is_alive()

    @contextmanager
    def runtime(self, monkeypatch):
        monkeypatch.setenv("TIANSHU_MEMORY_CONFIG", str(self.config_path))
        with TestClient(configured_app()) as client:
            client.headers["Authorization"] = "Bearer test-only-companion-secret"
            self.client, self.service = client, client.app.state.memory
            response = self.post(
                "identity/register",
                {
                    "command": self.command(first=True),
                    "account": self.account,
                },
            )
            assert response.status_code == 200, response.text
            self.bind_person(response.json()["person_id"])
            yield self
