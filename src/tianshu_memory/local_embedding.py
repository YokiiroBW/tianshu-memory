"""One warm offline worker; no implicit download or mandatory torch dependency."""

import hashlib
import json
import math
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .domain import Fault, require, strict_json


class LocalEmbeddingBackend:
    def __init__(self, configuration):
        self.config = dict(configuration)
        self.lock = threading.Lock()
        self.process = None
        self.output = None
        self.exchange = None
        self.closed = False
        for name in ("python_executable", "model_directory"):
            path = Path(self.config[name])
            require(path.is_absolute(), "invalid_input", 400)
        require(
            self.config.get("model_revision") and self.config.get("dimension") == 512,
            "dependency_unavailable",
            503,
        )
        require(not self.config.get("rerank", False), "invalid_input", 400)
        timeout = self.config.get("timeout_seconds", 45)
        require(
            type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 120,
            "invalid_input",
            400,
        )
        assets = []
        directory = Path(self.config["model_directory"])
        for path in sorted(directory.glob("*")):
            if path.is_file() and path.suffix in {".json", ".txt", ".safetensors", ".model"}:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                assets.append((path.name, digest.hexdigest()))
        signature = json.dumps(
            [self.config["model_revision"], assets, "cls-mean-chunks-l2-query-prefix-v1"],
            sort_keys=True,
        ).encode()
        self.embedding_revision = "local-bge:" + hashlib.sha256(signature).hexdigest()

    def call(self, operation, payload):
        if operation == "settings":
            return {
                "mode": self.config.get("mode", "hybrid"),
                "rerank": False,
                "candidates": self.config.get("candidates", 32),
                "revision": self.config.get("revision", self.embedding_revision),
                "embedding_revision": self.embedding_revision,
            }
        require(operation == "embed", "dependency_unavailable", 503)
        return self._embed(payload, "document")

    def embed_query(self, text, revision):
        return self._embed({"texts": [text], "revision": revision}, "query")

    def embed_query_before(self, text, revision, deadline):
        return self._embed({"texts": [text], "revision": revision}, "query", deadline=deadline)

    def _start(self):
        require(not self.closed, "dependency_unavailable", 503)
        if self.process and self.process.poll() is None:
            return
        process = subprocess.Popen(
            [
                self.config["python_executable"],
                "-X",
                "utf8",
                "-u",
                str(Path(__file__).with_name("local_embedding_worker.py")),
                self.config["model_directory"],
                self.config["model_revision"],
                str(self.config.get("threads", 2)),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.process = process

    def _embed(self, payload, kind, *, deadline=None):
        timeout = self.config.get("timeout_seconds", 45)
        if kind == "query":
            timeout = min(timeout, 8)
        deadline = (
            min(deadline, time.monotonic() + timeout)
            if deadline is not None
            else time.monotonic() + timeout
        )
        if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise Fault("dependency_unavailable", 503)
        try:
            try:
                self._start()
                request = {
                    "revision": self.config["model_revision"],
                    "texts": payload["texts"],
                    "kind": kind,
                }
                raw = json.dumps(request, ensure_ascii=False, allow_nan=False).encode() + b"\n"
                require(len(raw) <= 1024 * 1024, "budget_exceeded", 429)
                process, output = self.process, queue.Queue(maxsize=1)

                def exchange():
                    answer = None
                    try:
                        remaining = memoryview(raw)
                        while remaining:
                            written = process.stdin.write(remaining)
                            if not written:
                                raise OSError("closed worker input")
                            remaining = remaining[written:]
                        answer = process.stdout.readline(8 * 1024 * 1024 + 1)
                        if not answer or len(answer) > 8 * 1024 * 1024:
                            answer = None
                    except (OSError, ValueError):
                        pass
                    output.put_nowait(answer)

                self.exchange = threading.Thread(
                    target=exchange, name="memory-embedding-exchange", daemon=True
                )
                self.exchange.start()
                answer = output.get(timeout=max(0, deadline - time.monotonic()))
                self.exchange.join()
                self.exchange = None
                require(answer is not None, "dependency_unavailable", 503)
                answer = strict_json(answer)
                require(set(answer) == {"vectors"}, "dependency_unavailable", 503)
                return answer
            except (Fault, OSError, ValueError, queue.Empty, TypeError, KeyError):
                self._stop()
                raise Fault("dependency_unavailable", 503) from None
        finally:
            self.lock.release()

    def _stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.exchange is not None:
                self.exchange.join(timeout=2)
                self.exchange = None
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None

    def close(self):
        self.closed = True
        if self.lock.acquire(timeout=self.config.get("timeout_seconds", 45)):
            try:
                self._stop()
            finally:
                self.lock.release()
