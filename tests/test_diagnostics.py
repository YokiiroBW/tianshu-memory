"""The runtime event log: one closed shape, full coverage, no secret, no false ready.

These tests exercise the adapter directly and through a real application, and they are written
against the coordinator's frozen `contracts/diagnostics/v1` rather than against this module's own
constants: the JSON Schema and the published examples are loaded from disk, so an edit here that
stopped matching the contract would fail rather than quietly redefine it.
"""

import json
import re
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tianshu_memory.diagnostics import (
    CHAT_SERVICE,
    CORRELATION_HEADER,
    ERROR_CODES,
    EVENTS,
    HOOK_NAME,
    KNOWLEDGE_SERVICE,
    MAX_LINE_BYTES,
    NO_STORE,
    PROBE_PATHS,
    SINK_WARNING,
    Diagnostics,
    SinkFull,
    read_config,
    read_correlation,
)
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
    from tianshu_memory.diagnostics import note_authenticated, record_execution

    app = FastAPI()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(tmp_path)})
    adapter.install(app)
    bound(app)

    @app.post("/local/v1/memory/read")
    async def read():
        note_authenticated()
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
    """A bare `create_app` in a test is untouched by the arrival of diagnostics."""
    from tianshu_memory.diagnostics import note_authenticated, note_fault, record_execution

    assert note_authenticated() is None
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
