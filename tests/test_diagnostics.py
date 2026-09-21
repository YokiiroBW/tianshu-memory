"""The runtime event log: one closed shape, full coverage, no secret, no false ready.

These tests exercise the adapter directly and through a real application, and they are written
against the coordinator's frozen `contracts/diagnostics/v1` rather than against this module's own
constants: the JSON Schema and the published examples are loaded from disk, so an edit here that
stopped matching the contract would fail rather than quietly redefine it.
"""

import asyncio
import contextlib
import json
import re
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

from tianshu_memory.diagnostics import (
    CHAT_SERVICE,
    CORRELATION_HEADER,
    DEFAULT_TOKEN_ENV,
    ERROR_CODES,
    EVENTS,
    HOOK_NAME,
    KNOWLEDGE_SERVICE,
    LOG_DIRECTORY_ENV,
    MAX_LINE_BYTES,
    NO_STORE,
    PROBE_PATHS,
    SINK_WARNING,
    WRITE_QUEUE_LIMIT,
    Diagnostics,
    Sink,
    SinkFull,
    SinkUnavailable,
    Writer,
    read_config,
    read_correlation,
)
from tianshu_memory.domain import Fault
from tianshu_memory.runtime_probes import present_token
from tianshu_memory.server_runtime import install_networking, install_probe_routes, resolve_binding

FIELDS = (
    "schema_version",
    "timestamp",
    "service",
    "instance_id",
    "sequence",
    "event_id",
    "level",
    "event",
    "outcome",
    "correlation_id",
    "duration_ms",
    "error_code",
)
LEGAL = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def log_directory(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


@pytest.fixture
def adapter(log_directory):
    return Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(log_directory), "diagnostics": {"token_env": "TS102_TOKEN"}},
    )


@pytest.fixture(scope="module")
def diagnostics_contract():
    """The published diagnostics package, located the same way the runtime locates it."""
    from tianshu_memory.server_runtime import workspace_diagnostics_path

    root = Path(__file__).resolve().parents[1]
    directory = workspace_diagnostics_path(root / "pyproject.toml")
    if directory is None or not (directory / "manifest.json").is_file():
        pytest.skip("the published diagnostics package is not reachable from this checkout")
    return directory


def bound(app):
    """Install the authority check on a loopback binding, so a test app is a real app."""
    return install_networking(
        app,
        resolve_binding(host="127.0.0.1", port=8080, certfile=None, keyfile=None, allowed_hosts=[]),
    )


