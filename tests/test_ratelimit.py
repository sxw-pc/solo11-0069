"""限速器与按 origin 限速注册表的单元测试（固定虚拟时钟，零墙钟等待）。"""
from __future__ import annotations

import pytest
from pagefreeze.clock import VirtualClock
from pagefreeze.ratelimit import OriginRegistry, RateLimiter


def test_rate_limiter_spacing_under_virtual_clock() -> None:
    clock = VirtualClock()
    limiter = RateLimiter(500, clock)
    starts = [limiter.wait() for _ in range(4)]
    assert starts == [0.5, 1.0, 1.5, 2.0]
    assert all(right - left >= 0.5 for left, right in zip(starts, starts[1:]))


def test_zero_interval_never_advances_clock() -> None:
    clock = VirtualClock()
    limiter = RateLimiter(0, clock)
    assert [limiter.wait() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert clock.now() == 0.0


def test_registry_keeps_independent_slots_per_origin() -> None:
    clock = VirtualClock()
    registry = OriginRegistry(100, clock)
    slow = registry.apply_robots_delay("http://127.0.0.1:18001", 1.0)
    fast = registry.get("http://127.0.0.1:18002")
    fast_starts = [fast.wait() for _ in range(3)]
    slow_starts = [slow.wait() for _ in range(3)]
    assert fast_starts == pytest.approx([0.1, 0.2, 0.3])
    assert slow_starts == pytest.approx([1.0, 2.0, 3.0])
    assert registry.get("http://127.0.0.1:18001").interval_ms == 1000
    assert registry.get("http://127.0.0.1:18002").interval_ms == 100
    interwoven_clock = VirtualClock()
    interwoven = OriginRegistry(100, interwoven_clock)
    a = interwoven.apply_robots_delay("http://127.0.0.1:18010", 1.0)
    b = interwoven.get("http://127.0.0.1:18011")
    sequence = [a.wait(), b.wait(), a.wait(), b.wait(), a.wait()]
    assert sequence == pytest.approx([1.0, 1.0, 2.0, 2.0, 3.0])
    a_gaps = [sequence[i] - sequence[i - 2] for i in (2, 4)]
    assert a_gaps == pytest.approx([1.0, 1.0])


def test_robots_delay_uses_max_of_config_and_delay() -> None:
    clock = VirtualClock()
    registry = OriginRegistry(2000, clock)
    limiter = registry.apply_robots_delay("http://127.0.0.1:18003", 0.5)
    assert limiter.interval_ms == 2000
    limiter = registry.apply_robots_delay("http://127.0.0.1:18004", 3.0)
    assert limiter.interval_ms == 3000


def test_interval_tightening_keeps_last_start() -> None:
    clock = VirtualClock()
    registry = OriginRegistry(100, clock)
    limiter = registry.get("http://127.0.0.1:18005")
    first = limiter.wait()
    registry.apply_robots_delay("http://127.0.0.1:18005", 2.0)
    second = limiter.wait()
    assert first == 0.1
    assert second == 2.1
