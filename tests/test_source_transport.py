"""Actual loopback HTTPS transport, with published synthetic owner documents only."""

import copy
import json
import shutil
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError
from test_auth_https import certificates as certificates

from tianshu_memory.contracts import SOURCE_MANIFEST_SHA256, Contracts, digest
from tianshu_memory.domain import Fault
from tianshu_memory.source_transport import MAX_BODY_BYTES, SourceTransport


@pytest.fixture
def source_contracts(contracts):
    loaded = Contracts(contracts.directory)
    loaded.load_sources()
    return loaded


@pytest.fixture
def source_examples(source_contracts):
    path = source_contracts.directory.parent.parent / "source-sync/v1/examples/documents.json"
    return {row["id"]: row["document"] for row in json.loads(path.read_text(encoding="utf-8"))}


@pytest.fixture
def source_owner(certificates, source_examples, source_contracts, request):
    state = SimpleNamespace(
        calls=[],
        tokens={"facts": "synthetic-core-token", "current": "synthetic-platform-token"},
        documents={
            "facts": source_examples["self_private/facts"],
            "current": source_examples["self_private/access"],
        },
        status=200,
        raw=None,
        content_type="application/json; charset=utf-8",
        encoding=None,
        declared_length=None,
        send_length=True,
        response_request_id=None,
        delay=0,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            kind = "facts" if self.path == "/internal/v1/source-facts/read" else "current"
            state.calls.append((self.path, dict(self.headers), body))
            status = state.status
            if self.headers.get("Authorization") != f"Bearer {state.tokens[kind]}":
                status = 401
            document = copy.deepcopy(state.documents[kind])
            document["request_id"] = state.response_request_id or body["request_id"]
            document["request_digest"] = source_contracts.source_rules.digest(body)
            payload = json.dumps(document).encode() if state.raw is None else state.raw
            if status != 200:
                payload = b'{"private_upstream_detail":"must-not-escape"}'
            time.sleep(state.delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", state.content_type)
                if state.encoding is not None:
                    self.send_header("Content-Encoding", state.encoding)
                if state.send_length:
                    length = (
                        len(payload) if state.declared_length is None else state.declared_length
                    )
                    self.send_header("Content-Length", str(length))
                if status in {301, 302, 307, 308}:
                    self.send_header("Location", state.base + "/do-not-forward-credentials")
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                # Expected when a rejecting or timed-out client has already closed TLS.
                pass

        def log_message(self, *args):
            pass

    label = getattr(request, "param", "server")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificates / f"{label}.pem", certificates / f"{label}.key")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        state.base = f"https://127.0.0.1:{server.server_port}"
        state.facts_url = state.base + "/internal/v1/source-facts/read"
        state.current_url = state.base + "/internal/v1/source-access/read"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.fixture
def transport(tmp_path, source_owner, source_contracts, certificates):
    config = {
        "source_sync": {
            "core": {
                "url": source_owner.facts_url,
                "token": source_owner.tokens["facts"],
                "ca_file": str(certificates / "ca.pem"),
            },
            "platform": {
                "url": source_owner.current_url,
                "token": source_owner.tokens["current"],
                "ca_file": str(certificates / "ca.pem"),
            },
        }
    }
    path = tmp_path / "source-config.json"

    def save():
        path.write_text(json.dumps(config), encoding="utf-8")

    save()
    return SimpleNamespace(client=SourceTransport(path, source_contracts), config=config, save=save)


def unavailable(call):
    with pytest.raises(Fault) as failure:
        call()
    assert failure.value.code == "dependency_unavailable"
    assert failure.value.status == 503
    assert str(failure.value) == "dependency_unavailable"


def test_source_package_is_optional_and_preserves_old_schema_names(contracts, tmp_path):
    directory = tmp_path / "text-dialogue/v1"
    shutil.copytree(contracts.directory, directory)
    loaded = Contracts(directory)
    assert loaded.source_version is None and loaded.source_rules is None
    with pytest.raises(FileNotFoundError):
        loaded.load_sources()


def test_published_source_examples_and_pure_rules_load(source_contracts):
    directory = source_contracts.directory.parent.parent / "source-sync/v1"
    assert digest((directory / "manifest.json").read_bytes()) == SOURCE_MANIFEST_SHA256
    assert source_contracts.source_version == source_contracts.profile_version == "1.0.0"
    assert source_contracts.schemas["common"]["$id"].endswith("text-dialogue/v1/common.json")
    assert set(source_contracts.schemas) >= {"sync-shared", "sync-sources", "sync-workflow"}
    rows = json.loads((directory / "examples/documents.json").read_text(encoding="utf-8"))
    for row in rows:
        name = row["schema"]
        if name.startswith("text-"):
            name = name.removeprefix("text-")
        elif name.startswith("profile-"):
            name = name.removeprefix("profile-")
        else:
            name = "sync-" + name
        source_contracts.validate(name, row["document"])
    examples = {row["id"]: row["document"] for row in rows}
    source_contracts.source_rules.source_snapshot(
        examples["self_private/facts_request"], examples["self_private/facts"]
    )


@pytest.mark.parametrize(
    "package,relative",
    [
        ("source-sync", "manifest.json"),
        ("source-sync", "schemas/sources.json"),
        ("source-sync", "rules.py"),
        ("source-sync", "examples/documents.json"),
        ("text-dialogue", "schemas/common.json"),
        ("profile-memory", "schemas/profiles.json"),
    ],
)
def test_source_load_reverifies_entire_package_and_dependencies(
    contracts, tmp_path, package, relative
):
    for name in ("text-dialogue", "profile-memory", "source-sync"):
        shutil.copytree(contracts.directory.parent.parent / name, tmp_path / name)
    loaded = Contracts(tmp_path / "text-dialogue/v1")
    loaded.load_profiles()
    target = tmp_path / package / "v1" / relative
    target.write_bytes(target.read_bytes() + b"\n ")
    with pytest.raises(ValueError, match="hash mismatch"):
        loaded.load_sources()
    assert loaded.source_version is None and loaded.source_rules is None


def test_unpublished_extra_schema_cannot_override_verified_dependency(contracts, tmp_path):
    for name in ("text-dialogue", "profile-memory", "source-sync"):
        shutil.copytree(contracts.directory.parent.parent / name, tmp_path / name)
    directory = tmp_path / "text-dialogue/v1"
    (directory / "schemas/z-injected.json").write_text(
        json.dumps({"$id": contracts.schemas["common"]["$id"], "$defs": {"scope": {}}}),
        encoding="utf-8",
    )
    loaded = Contracts(directory)
    loaded.load_sources()
    assert "z-injected" not in loaded.schemas
    with pytest.raises(ValidationError):
        loaded.validate("common#scope", {})
    assert not (tmp_path / "source-sync/v1/__pycache__").exists()


@pytest.mark.parametrize(
    "kind,example", [("facts", "facts_request"), ("current", "access_request")]
)
def test_actual_https_exact_endpoint_credentials_and_environment_isolation(
    transport, source_owner, source_examples, certificates, monkeypatch, kind, example
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", str(certificates / "wrong-ca.pem"))
    body = source_examples[f"self_private/{example}"]
    result = getattr(transport.client, kind)(body)
    assert result["request_id"] == body["request_id"]
    assert len(source_owner.calls) == 1
    path, headers, sent = source_owner.calls[0]
    assert path == getattr(source_owner, kind + "_url").removeprefix(source_owner.base)
    assert headers["Authorization"] == f"Bearer {source_owner.tokens[kind]}"
    assert headers["Accept-Encoding"] == "identity"
    assert sent == body


def test_config_rotates_tokens_trust_and_endpoints_on_every_call(
    transport, source_owner, source_examples, certificates
):
    body = source_examples["self_private/facts_request"]
    transport.client.facts(body)
    source_owner.tokens["facts"] = "new-synthetic-token"
    unavailable(lambda: transport.client.facts(body))
    core = transport.config["source_sync"]["core"]
    core["token"] = source_owner.tokens["facts"]
    transport.save()
    transport.client.facts(body)
    core["ca_file"] = str(certificates / "wrong-ca.pem")
    transport.save()
    unavailable(lambda: transport.client.facts(body))
    assert len(source_owner.calls) == 3
    core["ca_file"] = str(certificates / "ca.pem")
    transport.save()
    transport.client.facts(body)
    core["url"] = source_owner.current_url
    transport.save()
    unavailable(lambda: transport.client.facts(body))
    assert len(source_owner.calls) == 4


@pytest.mark.parametrize("setting", ["default", "wrong", "missing", "malformed", "relative", False])
def test_ca_failures_never_send_credentials(
    transport, source_owner, source_examples, certificates, tmp_path, monkeypatch, setting
):
    core = transport.config["source_sync"]["core"]
    if setting == "default":
        core.pop("ca_file")
        monkeypatch.setenv("SSL_CERT_FILE", str(certificates / "ca.pem"))
        monkeypatch.setenv("SSL_CERT_DIR", str(certificates))
    else:
        malformed = tmp_path / "invalid-ca.pem"
        malformed.write_text("not a certificate", encoding="utf-8")
        core["ca_file"] = {
            "wrong": str(certificates / "wrong-ca.pem"),
            "missing": str(tmp_path / "missing.pem"),
            "malformed": str(malformed),
            "relative": "ca.pem",
            False: False,
        }[setting]
    transport.save()
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert source_owner.calls == []


@pytest.mark.parametrize("source_owner", ["wrong-host", "client-only", "expired"], indirect=True)
def test_tls_hostname_server_purpose_and_expiry(transport, source_owner, source_examples):
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert source_owner.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/internal/v1/source-facts/read",
        "https:///internal/v1/source-facts/read",
        "https://user:secret@127.0.0.1/internal/v1/source-facts/read",
        "https://127.0.0.1/internal/v1/source-facts/read?redirect=yes",
        "https://127.0.0.1/internal/v1/source-facts/read#fragment",
        "https://127.0.0.1:0/internal/v1/source-facts/read",
        "https://127.0.0.1:99999/internal/v1/source-facts/read",
        "https://☃.invalid/internal/v1/source-facts/read",
        "https://127.0.0.1/internal/v1/other",
        " https://127.0.0.1/internal/v1/source-facts/read",
        None,
    ],
)
def test_invalid_urls_fail_before_network(transport, source_owner, source_examples, url):
    transport.config["source_sync"]["core"]["url"] = url
    transport.save()
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert source_owner.calls == []


