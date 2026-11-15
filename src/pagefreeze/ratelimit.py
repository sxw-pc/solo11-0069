"""线程安全的请求间隔限速器与按 origin 独立的限速注册表。

每个 origin 持有一个独立 :class:`RateLimiter`，保证同 origin 相邻请求起始时间
至少间隔该 origin 的有效最小间隔，不同 origin 的等待槽互不占用。
"""
from __future__ import annotations

import threading
from .clock import Clock, SystemClock


class RateLimiter:
    """保证同一 origin 任意两个请求开始时间至少相差 ``interval_seconds``。"""

    def __init__(self, interval_ms: int, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._last = 0.0
        self._interval = 0.0
        self.set_interval_ms(interval_ms)

    @property
    def interval_seconds(self) -> float:
        return self._interval

    @property
    def interval_ms(self) -> int:
        return round(self._interval * 1000)

    @property
    def last_start(self) -> float:
        """上一次发放的请求起始时间（0.0 表示尚未发放）。"""
        with self._lock:
            return self._last

    def next_allowed_at(self) -> float:
        """下一条请求允许开始的最早时钟时间。"""
        with self._lock:
            return self._last + self._interval

    def set_interval_ms(self, interval_ms: int) -> None:
        """更新最小间隔；只接受不小于当前值的间隔，保留已发放的时间戳。"""
        seconds = max(0, interval_ms) / 1000.0
        with self._lock:
            if seconds >= self._interval:
                self._interval = seconds

    def wait(self) -> float:
        """阻塞到允许发起下一条请求，返回实际请求起始时间（时钟秒）。"""
        with self._lock:
            interval = self._interval
            if interval <= 0:
                return self._clock.now()
            now = self._clock.now()
            delay = self._last + interval - now
            if delay > 0:
                self._clock.sleep(delay)
                now = self._clock.now()
            self._last = now
            return now


class OriginRegistry:
    """按 origin 维护独立限速器；间隔可在首次 robots 解析后上调。"""

    def __init__(self, base_interval_ms: int, clock: Clock | None = None) -> None:
        self._base_ms = max(0, base_interval_ms)
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._limiters: dict[str, RateLimiter] = {}

    def get(self, origin: str) -> RateLimiter:
        """返回（或惰性创建）origin 对应的限速器。"""
        with self._lock:
            limiter = self._limiters.get(origin)
            if limiter is None:
                limiter = RateLimiter(self._base_ms, self._clock)
                self._limiters[origin] = limiter
            return limiter

    def apply_robots_delay(self, origin: str, delay_seconds: float) -> RateLimiter:
        """按 max(配置间隔, robots Crawl-delay) 收紧 origin 间隔，只增不减。"""
        effective_ms = max(self._base_ms, round(delay_seconds * 1000))
        limiter = self.get(origin)
        limiter.set_interval_ms(effective_ms)
        return limiter