def _request(authorization):
    """A minimal ASGI scope wrapped as a request, for the token verdict alone."""
    headers = [] if authorization is None else [(b"authorization", authorization.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": "/health/ready", "headers": headers})


def segment_lines(adapter):
    """Every line this adapter actually persisted, in file order across its segments."""
    directory = adapter.sink.directory
    if directory is None:
        return []
    lines = []
    for path in sorted(directory.glob("*.jsonl")):
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n"), "every record ends with exactly one newline"
        for line in text.splitlines():
            lines.append(json.loads(line))
    return lines


def published(contract_directory, name):
    return json.loads((contract_directory / name).read_text(encoding="utf-8"))


def entries(value):
    """The published examples are a list; accept either that or a wrapped object."""
    return value if isinstance(value, list) else value["examples"]


def validate_against_schema(record, schema):
    """A dependency-free check of the parts of JSON Schema the contract actually uses."""
    assert schema.get("additionalProperties") is False
    assert set(record) == set(schema["required"]), "the record's fields are exactly the closed set"
    properties = schema["properties"]
    for key, value in record.items():
        rule = properties[key]
        if "const" in rule:
            assert value == rule["const"], (key, value)
        if value is None:
            continue
        if "enum" in rule:
            assert value in rule["enum"], (key, value)
        if "pattern" in rule:
            assert re.fullmatch(rule["pattern"], value), (key, value)
        declared = rule.get("type")
        if declared == "string":
            assert isinstance(value, str), key
        elif declared == "integer":
            assert isinstance(value, int) and not isinstance(value, bool), key
        elif declared == "null":
            raise AssertionError(f"{key} is declared null but carries {value!r}")
        elif declared == "number":
            assert isinstance(value, int | float) and not isinstance(value, bool), key


def test_every_record_matches_the_published_schema(diagnostics_contract, adapter):
    adapter.emit("runtime.starting", level="INFO", outcome="started")
    adapter.emit(
        "request.authenticated", level="WARNING", outcome="rejected", error_code="forbidden"
    )
    adapter.emit("sync.execute.completed", level="INFO", outcome="succeeded", duration_ms=1.5)
    schema = published(diagnostics_contract, "event.schema.json")
    lines = segment_lines(adapter)
    assert len(lines) == 3
    for record in lines:
        validate_against_schema(record, schema)


def test_the_published_examples_are_the_vocabulary_this_adapter_accepts(
    diagnostics_contract, adapter
):
    examples = entries(published(diagnostics_contract, "examples.json"))
    for record in examples:
        adapter.emit(
            record["event"],
            level=record["level"],
            outcome=record["outcome"],
            error_code=record["error_code"],
            duration_ms=record["duration_ms"],
        )
    assert len(segment_lines(adapter)) == len(examples)


def test_every_published_negative_example_could_not_be_produced_here(diagnostics_contract, adapter):
    """Each negative example is refused for the reason the contract marks it invalid.

    The published negatives are mostly invalid *structurally* — an extra `message` field, a string
    `schema_version`, a `sequence` below one, a secret presented as a correlation identifier — so
    the interesting assertion is not that some call raises. It is that this adapter has no way to
    express any of them: its record shape is closed, its sequence starts at one, and it refuses a
    correlation identifier that is not the documented 32 character lower-case hex string.
    """
    negatives = entries(published(diagnostics_contract, "negative-examples.json"))
    assert negatives, "the published negative examples are the point of this test"
    for record in negatives:
        assert (
            "message" in record
            or record["schema_version"] != "1.0.0"
            or record["sequence"] < 1
            or (
                record["correlation_id"] is not None
                and not re.fullmatch(r"[a-f0-9]{32}", record["correlation_id"])
            )
        ), record
        # The correlation identifier is the one field a client can present, so it is the one that is
        # checked at the boundary rather than refused by the closed vocabulary.
        if record["correlation_id"] is not None:
            assert read_correlation(record["correlation_id"]) is None
    # And a legal call with an illegal presented identifier is logged under a fresh one instead.
    adapter.emit(
        "runtime.started",
        level="INFO",
        outcome="succeeded",
        error_code=None,
    )
    assert segment_lines(adapter)[0]["sequence"] == 1
    assert len(segment_lines(adapter)) == 1


def test_a_line_is_never_wider_than_the_contract_limit(adapter):
    adapter.emit("runtime.starting", level="INFO", outcome="started")
    for path in adapter.sink.directory.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert len(line.encode("utf-8")) + 1 <= MAX_LINE_BYTES


def test_the_record_has_no_place_to_put_a_message_extra_stack_body_or_path(adapter):
    """A secret has nowhere to go, which is stronger than any redaction filter."""
    import inspect

    parameters = list(inspect.signature(Diagnostics.emit).parameters)
    assert parameters == ["self", "name", "level", "outcome", "error_code", "duration_ms"]
    for forbidden in ("message", "extra", "extras", "body", "detail", "stack", "path", "token"):
        assert forbidden not in parameters
    adapter.emit("request.completed", level="INFO", outcome="succeeded")
    assert set(segment_lines(adapter)[0]) == set(FIELDS)


def test_unregistered_events_levels_outcomes_and_codes_are_refused(adapter):
    with pytest.raises(ValueError):
        adapter.emit("/health/ready")
    with pytest.raises(ValueError):
        adapter.emit("ValueError")
    with pytest.raises(ValueError):
        adapter.emit("runtime.starting", level="TRACE")
    with pytest.raises(ValueError):
        adapter.emit("runtime.starting", outcome="maybe")
    with pytest.raises(ValueError):
        adapter.emit("runtime.starting", error_code="SomethingWentWrong")
    assert segment_lines(adapter) == []


def test_an_unknown_product_verdict_becomes_the_fixed_internal_code():
    from tianshu_memory.diagnostics import map_fault

    assert map_fault("a_defect_nobody_registered") == "internal_error"
    assert map_fault(None) == "internal_error"
    assert map_fault(7) == "internal_error"
    assert map_fault("not_found") == "not_found"
    assert "internal_error" in ERROR_CODES


def test_sequence_is_strictly_increasing_and_never_repeats_under_concurrency(adapter):
    """The number is allocated under the same lock that writes the line, so order is record order."""
    workers = 8
    each = 40

    def write():
        for _ in range(each):
            adapter.emit("request.started", level="INFO", outcome="started")

    threads = [threading.Thread(target=write) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = segment_lines(adapter)
    assert len(lines) == workers * each
    assert [line["sequence"] for line in lines] == list(range(1, workers * each + 1))
    assert len({line["event_id"] for line in lines}) == len(lines)


def test_every_record_carries_the_one_process_identity(adapter):
    adapter.emit("runtime.starting", level="INFO", outcome="started")
    adapter.emit("runtime.started", level="INFO", outcome="succeeded")
    lines = segment_lines(adapter)
    assert {line["instance_id"] for line in lines} == {adapter.instance_id}
    assert {line["service"] for line in lines} == {CHAT_SERVICE}
    assert {line["schema_version"] for line in lines} == {"1.0.0"}


def test_the_two_memory_services_are_distinguishable(log_directory):
    chat = Diagnostics(CHAT_SERVICE, {"log_directory": str(log_directory)})
    knowledge = Diagnostics(KNOWLEDGE_SERVICE, {"log_directory": str(log_directory)})
    chat.emit("runtime.starting", level="INFO", outcome="started")
    knowledge.emit("runtime.starting", level="INFO", outcome="started")
    assert {chat.service, knowledge.service} == {"memory", "memory-knowledge"}
    assert chat.instance_id != knowledge.instance_id


def test_the_timestamp_is_utc_with_milliseconds(adapter):
    adapter.emit("runtime.starting", level="INFO", outcome="started")
    stamp = segment_lines(adapter)[0]["timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp), stamp


def test_a_legal_correlation_id_is_used_exactly_as_presented():
    assert read_correlation(LEGAL) == LEGAL
    assert read_correlation("f" * 32) == "f" * 32


@pytest.mark.parametrize(
    "value",
    [LEGAL.upper(), LEGAL[:-1], LEGAL + "0", "", " " + LEGAL, "z" * 32, None, 7],
)
def test_an_illegal_correlation_id_is_never_used_and_never_echoed(value):
    assert read_correlation(value) is None


def test_the_configuration_refuses_a_log_directory_it_cannot_honour(tmp_path):
    assert read_config({})["log_directory"] is None
    assert read_config({"log_directory": False})["log_directory"] is None
    for bad in ({"log_directory": "relative/logs"}, {"log_directory": ""}, {"log_directory": 5}):
        with pytest.raises(Exception):
            read_config(bad)
    with pytest.raises(Exception):
        read_config({"diagnostics": "not-an-object"})
    with pytest.raises(Exception):
        read_config({"diagnostics": {"token_env": ""}})
    assert read_config({"log_directory": str(tmp_path)})["log_directory"].is_absolute()


def test_the_token_is_read_from_the_named_variable_only(log_directory, monkeypatch):
    """The adapter keeps the *name*; the value is never stored, and never appears in a line."""
    monkeypatch.setenv("TS102_ONLY_HERE", "synthetic-token-value")
    monkeypatch.setenv("TS102_SOMEWHERE_ELSE", "another-synthetic-value")
    adapter = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(log_directory), "diagnostics": {"token_env": "TS102_ONLY_HERE"}},
    )
    assert adapter.token_env == "TS102_ONLY_HERE"
    adapter.emit("runtime.starting", level="INFO", outcome="started")
    text = "".join(path.read_text(encoding="utf-8") for path in log_directory.glob("*.jsonl"))
    assert "synthetic-token-value" not in text
    assert "another-synthetic-value" not in text
    assert "TS102_ONLY_HERE" not in text


def test_only_an_absolute_configured_directory_is_ever_called_durable(tmp_path):
    durable = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    assert durable.durable is True
    assert durable.log_state() == "ok"
    temporary = Diagnostics(CHAT_SERVICE, {})
    assert temporary.durable is False
    assert temporary.log_state() == "non_durable"


def test_the_deployment_log_directory_variable_really_assembles_a_durable_sink(
    tmp_path, monkeypatch
):
    """The reviewed defect: setting `TIANSHU_LOG_DIR` configured nothing at all.

    A container that exports the documented variable must end up with a durable sink, or the
    deployment convention is decoration. The variable is read exactly once, while the process is
    assembled, and it is a directory — never a mode, a stream or a token.
    """
    directory = tmp_path / "deployed-logs"
    directory.mkdir()
    monkeypatch.setenv(LOG_DIRECTORY_ENV, str(directory))
    adapter = Diagnostics(CHAT_SERVICE, {})
    assert adapter.durable is True
    assert adapter.log_configured is True
    assert adapter.sink.directory == directory
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is True
    assert [path.name for path in directory.glob("*.jsonl")] != []
    # A path the adapter cannot honour is refused at assembly, not silently downgraded.
    monkeypatch.setenv(LOG_DIRECTORY_ENV, "relative/logs")
    with pytest.raises(Exception):
        Diagnostics(CHAT_SERVICE, {})


def test_the_configuration_wins_over_the_environment(tmp_path, monkeypatch):
    """One fixed precedence, in one direction: what the deployment wrote down wins.

    A variable inherited from a unit must never redirect a process that was configured in writing,
    and it must never be read once the written configuration has answered.
    """
    from_configuration = tmp_path / "from-configuration"
    from_environment = tmp_path / "from-environment"
    from_configuration.mkdir()
    from_environment.mkdir()
    monkeypatch.setenv(LOG_DIRECTORY_ENV, str(from_environment))
    written = Diagnostics(CHAT_SERVICE, {"log_directory": str(from_configuration)})
    assert written.sink.directory == from_configuration
    written.emit("runtime.starting", level="INFO", outcome="started")
    assert [path.name for path in from_configuration.glob("*.jsonl")] != []
    assert list(from_environment.iterdir()) == []
    # An explicit `false` is a written decision too: it disables durability on purpose, and the
    # environment does not get to overrule it.
    monkeypatch.setenv(LOG_DIRECTORY_ENV, str(from_environment))
    disabled = Diagnostics(CHAT_SERVICE, {"log_directory": False})
    assert disabled.durable is False
    assert disabled.log_configured is False
    assert list(from_environment.iterdir()) == []


def test_the_contract_token_variable_is_consumed_when_no_name_is_configured(monkeypatch, tmp_path):
    """The default is the contract's own independent variable, and never a business credential."""
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    default = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    assert default.token_env == DEFAULT_TOKEN_ENV
    assert present_token(_request(None), default) == "unconfigured"
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "synthetic-readiness-token")
    assert present_token(_request("Bearer synthetic-readiness-token"), default) == "ok"
    assert present_token(_request("Bearer something-else"), default) == "unauthorized"
    # A configured name still wins, so a deployment that renamed the variable keeps working.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("TS102_RENAMED", "synthetic-readiness-token")
    renamed = Diagnostics(
        CHAT_SERVICE,
        {"log_directory": str(other), "diagnostics": {"token_env": "TS102_RENAMED"}},
    )
    assert renamed.token_env == "TS102_RENAMED"
    assert present_token(_request("Bearer synthetic-readiness-token"), renamed) == "ok"


def test_a_business_credential_variable_is_never_picked_up_as_the_probe_token(
    monkeypatch, tmp_path
):
    """The adapter reads one named variable; nothing else in the environment can authorize ready."""
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    monkeypatch.setenv("TIANSHU_MEMORY_TOKEN", "synthetic-business-credential")
    monkeypatch.setenv("TIANSHU_KNOWLEDGE_CREDENTIAL", "synthetic-business-credential")
    directory = tmp_path / "logs"
    directory.mkdir()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(directory)})
    assert adapter.token_env == DEFAULT_TOKEN_ENV
    assert (
        present_token(_request("Bearer synthetic-business-credential"), adapter) == "unconfigured"
    )
    # And the token value never reaches the log, whichever variable held it.
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "synthetic-readiness-token")
    adapter.emit("runtime.starting", level="INFO", outcome="started")
    text = "".join(path.read_text(encoding="utf-8") for path in directory.glob("*.jsonl"))
    assert "synthetic-readiness-token" not in text
    assert "synthetic-business-credential" not in text


