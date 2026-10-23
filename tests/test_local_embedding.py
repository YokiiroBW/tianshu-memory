"""Subprocess deadlines use synthetic Python children; no downloaded model is loaded."""

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tianshu_memory.domain import Fault
from tianshu_memory.local_embedding import LocalEmbeddingBackend


def backend(tmp_path, monkeypatch, script, timeout=0.3):
    worker = tmp_path / "synthetic_worker.py"
    worker.write_text(script, encoding="utf-8")
    original = subprocess.Popen
    children = []

    def start(args, **kwargs):
        assert args[1:4] == ["-X", "utf8", "-u"]
        process = original([sys.executable, "-X", "utf8", "-u", str(worker)], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr("tianshu_memory.local_embedding.subprocess.Popen", start)
    engine = LocalEmbeddingBackend(
        dict(
            python_executable=sys.executable,
            model_directory=str(tmp_path),
            model_revision="synthetic",
            dimension=512,
            timeout_seconds=timeout,
        )
    )
    return engine, children


def test_not_reading_stdin_cannot_block_request_or_leave_process(tmp_path, monkeypatch):
    engine, children = backend(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(Fault):
        engine.call("embed", {"texts": ["a" * 500000]})
    assert time.monotonic() - started < 3
    assert all(child.poll() is not None for child in children)
    assert not any(thread.name == "memory-embedding-exchange" for thread in threading.enumerate())
    engine.close()


def test_deadline_includes_wait_for_busy_worker_and_restart(tmp_path, monkeypatch):
    engine, children = backend(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")

    def invoke():
        started = time.monotonic()
        with pytest.raises(Fault):
            engine.call("embed", {"texts": ["synthetic"]})
        return time.monotonic() - started

    with ThreadPoolExecutor(max_workers=2) as executor:
        times = list(executor.map(lambda _: invoke(), range(2)))
    assert max(times) < 3
    engine.close()
    assert all(child.poll() is not None for child in children)
    assert not any(thread.name == "memory-embedding-exchange" for thread in threading.enumerate())


def test_warm_worker_reused_and_closed(tmp_path, monkeypatch):
    engine, children = backend(
        tmp_path,
        monkeypatch,
        "import sys,json\nfor line in sys.stdin:\n data=json.loads(line)\n assert data['texts'][0] == '\u4e2d\u6587\U0001f431'\n print(json.dumps({'vectors': [[1,0] for text in data['texts']]}),flush=True)\n",
        timeout=3,
    )
    for _ in range(2):
        assert engine.call("embed", {"texts": ["中文🐱"]}) == {"vectors": [[1, 0]]}
    assert len(children) == 1
    engine.close()
    assert children[0].poll() is not None
