"""Cooperative foreground priority without cancelling an in-flight model request."""
import threading
from contextlib import contextmanager


class PriorityGate:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.owner = None
        self.depth = 0
        self.reservations = 0
        self.waiters = 0
        self.local = threading.local()

    @property
    def interactive_pending(self):
        with self.condition:
            return self.reservations + self.waiters

    def reserve(self):
        with self.condition:
            self.reservations += 1
            self.condition.notify_all()

    def release_reservation(self):
        with self.condition:
            self.reservations = max(0, self.reservations - 1)
            self.condition.notify_all()

    @contextmanager
    def as_interactive(self):
        previous = getattr(self.local, 'priority', False)
        self.local.priority = True
        try:
            yield
        finally:
            self.local.priority = previous

    @contextmanager
    def interactive(self):
        with self.as_interactive():
            with self:
                yield self

    def checkpoint(self):
        if getattr(self.local, 'priority', False):
            return
        with self.condition:
            while self.reservations or self.waiters:
                self.condition.wait()

    def acquire(self, blocking=True, timeout=-1):
        import time
        thread = threading.get_ident()
        priority = getattr(self.local, 'priority', False)
        deadline = time.monotonic()+timeout if timeout >= 0 else None
        with self.condition:
            if self.owner == thread:
                self.depth += 1
                return True
            if priority:
                self.waiters += 1
            try:
                while self.owner is not None or (not priority and (self.reservations or self.waiters)):
                    if not blocking:
                        return False
                    remaining = None if deadline is None else deadline-time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self.condition.wait(remaining)
                self.owner, self.depth = thread, 1
                return True
            finally:
                if priority:
                    self.waiters -= 1

    def release(self):
        with self.condition:
            if self.owner != threading.get_ident():
                raise RuntimeError('Cannot release another thread\'s inference gate')
            self.depth -= 1
            if not self.depth:
                self.owner = None
                self.condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