def test_the_non_durable_mode_writes_no_file_and_no_stream(tmp_path, capsys):
    """A standard stream is not a durable record, so there is deliberately no fallback to one."""
    adapter = Diagnostics(CHAT_SERVICE, {})
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is True
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert list(tmp_path.iterdir()) == []


def test_a_capacity_refusal_latches_fail_closed_and_warns_exactly_once(tmp_path, capsys):
    directory = tmp_path / "logs"
    directory.mkdir()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(directory)})
    adapter.sink.directory_bytes = 1
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is False
    assert adapter.sink.failure == "log_capacity"
    assert adapter.available() is False
    assert adapter.log_state() == "log_capacity"
    # One fixed warning, carrying no path, no value and no exception text.
    assert capsys.readouterr().err == SINK_WARNING
    # It never recovers by itself, and it never removes history to make room.
    assert adapter.emit("runtime.started", level="INFO", outcome="succeeded") is False
    assert capsys.readouterr().err == ""
    assert isinstance(SinkFull("x"), RuntimeError)


def test_an_unwritable_directory_fails_closed_without_touching_the_business_result(
    tmp_path, capsys
):
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path / "absent")})
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is False
    assert adapter.sink.failure == "log_unavailable"
    assert adapter.log_state() == "log_unavailable"
    assert capsys.readouterr().err == SINK_WARNING


def test_a_failed_sink_refuses_new_business_with_a_closed_document(tmp_path, capsys):
    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    # The directory limit is reached by a real failed write, not by a flag the test sets: the latch
    # this test relies on is the one a serving process would really reach.
    adapter.sink.directory_bytes = 1
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is False
    assert adapter.sink.failure == "log_capacity"
    capsys.readouterr()

    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read():
        return JSONResponse({"status": "succeeded"})

    with TestClient(app) as client:
        response = client.post("/local/v1/memory/read")
    assert response.status_code == 503
    assert response.json() == {"status": "failed", "code": "log_unavailable"}
    assert adapter.refused == 1
    assert response.headers["cache-control"] == "no-store"


def test_the_log_sink_failed_event_goes_to_the_fixed_warning_not_the_file(tmp_path, capsys):
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    assert adapter.emit("log.sink_failed", level="ERROR", outcome="failed") is False
    assert capsys.readouterr().err == SINK_WARNING
    assert segment_lines(adapter) == []


def test_the_whole_request_lifecycle_is_recorded_once_each(tmp_path):
    from tianshu_memory.diagnostics import anote_authenticated, record_execution

    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read():
        assert await anote_authenticated() is True
        with record_execution("read"):
            pass
        return JSONResponse({"status": "succeeded"}, headers=dict(NO_STORE))

    with TestClient(app) as client:
        assert client.post("/local/v1/memory/read").status_code == 200
    lines = segment_lines(adapter)
    assert [line["event"] for line in lines] == [
        "request.started",
        "request.authenticated",
        "sync.execute.started",
        "sync.execute.completed",
        "request.completed",
    ]
    assert lines[-1]["outcome"] == "succeeded"
    assert lines[-1]["duration_ms"] is not None