@pytest.mark.parametrize("token", [None, False, "", "space token", "token\n", "凭据"])
def test_invalid_tokens_fail_before_network(transport, source_owner, source_examples, token):
    transport.config["source_sync"]["core"]["token"] = token
    transport.save()
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert source_owner.calls == []


@pytest.mark.parametrize("raw", ["{}", "[]", '{"source_sync":null}', '{"x":1,"x":2}', '{"x":NaN}'])
def test_missing_or_malformed_config_fails_closed(transport, source_owner, source_examples, raw):
    transport.client.config_path.write_text(raw, encoding="utf-8")
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert source_owner.calls == []


@pytest.mark.parametrize(
    "extra", ['"ignored":NaN', '"ignored":Infinity', '"ignored":1,"ignored":1']
)
def test_nonstandard_json_cannot_hide_in_otherwise_valid_configuration(
    transport, source_owner, source_examples, extra
):
    encoded = json.dumps(transport.config)
    transport.client.config_path.write_text(encoded[:-1] + "," + extra + "}", encoding="utf-8")
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert source_owner.calls == []


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 429, 500, 503])
def test_redirects_and_non_success_responses_are_unavailable(
    transport, source_owner, source_examples, status
):
    source_owner.status = status
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert len(source_owner.calls) == 1
    assert source_owner.calls[0][0] == "/internal/v1/source-facts/read"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1',
        b'{"schema_version":1,"schema_version":1}',
        b'{"head":{"sequence":1,"sequence":1}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b"[]",
        b"null",
        b"{}",
        b"\xff",
        b"{} {}",
    ],
)
def test_strict_json_and_response_schema(transport, source_owner, source_examples, raw):
    source_owner.raw = raw
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert len(source_owner.calls) == 1


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_properties_in_otherwise_valid_response_are_rejected(
    transport, source_owner, source_examples, nested
):
    body = source_examples["self_private/facts_request"]
    response = copy.deepcopy(source_owner.documents["facts"])
    response["request_id"] = body["request_id"]
    encoded = json.dumps(response)
    if nested:
        sequence = response["head"]["sequence"]
        encoded = encoded.replace(
            f'"sequence": {sequence}', f'"sequence": {sequence}, "sequence": {sequence}', 1
        )
    else:
        encoded = encoded[:-1] + ', "schema_version": 1}'
    source_owner.raw = encoded.encode()
    unavailable(lambda: transport.client.facts(body))


