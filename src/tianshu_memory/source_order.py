"""FIFO ownership of SourceAuthority's existing outer RLock boundaries."""

import threading
from collections import deque
from contextlib import contextmanager

from .read_lifecycle import source_wait_timeout


class SourceOperationOrder:
    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._waiters = deque()
        self._owner = None
        self._depth = 0

    @contextmanager
    def turn(self):
        """Keep one place through cancellation polls; same-thread reentry is immediate.

        This mutex never encloses physical RLock acquisition or source/business
        work. The caller keeps this ownership until its physical lock is released.
        """
        thread = threading.get_ident()
        ticket = object()
        queued = owned = False
        try:
            with self._condition:
                if self._owner == thread:
                    self._depth += 1
                    owned = True
                else:
                    self._waiters.append(ticket)
                    queued = True
                    while True:
                        timeout = source_wait_timeout()
                        if self._owner is None and self._waiters[0] is ticket:
                            self._waiters.popleft()
                            queued = False
                            self._owner, self._depth = thread, 1
                            owned = True
                            break
                        self._condition.wait(timeout=timeout)
            yield
        finally:
            with self._condition:
                if queued:
                    self._waiters.remove(ticket)
                    self._condition.notify_all()
                if owned:
                    self._depth -= 1
                    if not self._depth:
                        self._owner = None
                        self._condition.notify_all()