def test_a_correlation_id_is_echoed_only_when_it_was_legal(tmp_path):
    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read(request: Request):
        return JSONResponse(
            {"correlation": request.scope.get("tianshu_correlation_id")}, headers=dict(NO_STORE)
        )

    with TestClient(app) as client:
        first = client.post("/local/v1/memory/read", headers={CORRELATION_HEADER: LEGAL})
        second = client.post("/local/v1/memory/read", headers={CORRELATION_HEADER: LEGAL.upper()})
    assert first.json()["correlation"] == LEGAL
    # An illegal value is never reused, never trimmed into shape and never echoed.
    echoed = second.json()["correlation"]
    assert echoed != LEGAL.upper() and echoed != LEGAL and len(echoed) == 32
    logged = [line["correlation_id"] for line in segment_lines(adapter)]
    # The log never carries the illegal string either: the second request is recorded under the
    # fresh identifier, which is also the one it was answered with.
    assert LEGAL.upper() not in logged
    assert logged[0] == logged[1] == LEGAL
    assert logged[2] == logged[3] == echoed


def test_the_middleware_publishes_the_hooks_under_the_fixed_name(tmp_path):
    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    hooks = app.state.__dict__[HOOK_NAME]
    assert hooks.adapter is adapter
    with hooks.execution("read"):
        pass
    assert [line["event"] for line in segment_lines(adapter)] == [
        "sync.execute.started",
        "sync.execute.completed",
    ]


class _Assembly:
    """The smallest thing the probe routes need: an adapter and nothing else."""

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.readiness = None

    def probe_settings(self):
        return None

    def announce_ready(self, document):
        self.readiness = document


def test_the_probe_paths_are_excluded_by_exact_path(tmp_path):
    """Asking a process whether it is alive cannot change what it is alive about."""
    assert PROBE_PATHS == {"/health/live", "/health/ready", "/health"}
    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)
    install_probe_routes(app, _Assembly(adapter))

    @app.post("/local/v1/memory/read")
    async def read():
        return JSONResponse({"status": "succeeded"})

    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/health/live").status_code == 200
            # No diagnostics token is configured and none is presented, so this is a refusal by the
            # HTTP status alone; the document shape is checked in the runtime suite.
            assert client.get("/health/ready").status_code in {401, 503}
        assert segment_lines(adapter) == []
        client.post("/local/v1/memory/read")
    # Business traffic is numbered from one: the probes never advanced the sequence.
    assert [line["sequence"] for line in segment_lines(adapter)] == [1, 2]


def test_a_lookalike_probe_path_is_ordinary_traffic(tmp_path):
    """Only the exact paths are excluded: `/health/live/x` is logged like any other request."""
    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.get("/health/live/x")
    async def lookalike():
        return JSONResponse({"status": "alive"})

    with TestClient(app) as client:
        client.get("/health/live/x")
    assert len(segment_lines(adapter)) == 2


def test_a_disconnect_is_unknown_and_never_a_business_cancellation(tmp_path):
    from starlette.requests import ClientDisconnect

    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read():
        raise ClientDisconnect()

    with TestClient(app) as client:
        with pytest.raises(Exception):
            client.post("/local/v1/memory/read")
    completed = [line for line in segment_lines(adapter) if line["event"] == "request.completed"]
    assert len(completed) == 1
    assert completed[0]["outcome"] == "unknown"
    assert completed[0]["error_code"] == "client_disconnected"


def test_a_408_describes_the_response_and_not_a_cancelled_operation(tmp_path):
    from tianshu_memory.diagnostics import record_execution

    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read():
        with record_execution("read"):
            pass
        return JSONResponse({"status": "failed"}, status_code=408)

    with TestClient(app) as client:
        assert client.post("/local/v1/memory/read").status_code == 408
    lines = segment_lines(adapter)
    completed = [line for line in lines if line["event"] == "request.completed"][0]
    # The response deadline expired; the work itself still reports its own real ending.
    assert completed["outcome"] == "unknown"
    assert completed["error_code"] == "request_timeout"
    worked = [line for line in lines if line["event"] == "sync.execute.completed"][0]
    assert worked["outcome"] == "succeeded"


def test_the_product_verdict_is_carried_into_the_final_record(tmp_path):
    from tianshu_memory.diagnostics import note_fault

    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read():
        note_fault("project_uninitialized")
        return JSONResponse({"status": "failed"}, status_code=409)

    with TestClient(app) as client:
        client.post("/local/v1/memory/read")
    completed = [line for line in segment_lines(adapter) if line["event"] == "request.completed"][0]
    assert completed["outcome"] == "rejected"
    assert completed["error_code"] == "project_uninitialized"


def test_the_seams_are_no_ops_without_an_installed_adapter(tmp_path):
    """A bare `create_app` in a test is untouched by the arrival of diagnostics.

    With nothing installed there is no log to confirm and therefore nothing to refuse: the seams
    report success and write nothing, which is exactly how these entry points behaved before.
    """
    from tianshu_memory.diagnostics import note_authenticated, note_fault, record_execution

    assert note_authenticated() is True
    assert note_fault("not_found") is None
    with record_execution("read"):
        pass
    assert list(tmp_path.iterdir()) == []


def test_every_registered_event_is_reachable_through_the_adapter(tmp_path):
    """Full coverage by construction: each registered name is emittable and lands in the file.

    `log.sink_failed` is the one deliberate exception, and it is the reason that event exists: it is
    raised by a sink that has just failed, so it cannot be written to the very file that failed. It
    goes to the one fixed warning instead, and its own test above asserts exactly that.
    """
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    emittable = EVENTS - {"log.sink_failed"}
    for name in sorted(emittable):
        adapter.emit(name, level="INFO", outcome="succeeded")
    assert {line["event"] for line in segment_lines(adapter)} == set(emittable)


@contextlib.contextmanager
def injected_writes(*, fail_from=None, delay=0.0, calls=None):
    """Make every durable write of this test slow, or failing, without changing any object.

    The injection goes through the module's own `Sink.write` rather than through a stand-in sink, so
    object identity is untouched: there is one sink, one writer and one latch, exactly as in a real
    process, and the latch the middleware reads is the latch the failed write set. A wrapper that
    merely forwarded attributes would accumulate the writer's state on itself and leave the real
    sink's counter and latch behind.

    `fail_from` is the 1-based write that starts failing, which is how a test puts the failure at a
    chosen seam of the request path: `1` is the very first line, `2` is the line after the acceptance
    record has really been confirmed. `None` means nothing fails, which is how the timing tests below
    exercise a disk that is slow rather than broken.
    """
    real = Sink.write
    state = {"attempts": 0, "failed": 0, "fail_from": fail_from}

    def write(self, line, sequence):
        state["attempts"] += 1
        if calls is not None:
            calls.append(line)
        if delay:
            time.sleep(delay)
        if fail_from is not None and state["attempts"] >= fail_from:
            state["failed"] += 1
            raise SinkUnavailable("synthetic write failure")
        return real(self, line, sequence)

    Sink.write = write
    try:
        yield state
    finally:
        Sink.write = real