@pytest.mark.parametrize(
    "kind,example", [("facts", "facts_request"), ("current", "access_request")]
)
def test_response_request_correlation(transport, source_owner, source_examples, kind, example):
    source_owner.response_request_id = "unrelated-request"
    unavailable(lambda: getattr(transport.client, kind)(source_examples[f"self_private/{example}"]))


@pytest.mark.parametrize("mode", ["declared", "streamed", "truncated", "oversized", "gzip", "html"])
def test_response_size_framing_and_encoding(transport, source_owner, source_examples, mode):
    body = source_examples["self_private/facts_request"]
    response = copy.deepcopy(source_owner.documents["facts"])
    response["request_id"] = body["request_id"]
    encoded = json.dumps(response).encode()
    if mode == "declared":
        source_owner.declared_length = MAX_BODY_BYTES + 1
    elif mode == "streamed":
        source_owner.send_length = False
        source_owner.raw = encoded + b" " * (MAX_BODY_BYTES + 1 - len(encoded))
    elif mode == "truncated":
        source_owner.declared_length = len(encoded) + 1
        source_owner.raw = encoded
    elif mode == "oversized":
        source_owner.raw = encoded + b" " * (MAX_BODY_BYTES + 1 - len(encoded))
    elif mode == "gzip":
        source_owner.encoding = "gzip"
    else:
        source_owner.content_type = "text/html"
    unavailable(lambda: transport.client.facts(body))
    assert len(source_owner.calls) == 1


