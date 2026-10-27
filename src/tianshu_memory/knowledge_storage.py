"""Leased Knowledge storage resources; no authorization or query results are cached."""

import threading
import weakref
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from tianshu_storage import StorageOptions

from .domain import fingerprint, require
from .store import Store

STORAGE_FIELDS = (
    "database_path",
    "source_sync",
    "knowledge_files_directory",
    "storage_backends",
    "role_grants_database_path",
)


def registration(config, *, storage_environ=None):
    """Include effective environment overrides without retaining/logging credentials."""
    return fingerprint(
        {
            "config": {key: config.get(key) for key in STORAGE_FIELDS},
            "options": asdict(StorageOptions.from_settings(config, environ=storage_environ)),
        }
    )


@dataclass
class _Entry:
    key: str
    store: Store
    users: int = 0
    retired: bool = False


@dataclass
class _State:
    lock: object = field(default_factory=threading.RLock)
    current: _Entry | None = None
    closed: bool = False


def _close(state):
    with state.lock:
        state.closed = True
        entry, state.current = state.current, None
        if entry is not None:
            entry.retired = True
            if not entry.users:
                entry.store.close()


class KnowledgeStorage:
    """One current registration, with old owners retained only until their work ends.

    Store owns a thread-safe backend pool, while every transaction still owns its
    connection, authority lock and independent source-guard check. Configuration is
    supplied after each dispatch has performed its current authorization check.
    """

    def __init__(self, *, storage_environ=None):
        self._state = _State()
        self._finalizer = weakref.finalize(self, _close, self._state)
        self.storage_environ = None if storage_environ is None else dict(storage_environ)

    def registration(self, config):
        return registration(config, storage_environ=self.storage_environ)

    @contextmanager
    def lease(self, config, key):
        state = self._state
        with state.lock:
            require(self.registration(config) == key, "registration_changed", 409)
            entry = state.current
            if entry is None or entry.key != key:
                if entry is not None:
                    entry.retired = True
                    state.current = None
                    if not entry.users:
                        entry.store.close()
                options = (
                    {}
                    if self.storage_environ is None
                    else {"storage_environ": self.storage_environ}
                )
                store = Store.from_config(config, service="knowledge", **options)
                try:
                    require(self.registration(config) == key, "registration_changed", 409)
                except BaseException:
                    store.close()
                    raise
                # An admitted HTTP worker may reach its first authorized context
                # after lifespan shutdown. It still runs, but gets an uncached
                # owner which closes when that call actually finishes.
                entry = _Entry(key, store, retired=state.closed)
                if not state.closed:
                    state.current = entry
            entry.users += 1
        try:
            yield entry.store
        finally:
            with state.lock:
                entry.users -= 1
                if entry.retired and not entry.users:
                    entry.store.close()

    def close(self):
        # Transport owns admission. Retire cached resources without cancelling
        # admitted work, including a worker which has not acquired its Store yet.
        self._finalizer()
