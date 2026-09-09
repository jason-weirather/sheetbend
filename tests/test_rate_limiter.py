from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from sheetbend.rate_limiter import RateLimiter


def test_concurrency_waits_and_release_is_idempotent():
    limiter = RateLimiter(max_concurrent=1)
    first = limiter.acquire()
    waiting = Event()
    entered = Event()
    def worker():
        waiting.set()
        with limiter.acquire():
            entered.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert waiting.wait(2)
            assert not entered.wait(0.05)
        finally:
            first.release()
            first.release()
        future.result(timeout=2)
    assert entered.is_set()
    assert limiter._active == 0


def test_exception_releases_slot():
    limiter = RateLimiter(max_concurrent=1)
    with pytest.raises(RuntimeError), limiter.acquire():
        raise RuntimeError("test")
    with limiter.acquire():
        assert limiter._active == 1


def test_rolling_window_with_fake_clock(monkeypatch):
    import sheetbend.rate_limiter as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    limiter = RateLimiter(requests_per_minute=2)
    waits = []
    def wait(timeout=None):
        assert timeout is not None
        waits.append(timeout)
        clock[0] += timeout
    monkeypatch.setattr(limiter._condition, "wait", wait)
    with limiter.acquire():
        pass
    clock[0] = 10.0
    with limiter.acquire():
        pass
    clock[0] = 20.0
    with limiter.acquire():
        assert clock[0] == 60.0
    assert waits == [40.0]
    assert list(limiter._starts) == [10.0, 60.0]


def test_unlimited_requests_do_not_accumulate_history():
    limiter = RateLimiter()
    for _ in range(100):
        with limiter.acquire():
            pass
    assert not limiter._starts
    assert limiter._active == 0