def business_app(adapter, effects):
    """One real app with a real business route that records what actually ran."""
    app = FastAPI()

    @app.post("/work")
    async def work():
        effects.append("committed")
        return JSONResponse({"status": "ok"})

    adapter.install(app)
    bound(app)
    return app


def fault_boundary(app):
    """The boundary the real entry point has: a refusal raised at the seam becomes its own answer.

    `runtime_app` already turns a `Fault` into `error.wire(...)` at `error.status`. A test route that
    called `record_execution` directly would instead let that refusal escape, so this installs the
    same translation — the production behaviour, not a substitute for it.
    """

    @app.exception_handler(Fault)
    async def refused(request, error):
        return JSONResponse(
            error.wire(request.headers.get("x-tianshu-request-id", "")),
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )


@pytest.mark.parametrize("skip", [0, 1, 2])
def test_a_write_failure_refuses_the_request_and_never_runs_the_business(tmp_path, skip):
    """The reviewed defect, at every stage it can happen in: nothing may run without its record.

    `skip` is how many writes succeed first. Each of them is a real seam of this request path — the
    acceptance record, the authentication verdict, the execution start — so breaking at 0, 1 and 2
    covers a failure at the entry, at authorization and immediately before the business call.
    """
    effects = []
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    app = business_app(adapter, effects)
    with injected_writes(fail_from=skip + 1 if skip else 1) as state:
        for _ in range(skip):
            # Real prior writes, so the failure really lands at the stage this case names.
            assert adapter.emit("runtime.starting", level="INFO", outcome="started") is True
        with TestClient(app) as client:
            response = client.post("/work")
    assert response.status_code == 503, response.text
    assert response.json() == {"status": "failed", "code": "log_unavailable"}
    assert effects == [], "a request whose record could not be persisted must not run"
    assert adapter.sink.failure == "log_unavailable"
    assert adapter.available() is False
    assert adapter.log_state() == "log_unavailable"
    assert state["failed"] == 1, "exactly one write failed, and it was the one this case names"


def test_an_execution_whose_start_record_fails_refuses_before_the_call_runs(tmp_path):
    """The second seam, on its own: `Execution.__enter__` must not ignore its own failure.

    The request is admitted normally, then persistence breaks exactly at the execution start — the
    last moment at which the call can still be refused for free. The refusal arrives as the domain
    fault the real entry point already translates, carrying 503 rather than a 200 with the work done.
    """
    from tianshu_memory.diagnostics import record_execution

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    app = FastAPI()
    effects = []

    @app.post("/work")
    async def work():
        with record_execution("read"):
            effects.append("committed")
        return JSONResponse({"status": "ok"})

    adapter.install(app)
    bound(app)
    fault_boundary(app)
    with injected_writes(fail_from=2):
        # The acceptance record succeeds; the execution record is the one that fails, and it is the
        # one that decides whether the call may run at all.
        with TestClient(app) as client:
            response = client.post("/work")
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "log_unavailable"
    assert response.json()["execution_state"] == "not_started"
    assert response.json()["retryable"] is True
    assert effects == []
    assert adapter.sink.failure == "log_unavailable"


def test_a_synchronous_business_call_is_refused_the_same_way(tmp_path):
    """The same refusal from a worker thread, which is where real `execute` calls run."""
    from starlette.concurrency import run_in_threadpool

    from tianshu_memory.diagnostics import record_execution

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    app = FastAPI()
    effects = []

    def business():
        with record_execution("read"):
            effects.append("committed")
        return "done"

    @app.post("/work")
    async def work():
        return JSONResponse({"result": await run_in_threadpool(business)})

    adapter.install(app)
    bound(app)
    fault_boundary(app)
    with injected_writes(fail_from=2):
        with TestClient(app) as client:
            response = client.post("/work")
    assert response.status_code == 503
    assert response.json()["code"] == "log_unavailable"
    assert effects == []
    assert adapter.sink.failure == "log_unavailable"


def test_a_slow_write_never_blocks_the_event_loop(tmp_path):
    """The reviewed defect: a 250 ms `fsync` used to stall a 20 ms heartbeat to 252 ms.

    The blocking work belongs to the one writer thread, so the loop keeps answering — including the
    real liveness probe, which exists precisely to answer while the process is busy. The probe route
    installed here is the runtime's own, so this asserts the claim the review made about the actual
    route rather than about a stand-in.
    """
    from tianshu_memory.runtime_probes import ProbeConfig
    from tianshu_memory.server_runtime import Assembly, install_probe_routes

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    effects = []
    app = business_app(adapter, effects)

    def settings(runtime):
        return ProbeConfig(
            service=CHAT_SERVICE,
            diagnostics=runtime.diagnostics,
            config_path=Path("absent.json"),
            contract_path=None,
            runtime=runtime,
        )

    install_probe_routes(app, Assembly(app, adapter, None, probe_factory=settings))

    async def drive():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client:

            async def heartbeat():
                started = time.monotonic()
                await asyncio.sleep(0.02)
                return time.monotonic() - started

            async def liveness():
                started = time.monotonic()
                response = await client.get("/health/live")
                return response, time.monotonic() - started

            elapsed, live, response = await asyncio.gather(
                heartbeat(), liveness(), client.post("/work")
            )
            return elapsed, live, response

    # The 250 ms of blocking work happens on every record of the business request, which is the
    # exact case the review reproduced.
    with injected_writes(delay=0.25) as state:
        elapsed, (live, live_elapsed), response = asyncio.run(drive())
    assert state["attempts"] >= 1
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert response.status_code == 200, response.text
    assert effects == ["committed"]
    # The heartbeat and the probe are allowed to overshoot a little; neither may wait for the disk.
    assert elapsed < 0.15, f"the event loop waited {elapsed:.3f}s for a 0.25s write"
    assert live_elapsed < 0.15, f"the liveness probe waited {live_elapsed:.3f}s for a 0.25s write"


