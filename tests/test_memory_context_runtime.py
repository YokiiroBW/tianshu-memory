"""Real local restart and HTTPS owner/proof transport with exclusively synthetic data."""

import copy
import json
import ssl
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from source_sync_harness import SyncHarness
from test_auth_https import certificates as certificates
from test_memory_context import continuity as continuity
from test_memory_context import proposal, query_request
from test_process import server
from test_source_transport import source_contracts as source_contracts
from test_source_transport import source_examples as source_examples

from tianshu_memory.domain import canonical, fingerprint, parse_time, utc
from tianshu_memory.memory_context.migration import semantic_payload


def test_http_restart_can_query_original_receipt_and_recall_same_records(continuity):
    h = continuity
    request = proposal(h, key="restart-operation")
    with server(h.config_path) as client:
        first = client.post("/internal/v1/memory/context/propose", json=request)
        assert first.status_code == 200, first.text
        first = first.json()
    with server(h.config_path) as client:
        recovered = client.post(
            "/internal/v1/memory/context/receipt",
            json=dict(query=h.query(), scope=h.private, operation_id="restart-operation"),
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["receipt"] == first
        recall = client.post("/internal/v1/memory/context/query", json=query_request(h))
        assert recall.status_code == 200, recall.text
        assert recall.json()["selected_units"][0]["record_id"] == first["record_ids"][0]


@contextmanager
def proof_server(harness):
    accepted = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/internal/v1/memory-context/proof/verify"
            assert (
                self.headers.get("Authorization") == "Bearer synthetic-proof-credential-0123456789"
            )
            expected = accepted.get(body["proof_ref"])
            valid = expected == (body["operation_digest"], body["purpose"])
            response = dict(
                body,
                valid=True,
                principal_id="synthetic-person",
                accounts=[harness.account],
                scopes=[harness.scope()],
                expires_at="2030-01-01T00:00:00Z",
            )
            response.pop("origin")
            raw = canonical(response).encode()
            self.send_response(200 if valid else 403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(harness.certificates / "server.pem", harness.certificates / "server.key")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as listener:
        listener.socket = tls.wrap_socket(listener.socket, server_side=True)
        harness.config["memory_context_proofs"] = dict(
            url=f"https://127.0.0.1:{listener.server_port}/internal/v1/memory-context/proof/verify",
            token="synthetic-proof-credential-0123456789",
            ca_file=str(harness.certificates / "ca.pem"),
        )
        harness.save()
        thread = Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield accepted
        finally:
            listener.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_configured_https_sources_natural_query_time_coverage_and_verified_forget(
    tmp_path, source_contracts, source_examples, certificates, monkeypatch
):
    h = SyncHarness(tmp_path, source_contracts, source_examples, certificates)
    h.store.migrate_context(tmp_path / "before-context.sqlite")
    h.config["callers"]["companion"]["operations"] += [
        "context_" + name for name in ("query", "propose", "receipt", "batch", "association")
    ]
    with h.owners(), proof_server(h) as proofs, h.runtime(monkeypatch):
        first, _, event = h.seed()
        request = dict(
            h.selection(),
            include_associated=False,
            time_range=None,
            limit=32,
            known_association_version=None,
            known_scope_checks=None,
        )
        request["query_text"] = "咖啡"
        recalled = h.post("memory/context/query", request)
        assert recalled.status_code == 200, recalled.text
        assert recalled.json()["selected_units"][0]["record_id"] == first["record_ids"][0]
        physical_input = copy.deepcopy(source_examples["self_private/request"]["input"])
        source_time = parse_time(physical_input["sent_at"])

        def with_original_time(kind, body, response):
            if kind == "snapshot" and body["include_content"]:
                for physical in response["physicals"]:
                    physical["content"] = copy.deepcopy(physical_input)

        h.mutate = with_original_time
        request["time_range"] = {
            "from": utc(source_time - timedelta(seconds=1)),
            "to": utc(source_time + timedelta(seconds=1)),
        }
        timed = h.post("memory/context/query", request)
        assert timed.status_code == 200 and timed.json()["selected_units"]
        request["time_range"] = {
            "from": utc(source_time + timedelta(days=1)),
            "to": utc(source_time + timedelta(days=2)),
        }
        assert h.post("memory/context/query", request).json()["selected_units"] == []
        forget = dict(
            command=h.command(),
            scope=h.scope(),
            batch_ref="batch:synthetic",
            item_id="item:forget",
            kind="forget",
            target=dict(
                category="evidence",
                field_key="coffee",
                item_key=None,
                record_id=first["record_ids"][0],
                expected_version=1,
            ),
            units=[],
            evidence_refs=copy.deepcopy(event["sources"]),
            proof_ref="proof:real-https",
        )
        proofs[forget["proof_ref"]] = (fingerprint(semantic_payload(forget)), "revision")
        forgotten = h.post("memory/context/propose", forget)
        assert forgotten.status_code == 200, forgotten.text
        assert forgotten.json()["state"] == "tombstoned"
        request["time_range"] = None
        assert not h.post("memory/context/query", request).json()["selected_units"]
        lookup = dict(
            query={k: forget["command"][k] for k in ("schema_version", "request_id", "origin")},
            scope=h.scope(),
            operation_id=forget["command"]["idempotency_key"],
        )
        h.status["snapshot"] = 503
        assert h.post("memory/context/receipt", lookup).json()["receipt"]["state"] == "tombstoned"
        batch = dict(
            query=lookup["query"],
            scope=h.scope(),
            batch_ref="batch:synthetic",
            after=None,
            limit=32,
        )
        assert h.post("memory/context/batch", batch).json()["receipts"][0]["state"] == "tombstoned"
        assert h.post("memory/context/query", request).status_code == 503
