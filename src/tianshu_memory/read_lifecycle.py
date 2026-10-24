"""Request-local cancellation before a pure read starts its source boundary."""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .domain import Fault


@dataclass
class _Read:
    abandoned: Callable[[], bool]
    started: bool = False


_current: ContextVar[_Read | None] = ContextVar("memory_read_lifecycle", default=None)


@contextmanager
def cancellable_read(abandoned):
    """Bind only the explicitly selected HTTP pure-read worker, never its siblings."""
    token = _current.set(_Read(abandoned))
    try:
        yield
    finally:
        _current.reset(token)


def begin_source_read():
    """Called with the outer source lock held, before any barrier or sync transaction.

    Once started, every later boundary keeps the original completion semantics.
    This does not interrupt a sync, business transaction, or multi-phase read.
    """
    read = _current.get()
    if read is not None and not read.started:
        if read.abandoned():
            raise Fault("timeout", 408)
        read.started = True
