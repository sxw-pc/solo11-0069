"""线程安全的请求间隔限速器。"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """保证任意两个请求开始时间至少相差 ``interval_seconds``。"""

    def __init__(self, interval_ms: int) -> None:
        self._interval = max(0, interval_ms) / 1000.0
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        """阻塞到允许发起下一条请求，并把新的开始时间记入共享状态。"""
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._last + self._interval - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._last = now
