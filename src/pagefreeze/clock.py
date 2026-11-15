"""抓取时钟：真实时钟与可注入的虚拟时钟。

虚拟时钟只驱动限速/调度语义，不参与 HTTP 超时；sleep 用条件变量等待，
使固定时钟测试可以在零墙钟耗时下验证任意长的 Crawl-delay。
"""
from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    """限速器依赖的最小时钟面。"""

    def now(self) -> float:
        """单调秒。"""

    def sleep(self, seconds: float) -> None:
        """至少等待 seconds 秒（虚拟时钟下为逻辑秒）。"""


class SystemClock:
    """基于 time.monotonic 的进程真实时钟。"""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock:
    """线程安全的固定虚拟时钟：sleep 推进逻辑时间，不消耗墙钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._cond = threading.Condition()

    def now(self) -> float:
        with self._cond:
            return self._now

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._cond:
            self._now += float(seconds)
            self._cond.notify_all()

    def advance(self, seconds: float) -> None:
        """测试辅助：显式推进逻辑时间。"""
        with self._cond:
            self._now += float(seconds)
            self._cond.notify_all()