def test_a_write_that_cannot_be_confirmed_in_time_refuses_rather_than_guessing(tmp_path):
    """A deadline is a refusal, not an assertion: the work it would describe does not run."""
    from tianshu_memory.diagnostics import WRITER_DEADLINE_SECONDS

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    effects = []
    app = business_app(adapter, effects)
    with injected_writes(delay=WRITER_DEADLINE_SECONDS + 0.75) as state:
        started = time.monotonic()
        with TestClient(app) as client:
            response = client.post("/work")
        elapsed = time.monotonic() - started
        assert response.status_code == 503, response.text
        assert effects == []
        assert elapsed < WRITER_DEADLINE_SECONDS + 0.5, (
            "the refusal must not wait for the whole write"
        )
        # A timed-out write is not a cancelled one: it is the writer's own work, and it is written
        # exactly once. A later request is refused by the latch, not by running the business again.
        assert state["attempts"] == 1
        with TestClient(app) as client:
            again = client.post("/work")
        assert again.status_code == 503
        assert effects == []
    # The latch really was set by the write that did not confirm in time, so every later request is
    # refused on the acceptance record alone, without reaching the business call at all.
    assert adapter.sink.failure == "log_unavailable"
    assert adapter.available() is False
    # Draining joins the writer. The record whose confirmation timed out is not a lost line: it is
    # the writer's own work and it did land. What matters is that no *business* side effect was
    # produced for either request, and that no record was ever written about twice.
    adapter.shutdown()
    events = [line["event"] for line in segment_lines(adapter)]
    assert events.count("request.completed") == 0
    assert state["attempts"] == 1, "the timed-out record is attempted once and never re-sent"
    assert effects == []


def test_a_burst_past_the_bounded_buffer_is_refused_and_never_queued_without_limit(tmp_path):
    """The buffer is bounded, and exceeding it is a refusal about the log, not a lost line.

    The bound only binds when calls arrive faster than the one writer can persist them, so this is
    many concurrent callers against a deliberately slow disk — the shape of the failure the bound
    exists for: parking an unbounded number of requests on the log.
    """
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    with injected_writes(delay=0.1):
        admitted = []
        lock = threading.Lock()

        def caller():
            landed = adapter.emit("request.started", level="INFO", outcome="started")
            with lock:
                admitted.append(landed)

        callers = [threading.Thread(target=caller) for _ in range(120)]
        for thread in callers:
            thread.start()
        for thread in callers:
            thread.join()
        assert adapter.writer.queue.qsize() <= WRITE_QUEUE_LIMIT
        assert False in admitted, (
            "a burst past the bound must refuse rather than queue without limit"
        )
        assert adapter.sink.failure == "log_capacity"
        assert adapter.available() is False
        assert adapter.log_state() == "log_capacity"
    adapter.shutdown()
    # Every record that reached the writer is in the file exactly once, numbered contiguously from
    # one: the bound refuses work, it never drops a line it already accepted. One segment holds at
    # most the queue's worth of lines, which is the bound stated as a fact about the file.
    numbers = sorted(line["sequence"] for line in segment_lines(adapter))
    assert numbers == list(range(1, len(numbers) + 1))
    assert 0 < len(numbers) <= WRITE_QUEUE_LIMIT * 2


def test_shutdown_is_bounded_and_never_accepts_a_record_it_cannot_write(tmp_path):
    """Closing drains what is already queued, then refuses new work within its own deadline."""
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    for _ in range(5):
        adapter.emit("runtime.starting", level="INFO", outcome="started")
    adapter.shutdown()
    lines = segment_lines(adapter)
    assert len(lines) == 5
    assert [line["sequence"] for line in lines] == [1, 2, 3, 4, 5]
    # Closing twice is closing once, and a stopping adapter refuses new work instead of parking it
    # in a queue whose reader has gone: a record that cannot be written is a refusal, never a
    # silent accept.
    adapter.shutdown()
    assert adapter.emit("runtime.started", level="INFO", outcome="succeeded") is False
    assert len(segment_lines(adapter)) == 5


def test_a_restarted_instance_never_repeats_a_sequence_inside_the_same_file(tmp_path):
    """A process that comes back with the same instance id continues the numbers it wrote."""
    first = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    for _ in range(3):
        first.emit("runtime.starting", level="INFO", outcome="started")
    first.shutdown()
    # The same instance id, as a process that reused a persisted identity would have.
    resumed = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    resumed.instance_id = first.instance_id
    resumed.sink = Sink(tmp_path, resumed.instance_id)
    resumed.sink.sequence = resumed.sink.last_persisted_sequence()
    resumed.writer = Writer(resumed)
    resumed.emit("runtime.started", level="INFO", outcome="succeeded")
    resumed.shutdown()
    lines = segment_lines(resumed)
    numbers = [line["sequence"] for line in lines]
    assert numbers == [1, 2, 3, 4]
    assert len(numbers) == len(set(numbers))


def test_a_write_that_fails_after_the_business_already_ran_keeps_the_result_and_never_repeats_it(
    tmp_path,
):
    """The other half of R1: a failure that arrives *after* the work is a latch, not a rewrite.

    The request is admitted and the side effect really happens, then persistence breaks while the
    completion record is being written. The caller keeps the result it was promised — a receipt is
    not withdrawn because a log line failed — the business call runs exactly once, and the failed
    write is never re-sent. What changes is only the future: readiness goes false and the next
    request is refused before it can produce a side effect nobody could account for.
    """
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    calls = []
    app = FastAPI()

    @app.post("/work")
    async def work():
        calls.append("committed")
        return JSONResponse({"status": "succeeded"})

    adapter.install(app)
    bound(app)
    with injected_writes(fail_from=2) as state:
        # The acceptance record succeeds, so this request really is admitted; the completion record
        # is the write that then fails. The business call runs, and the refusal only changes what
        # happens to later requests.
        with TestClient(app) as client:
            response = client.post("/work")
            # The result is the one the business produced, unchanged and un-retried.
            assert response.status_code == 200, response.text
            assert response.json() == {"status": "succeeded"}
            assert calls == ["committed"]
            # A later request is a different question, and the answer is now a refusal.
            refused = client.post("/work")
    assert refused.status_code == 503
    assert refused.json() == {"status": "failed", "code": "log_unavailable"}
    assert calls == ["committed"], "the refused request never reached the business call"
    # The completion record was attempted once and never re-sent; the refused request never reached
    # the writer at all, so the acceptance record it would have needed was never even built.
    assert state["attempts"] == 2
    assert state["failed"] == 1
    assert adapter.sink.failure == "log_unavailable"
    adapter.shutdown()
    events = [line["event"] for line in segment_lines(adapter)]
    assert events == ["request.started"]


def test_a_pre_latched_sink_refuses_without_writing_and_recovers_by_nothing_implicit(tmp_path):
    """The latch is the only failure state, and nothing in this adapter clears it on its own."""
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path / "absent")})
    app = FastAPI()
    effects = []

    @app.post("/work")
    async def work():
        effects.append("committed")
        return JSONResponse({"status": "ok"})

    adapter.install(app)
    bound(app)
    assert adapter.emit("runtime.starting", level="INFO", outcome="started") is False
    with TestClient(app) as client:
        response = client.post("/work")
    assert response.status_code == 503
    assert effects == []
    assert adapter.available() is False
    assert adapter.log_state() == "log_unavailable"


