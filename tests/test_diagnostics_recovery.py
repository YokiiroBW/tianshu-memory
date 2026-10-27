import asyncio
import threading
import time

import pytest

from tianshu_memory import diagnostics
from tianshu_memory.diagnostics import CHAT_SERVICE, KNOWLEDGE_SERVICE, Diagnostics


def full_adapter(directory, service=CHAT_SERVICE):
    directory.mkdir()
    adapter = Diagnostics(service, {"log_directory": str(directory)})
    adapter.sink.segment_bytes = 1024
    adapter.sink.directory_bytes = 4096
    for _ in range(50):
        if not adapter.emit("runtime.started"):
            break
    else:
        raise AssertionError("real log did not reach its budget")
    assert adapter.log_state() == "log_capacity"
    return adapter


def remove_sealed_prefix(adapter):
    paths = sorted(adapter.sink.directory.glob("*.jsonl"))
    assert len(paths) >= 2
    kept = {path: path.read_bytes() for path in paths[1:]}
    paths[0].unlink()
    return kept


@pytest.mark.parametrize("service", [CHAT_SERVICE, KNOWLEDGE_SERVICE])
def test_real_capacity_proof_preserves_sequences_active_tail_and_remaining_segments(
    tmp_path, service
):
    adapter = full_adapter(tmp_path / "logs", service)
    try:
        sequence = adapter.sink.sequence
        assert not adapter.recover(), "a still-full directory cannot become ready"
        assert adapter.sink.sequence == sequence
        kept = remove_sealed_prefix(adapter)
        assert asyncio.run(adapter.arecover())
        assert adapter.log_state() == "ok"
        assert adapter.sink.sequence == sequence, "maintenance must not spend a log sequence"
        assert all(path.read_bytes() == content for path, content in kept.items())
        assert not list(adapter.sink.directory.glob(".tianshu-log-probe-*.tmp"))
        assert adapter.emit("runtime.ready")
        assert adapter.sink.sequence == sequence + 1
        assert adapter.writer.admitted == 0
    finally:
        assert adapter.shutdown()


def test_writer_idle_recovers_after_external_sealed_cleanup_without_a_health_write(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(diagnostics, "WRITER_QUEUE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(diagnostics, "RECOVERY_INTERVAL_SECONDS", 0.02)
    adapter = full_adapter(tmp_path / "logs")
    try:
        sequence = adapter.sink.sequence
        kept = remove_sealed_prefix(adapter)
        deadline = time.monotonic() + 2
        while adapter.log_state() != "ok" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert adapter.log_state() == "ok"
        assert adapter.sink.sequence == sequence
        assert all(path.read_bytes() == content for path, content in kept.items())
        assert adapter.emit("runtime.ready")
    finally:
        assert adapter.shutdown()


def test_failed_fsync_never_clears_capacity_or_leaves_a_probe_file(tmp_path, monkeypatch):
    adapter = full_adapter(tmp_path / "logs")
    try:
        remove_sealed_prefix(adapter)
        sequence = adapter.sink.sequence

        def denied(descriptor):
            raise PermissionError("synthetic filesystem refusal")

        with monkeypatch.context() as patch:
            patch.setattr(diagnostics.os, "fsync", denied)
            assert not adapter.recover()
        assert adapter.log_state() == "log_capacity"
        assert adapter.sink.sequence == sequence
        assert not list(adapter.sink.directory.glob(".tianshu-log-probe-*.tmp"))
        assert adapter.recover()
    finally:
        assert adapter.shutdown()


def test_a_new_failure_during_the_real_proof_is_not_cleared(tmp_path, monkeypatch):
    adapter = full_adapter(tmp_path / "logs")
    try:
        remove_sealed_prefix(adapter)
        fsync = diagnostics.os.fsync

        def newer_failure(descriptor):
            fsync(descriptor)
            adapter.sink.fail("log_capacity")

        with monkeypatch.context() as patch:
            patch.setattr(diagnostics.os, "fsync", newer_failure)
            assert not adapter.recover()
        assert adapter.log_state() == "log_capacity"
        assert adapter.recover()
    finally:
        assert adapter.shutdown()


def test_new_unknown_io_failure_cannot_be_downgraded_by_later_capacity_proofs(
    tmp_path, monkeypatch
):
    adapter = full_adapter(tmp_path / "logs")
    try:
        remove_sealed_prefix(adapter)
        fsync = diagnostics.os.fsync

        def unknown(descriptor):
            fsync(descriptor)
            adapter.sink.fail("log_unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(diagnostics.os, "fsync", unknown)
            assert not adapter.recover()
        assert adapter.log_state() == "log_capacity", "the original closed verdict is preserved"
        adapter.sink.fail("log_capacity")
        assert not adapter.recover()
        assert adapter.log_state() == "log_capacity"
    finally:
        assert adapter.shutdown()


def test_capacity_probe_does_not_clear_an_unknown_io_outcome(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    adapter = Diagnostics(CHAT_SERVICE, {"log_directory": str(directory)})
    try:
        assert adapter.emit("runtime.started")
        adapter.sink.fail("log_unavailable")
        sequence = adapter.sink.sequence
        assert not adapter.recover()
        assert adapter.log_state() == "log_unavailable"
        assert adapter.sink.sequence == sequence
    finally:
        assert adapter.shutdown()


def test_inflight_proof_keeps_its_real_admission_and_failure_until_fsync_ack(tmp_path, monkeypatch):
    adapter = full_adapter(tmp_path / "logs")
    entered, release = threading.Event(), threading.Event()
    result = []
    fsync = diagnostics.os.fsync

    def held(descriptor):
        entered.set()
        assert release.wait(2)
        fsync(descriptor)

    try:
        remove_sealed_prefix(adapter)
        sequence = adapter.sink.sequence
        with monkeypatch.context() as patch:
            patch.setattr(diagnostics.os, "fsync", held)
            caller = threading.Thread(target=lambda: result.append(adapter.recover()))
            caller.start()
            try:
                assert entered.wait(2)
                assert adapter.log_state() == "log_capacity"
                assert adapter.writer.admitted == 1
                assert adapter.sink.sequence == sequence
            finally:
                release.set()
                caller.join(2)
            assert not caller.is_alive()
        assert result == [True]
        assert adapter.log_state() == "ok"
        assert adapter.writer.admitted == 0
        assert adapter.sink.sequence == sequence
    finally:
        release.set()
        assert adapter.shutdown()
