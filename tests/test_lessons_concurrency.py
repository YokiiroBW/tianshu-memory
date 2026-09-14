"""Lesson reads stay outside the shared writer lock, and their gap re-checks state.

Reuses the two-project fixture from test_lessons. Every slow path is paused with a gate while
an unrelated MemoryService register/resolve must still complete.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_knowledge_concurrency import chat_operation
from test_lessons import imported, lesson, record, reference
from test_lessons import lessons as lessons

from tianshu_memory import knowledge as module
from tianshu_memory import lessons as lesson_module
from tianshu_memory.domain import Fault, canonical


def operation(harness, name, note):
    """One lesson-domain read or write, issued through the application."""
    if name == "lesson_query":
        return harness.run(
            "lesson_query",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
        )
    if name == "lesson_recover":
        return harness.run(
            "lesson_recover",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
        )
    if name == "lesson_check":
        package = harness.run(
            "lesson_recover",
            dict(text="receipt", budget_bytes=8192),
            project="alpha",
            client="operator",
        )
        return harness.run(
            "lesson_check", dict(package=package), project="alpha", client="operator"
        )
    if name == "lesson_revise":
        return harness.run(
            "lesson_revise",
            dict(
                key="alpha-lesson",
                dedupe="gate@1",
                lesson_id=note["lesson_id"],
                expected_version=1,
                lesson=lesson(harness, "alpha", correction=note["correction"]),
            ),
            project="alpha",
            client="alpha-writer",
        )
    return harness.run(
        "lesson_record",
        dict(key="gate-lesson", expected_version=0, lesson=lesson(harness, "alpha")),
        project="alpha",
        client="alpha-writer",
    )


def gate(monkeypatch, real):
    """Pause every lesson evidence file read, in whichever module performs the read."""
    entered, release = Event(), Event()

    def paused(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "test gate was not released"
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "read_file", paused)
    monkeypatch.setattr(lesson_module, "read_file", paused)
    return entered, release


@pytest.mark.parametrize(
    "name", ["lesson_query", "lesson_recover", "lesson_check", "lesson_record", "lesson_revise"]
)
def test_slow_lesson_evidence_allows_memory_register_and_resolve(
    lessons, contracts, monkeypatch, name
):
    imported(lessons, "alpha")
    first = record(lessons, "alpha")
    reference(lessons, "alpha")
    # Drop the per-dispatch file cache so the gated read really happens (a fresh operation
    # starts with an empty cache; the warm-up above filled this one).
    lessons.app.file_cache = {}
    note = {"lesson_id": first["lesson_id"], "correction": "Read the receipt table first"}
    entered, release = gate(monkeypatch, module.read_file)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(operation, lessons, name, note)
        try:
            assert entered.wait(2)
            # The shared Memory writer must not wait for lesson file I/O.
            assert (
                pool.submit(chat_operation, lessons.store, contracts).result(timeout=1)["state"]
                == "found"
            )
        finally:
            release.set()
        result = waiting.result(timeout=5)
    if name == "lesson_check":
        assert result["valid"]
    elif name in {"lesson_record", "lesson_revise"}:
        assert result["status"] in {"recorded", "revised"}
    else:
        assert result["lessons"]


@pytest.mark.parametrize("name", ["lesson_query", "lesson_recover", "lesson_check"])
def test_lesson_read_gap_revalidates_project_and_permissions(lessons, monkeypatch, name):
    imported(lessons, "alpha")
    record(lessons, "alpha")
    note = {"lesson_id": "unused", "correction": "unused"}
    entered, release = Event(), Event()
    real = module.read_file

    def paused(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            # Revoke the caller s project access while the evidence read is in flight.
            lessons.config["knowledge"]["clients"]["operator"]["projects"] = ["beta"]
            lessons.path.write_text(canonical(lessons.config))
            assert release.wait(10), "test gate was not released"
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "read_file", paused)
    monkeypatch.setattr(lesson_module, "read_file", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiting = pool.submit(operation, lessons, name, note)
        try:
            assert entered.wait(2)
        finally:
            release.set()
        with pytest.raises(Fault, match="project_conflict|forbidden"):
            waiting.result(timeout=5)


def test_lesson_file_change_between_preview_and_final_read_is_rejected(lessons, monkeypatch):
    imported(lessons, "alpha")
    first = record(lessons, "alpha")
    # Build the revision against the current file before the file changes under us.
    revision = lesson(lessons, "alpha", correction="Read the receipt table first")
    real = module.read_file
    count = 0

    def changed(project, locator):
        nonlocal count
        raw = real(project, locator)
        count += 1
        if count == 1:
            (lessons.roots["alpha"] / "notes.md").write_text(
                "Changed receipt behaviour.", encoding="utf-8"
            )
        return raw

    monkeypatch.setattr(module, "read_file", changed)
    monkeypatch.setattr(lesson_module, "read_file", changed)
    with pytest.raises(Fault, match="stale_evidence"):
        lessons.run(
            "lesson_revise",
            dict(
                key="alpha-lesson",
                dedupe="changed@1",
                lesson_id=first["lesson_id"],
                expected_version=1,
                lesson=revision,
            ),
            project="alpha",
            client="alpha-writer",
        )
    # The recorded lesson stays readable, but its evidence is no longer current.
    assert not lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=8192), project="alpha", client="operator"
    )["lessons"]
    # The recorded lesson stays readable, but its evidence is no longer current.
    assert not lessons.run(
        "lesson_query", dict(text="receipt", budget_bytes=8192), project="alpha", client="operator"
    )["lessons"]