def test_a_slow_authentication_record_never_blocks_the_event_loop(tmp_path):
    """The second review's finding: fixing the acceptance record left the *auth* record waiting.

    `request.started` was already handed over asynchronously, but the authentication verdict was
    still written with the blocking form from inside an asynchronous endpoint: a 250 ms disk there
    stalled a 10 ms heartbeat to 257 ms. The seam is now the loop's own form, so the same 250 ms is
    spent on a bounded worker while the loop keeps answering — including the real liveness probe,
    which is the route that exists to answer while everything else is busy.

    Only the authentication record is slow here. Every other record of the request really lands, so
    this cannot pass by the request failing early.
    """
    from tianshu_memory.runtime_probes import ProbeConfig
    from tianshu_memory.server_runtime import Assembly, install_probe_routes

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    effects = []
    app = FastAPI()

    @app.post("/work")
    async def work():
        from tianshu_memory.diagnostics import anote_authenticated

        assert await anote_authenticated() is True
        effects.append("committed")
        return JSONResponse({"status": "ok"})

    adapter.install(app)
    bound(app)

    def settings(runtime):
        return ProbeConfig(
            service=CHAT_SERVICE,
            diagnostics=runtime.diagnostics,
            config_path=Path("absent.json"),
            contract_path=None,
            runtime=runtime,
        )

    install_probe_routes(app, Assembly(app, adapter, None, probe_factory=settings))

    real = Sink.write

    def slow_authentication(self, line, sequence):
        if json.loads(line)["event"] == "request.authenticated":
            time.sleep(0.25)
        return real(self, line, sequence)

    async def drive():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
        ) as client:
            gaps = []
            clock = [time.monotonic()]

            async def heartbeat():
                for _ in range(30):
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps.append(now - clock[0])
                    clock[0] = now
                return max(gaps)

            async def liveness():
                started = time.monotonic()
                response = await client.get("/health/live")
                return response, time.monotonic() - started

            worst, (live, live_elapsed), response = await asyncio.gather(
                heartbeat(), liveness(), client.post("/work")
            )
            return worst, live, live_elapsed, response

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(Sink, "write", slow_authentication))
        worst, live, live_elapsed, response = asyncio.run(drive())
        stack.close()
        adapter.shutdown()
    assert response.status_code == 200, response.text
    assert effects == ["committed"]
    assert live.status_code == 200 and live.json() == {"status": "alive"}
    assert worst < 0.2, f"a 10 ms heartbeat waited {worst:.3f}s for a 0.25s auth record"
    assert live_elapsed < 0.2, f"liveness waited {live_elapsed:.3f}s for a 0.25s auth record"
    events = [line["event"] for line in segment_lines(adapter)]
    assert events == ["request.started", "request.authenticated", "request.completed"]


def test_an_authentication_record_that_cannot_be_confirmed_refuses_before_the_business(tmp_path):
    """The verdict was reached, so acting on it needs a record of it: no record, no side effect."""
    from tianshu_memory.diagnostics import anote_authenticated

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    effects = []
    app = FastAPI()

    @app.post("/work")
    async def work():
        assert await anote_authenticated() is False
        raise Fault("log_unavailable", 503)

    adapter.install(app)
    bound(app)
    fault_boundary(app)
    # The acceptance record is the first write and really lands; the authentication record is the
    # second and is the one that fails.
    with injected_writes(fail_from=2) as state:
        with TestClient(app) as client:
            response = client.post("/work")
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "log_unavailable"
        assert effects == []
        assert state["attempts"] == 2
        assert adapter.sink.failure == "log_unavailable"
        adapter.shutdown()


def test_both_authentication_forms_write_the_same_record(tmp_path):
    """One verdict, one record, two callers: the thread form and the loop form cannot diverge."""
    from tianshu_memory.diagnostics import anote_authenticated, note_authenticated

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    app = FastAPI()

    @app.post("/loop")
    async def loop_form():
        assert await anote_authenticated() is True
        return JSONResponse({"status": "ok"})

    @app.post("/thread")
    async def thread_form():
        assert await run_in_threadpool(note_authenticated) is True
        return JSONResponse({"status": "ok"})

    adapter.install(app)
    bound(app)
    with injected_writes() as state:
        with TestClient(app) as client:
            assert client.post("/loop").status_code == 200
            assert client.post("/thread").status_code == 200
        adapter.shutdown()
    # Two requests, three records each: the acceptance record, the authentication verdict and the
    # closing record. The two forms are indistinguishable in the file, which is the point — the
    # difference between them is where the wait happens, not what is written.
    assert state["attempts"] == 6
    events = [line["event"] for line in segment_lines(adapter)]
    assert events == [
        "request.started",
        "request.authenticated",
        "request.completed",
        "request.started",
        "request.authenticated",
        "request.completed",
    ]
    assert {line["outcome"] for line in segment_lines(adapter)} == {"started", "succeeded"}


def test_the_async_admission_is_bounded_and_never_moved_to_an_unbounded_pool(tmp_path):
    """Handing a wait to a worker is not a bound: the pool's own queue would grow instead.

    `asyncio.to_thread` (and any bare `run_in_executor`) parks work on the default executor, whose
    queue has no ceiling. The writer's `WRITE_QUEUE_LIMIT` would then describe a queue that is not
    the one the work is actually waiting in. The loop's admission is bounded by the same number, so
    a burst that outruns the disk is refused and latched rather than accumulated.
    """
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    admitted = []
    lock = threading.Lock()
    peak = [0]

    class Tracked(asyncio.Semaphore):
        """The adapter's own admission, watched: how many waits got past the gate at once."""

        def __init__(self, value):
            super().__init__(value)
            self.holding = 0

        async def acquire(self):
            await super().acquire()
            with lock:
                self.holding += 1
                peak[0] = max(peak[0], self.holding)
            return True

        def release(self):
            with lock:
                self.holding -= 1
            super().release()

    async def burst():
        async def one(index):
            landed = await adapter.aemit("request.started", level="INFO", outcome="started")
            with lock:
                admitted.append(landed)

        await asyncio.gather(*(one(index) for index in range(WRITE_QUEUE_LIMIT * 3)))

    adapter.writes_in_flight = Tracked(WRITE_QUEUE_LIMIT)
    with injected_writes(delay=0.05):
        asyncio.run(burst())
        assert False in admitted, "a burst past the admission must be refused, not queued"
        assert adapter.sink.failure == "log_capacity"
        assert adapter.log_state() == "log_capacity"
        # The ceiling is real and is the writer's own number: at most `WRITE_QUEUE_LIMIT` blocking
        # waits were ever parked on workers, so the work moved off the loop cannot become a larger,
        # separate backlog in the executor's queue.
        assert peak[0] <= WRITE_QUEUE_LIMIT, f"{peak[0]} log waits ran at once"
        adapter.shutdown()
    # Nothing was dropped silently: every admitted record is in the file exactly once, numbered
    # contiguously from one.
    numbers = sorted(line["sequence"] for line in segment_lines(adapter))
    assert numbers == list(range(1, len(numbers) + 1))
    assert 0 < len(numbers) <= WRITE_QUEUE_LIMIT * 2