def test_exact_one_mib_response_is_accepted(transport, source_owner, source_examples):
    body = source_examples["self_private/facts_request"]
    response = copy.deepcopy(source_owner.documents["facts"])
    response["request_id"] = body["request_id"]
    encoded = json.dumps(response).encode()
    source_owner.raw = encoded + b" " * (MAX_BODY_BYTES - len(encoded))
    assert transport.client.facts(body) == response


def test_invalid_request_and_257_coverage_are_never_sent(transport, source_owner, source_examples):
    body = source_examples["self_private/facts_request"]
    oversized = copy.deepcopy(body)
    oversized["selectors"] = [dict(body["selectors"][0], actor_id=f"actor:{i}") for i in range(257)]
    unavailable(lambda: transport.client.facts(oversized))
    unavailable(lambda: transport.client.facts({}))
    assert source_owner.calls == []


def test_exact_256_coverage_is_sent_without_truncation(transport, source_owner, source_examples):
    body = copy.deepcopy(source_examples["self_private/facts_request"])
    body["selectors"] = [dict(body["selectors"][0], actor_id=f"actor:{i}") for i in range(256)]
    transport.client.facts(body)
    assert source_owner.calls[0][2] == body


def test_actual_https_timeout_is_unavailable(transport, source_owner, source_examples):
    source_owner.delay = 5.5
    before = time.monotonic()
    unavailable(lambda: transport.client.facts(source_examples["self_private/facts_request"]))
    assert 4.5 <= time.monotonic() - before < 7.5
    assert len(source_owner.calls) == 1
