"""Small, thread-safe in-process limits on request starts and active responses."""

from collections import deque
from threading import Condition
import time


class _Permit:
    """A releasable concurrency slot; release is idempotent under the limiter lock."""

    def __init__(self, limiter: "RateLimiter") -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        with self._limiter._condition:
            if not self._released:
                self._released = True
                self._limiter._active -= 1
                self._limiter._condition.notify_all()

    def __enter__(self) -> "_Permit":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class RateLimiter:
    """Internal limiter shared by every operation on one Source.

    Limits are validated by the configuration barrier. Waiting has no queue
    deadline. Attempts count even when the provider subsequently rejects them.
    No token accounting, retries, global coordination, or fairness guarantee.
    """

    def __init__(
        self, *, requests_per_minute: int | None = None, max_concurrent: int | None = None
    ) -> None:
        self._rpm = requests_per_minute
        self._max_concurrent = max_concurrent
        self._starts: deque[float] = deque()
        self._active = 0
        self._condition = Condition()

    def acquire(self) -> _Permit:
        """Wait for both limits atomically, then reserve one request attempt."""
        with self._condition:
            while True:
                now = time.monotonic()
                while self._starts and self._starts[0] <= now - 60:
                    self._starts.popleft()
                if self._max_concurrent is not None and self._active >= self._max_concurrent:
                    self._condition.wait()
                    continue
                if self._rpm is not None and len(self._starts) >= self._rpm:
                    self._condition.wait(timeout=max(0, self._starts[0] + 60 - now))
                    continue
                self._active += 1
                if self._rpm is not None:
                    self._starts.append(now)
                return _Permit(self)