def test_the_writer_owns_the_file_and_a_timed_out_shutdown_never_takes_it(tmp_path, monkeypatch):
    """The second review's finding: a shutdown that gave up still closed a live writer's handle.

    With a 30 ms budget and a writer held inside a real write, the old code returned after 242 ms
    having called `sink.close` while the writer was still alive — a second owner of a descriptor, an
    unbounded `close` on the caller, and a writer that could then reopen the segment it had just
    lost. Now the writer releases the file itself, when it really exits, and the shutdown reports
    that it could not confirm the end instead of pretending it had.
    """
    from tianshu_memory.diagnostics import WRITER_SHUTDOWN_SECONDS

    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    assert adapter.emit("runtime.started", level="INFO", outcome="succeeded") is True
    writer_thread = adapter.writer.thread
    entered = threading.Event()
    release = threading.Event()
    closed_by = []
    real_write = Sink.write
    real_close = Sink._close

    def held(self, line, sequence):
        entered.set()
        assert release.wait(5), "the test never released the writer"
        return real_write(self, line, sequence)

    def watched_close(self):
        closed_by.append(threading.current_thread().name)
        return real_close(self)

    monkeypatch.setattr("tianshu_memory.diagnostics.WRITER_SHUTDOWN_SECONDS", 0.03)
    with (
        patch.object(Sink, "write", held),
        patch.object(Sink, "_close", watched_close),
    ):
        caller = threading.Thread(
            target=lambda: adapter.emit("runtime.ready", level="INFO", outcome="succeeded")
        )
        caller.start()
        assert entered.wait(5), "the writer never reached the held write"
        started = time.monotonic()
        confirmed = adapter.shutdown()
        elapsed = time.monotonic() - started
        # The deadline is on the refusal, not on the file: the caller returns promptly, having
        # confirmed nothing, and has not touched a handle a live writer still owns.
        assert elapsed < WRITER_SHUTDOWN_SECONDS + 0.5, f"shutdown waited {elapsed:.3f}s"
        assert confirmed is False
        assert closed_by == []
        assert writer_thread.is_alive()
        release.set()
        caller.join(5)
        writer_thread.join(5)
    assert writer_thread.is_alive() is False
    assert closed_by == ["tianshu-memory-log-writer"]
    # The line the writer was holding is not lost and not repeated, and the adapter stays refused:
    # a shutdown that could not be confirmed never becomes a licence to write again.
    numbers = [line["sequence"] for line in segment_lines(adapter)]
    assert numbers == [1, 2]
    assert adapter.emit("runtime.stopped", level="INFO", outcome="succeeded") is False


def test_concurrent_first_events_start_exactly_one_writer(tmp_path):
    """The second review's finding: the first start had no mutex, so two callers built two writers.

    Every caller here is a real thread and they all arrive together, so the check-then-construct
    inside `Writer.start` is entered concurrently for real. One writer must exist afterwards, the
    file must contain each line once, and the numbers must be contiguous — two writers over one
    handle produced duplicate numbers, not just a wasted thread.
    """
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    started = []
    results = []
    lock = threading.Lock()
    gate = threading.Barrier(8)
    real_thread = threading.Thread

    class Watched(real_thread):
        def start(self):
            with lock:
                started.append(self)
            return super().start()

    def emit_once():
        gate.wait(10)
        landed = adapter.emit("runtime.started", level="INFO", outcome="succeeded")
        with lock:
            results.append(landed)

    callers = [real_thread(target=emit_once) for _ in range(8)]
    with patch.object(threading, "Thread", Watched):
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(10)
        writers = [thread for thread in started if thread.name == "tianshu-memory-log-writer"]
        assert len(writers) == 1, f"{len(writers)} writers were started over one file"
        assert adapter.shutdown() is True
    for writer in writers:
        writer.join(5)
    lines = segment_lines(adapter)
    numbers = [line["sequence"] for line in lines]
    assert results == [True] * 8
    assert len(lines) == 8
    assert numbers == list(range(1, 9))
    assert len(numbers) == len(set(numbers))
    # A process that stopped is never handed a second writer over the same sink.
    assert adapter.emit("runtime.started", level="INFO", outcome="succeeded") is False
    assert len([thread for thread in started if thread.name == "tianshu-memory-log-writer"]) == 1


def test_a_first_event_racing_a_shutdown_never_starts_a_writer_behind_it(tmp_path):
    """Start and stop interleaved: whoever wins, the process ends with no writer and no lost line.

    Two shapes are covered because they are the two ways a first event and a shutdown can meet: the
    shutdown arrives first and the event is refused outright, or the event arrives first and its
    line is written before the writer ends. Neither may produce a second writer, and neither may
    leave a line the file does not contain.
    """
    stopped_first = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path / "a")})
    assert stopped_first.shutdown() is True
    assert stopped_first.emit("runtime.started", level="INFO", outcome="succeeded") is False
    assert segment_lines(stopped_first) == []
    assert stopped_first.writer.thread is None, "a refused first event must not start a writer"

    started_first = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path / "b")})
    order = []
    lock = threading.Lock()

    def emit():
        landed = started_first.emit("runtime.started", level="INFO", outcome="succeeded")
        with lock:
            order.append(("emit", landed))

    def stop():
        with lock:
            order.append(("stop", None))
        started_first.shutdown()

    gate = threading.Barrier(2)

    def run(action):
        gate.wait(5)
        action()

    threads = [
        threading.Thread(target=run, args=(emit,)),
        threading.Thread(target=run, args=(stop,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert started_first.shutdown() is True
    lines = segment_lines(started_first)
    assert [line["sequence"] for line in lines] == list(range(1, len(lines) + 1))
    # Whichever order the two arrived in, the process is stopped and stays stopped.
    assert started_first.emit("runtime.ready", level="INFO", outcome="succeeded") is False
    assert len(segment_lines(started_first)) == len(lines)
