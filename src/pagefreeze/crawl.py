"""有界 BFS 抓取：回环白名单、按 origin 的 robots/Crawl-delay、并发上限与跳过留痕。

调度采用方案 B：每个 origin 维护独立 FIFO 与独立限速器，调度线程按各
origin 的 next-allowed-at 选择已到期任务派发；同一 origin 的请求不会越过
有效最小间隔（max(配置间隔, robots Crawl-delay)），不同 origin 互不占用
等待槽，长 delay 站点等待期间线程可继续服务其它 origin。
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urljoin
from .clock import Clock, SystemClock
from .config import CrawlConfig
from .crawlresult import CrawlResult, FetchLog, OriginStats, RejectedTarget, log_for
from .errors import BodyTooLarge, FetchError, FetchTimeout, PagefreezeError, PolicyError
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem
from .links import extract_links
from .policy import (
    RobotsRules, is_loopback_target, normalize_url, origin_of, parse_robots,
    path_and_query, robots_url_for, urlkey,
)
from .ratelimit import OriginRegistry


@dataclass
class _OriginState:
    """调度器线程维护的 origin 抓取状态（frontier 仅调度线程读写）。"""
    origin: str
    frontier: Frontier
    stats: OriginStats
    rules: RobotsRules | None = None
    ready: bool = False


class CrawlEngine:
    """按 origin 独立维护 robots 策略、限速器与 FIFO 的有界调度器。"""

    def __init__(self, config: CrawlConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self._start = self.clock.now()
        self.registry = OriginRegistry(config.request_interval_ms, self.clock)
        self.hits: list[FetchResult] = []
        self.logs: list[FetchLog] = []
        self.rejected: list[RejectedTarget] = []
        self.origin_stats: dict[str, OriginStats] = {}
        self._states: dict[str, _OriginState] = {}
        self._handled: set[str] = set()
        self._attempts = 0

    def _state_for(self, origin: str) -> _OriginState:
        state = self._states.get(origin)
        if state is None:
            stats = OriginStats(origin=origin,
                effective_interval_ms=self.config.request_interval_ms)
            state = _OriginState(origin=origin, frontier=Frontier(), stats=stats)
            self._states[origin] = state
            self.origin_stats[origin] = stats
        return state

    def _ensure_origin(self, origin: str) -> None:
        """首次访问 origin 时先取 robots.txt、应用策略，之后才调度其页面。"""
        state = self._state_for(origin)
        if state.ready:
            return
        state.ready = True
        if not self.config.respect_robots:
            return
        robots_target = robots_url_for(origin)
        self._handled.add(robots_target)
        item = FrontierItem(robots_target, urlkey(robots_target), 0)
        limiter = self.registry.get(origin)
        try:
            result = fetch_once(robots_target, self.config, limiter)
        except FetchError as exc:
            self.logs.append(log_for(item, None, "", 0, "error", exc.code.lower()))
            return
        state.stats.record_start(self.clock.now() - self._start)
        if result.mime not in self.config.allowed_mime:
            self.logs.append(log_for(item, result.status, result.mime, result.size,
                "skipped", "mime_not_allowed"))
            return
        self.hits.append(result)
        self.logs.append(log_for(item, result.status, result.mime, result.size, "archived"))
        if result.status != 200:
            return
        text = result.body.decode("utf-8", errors="replace")
        rules = parse_robots(text, self.config.user_agent)
        state.rules = rules
        state.stats.robots_warnings = rules.warnings
        if rules.crawl_delay_seconds is not None:
            state.stats.robots_delay_ms = round(rules.crawl_delay_seconds * 1000)
            limiter = self.registry.apply_robots_delay(origin, rules.crawl_delay_seconds)
        state.stats.effective_interval_ms = limiter.interval_ms

    def enqueue(self, raw_url: str, base: str | None, depth: int, source: str) -> None:
        """规范化、白名单与 robots 前置过滤后加入对应 origin 的 FIFO。"""
        try:
            target = normalize_url(raw_url, base)
        except PagefreezeError:
            return
        if target in self._handled:
            return
        self._handled.add(target)
        if not is_loopback_target(target):
            self.rejected.append(RejectedTarget(source_url=source, target=target,
                reason="TARGET_NOT_LOOPBACK"))
            return
        if depth > self.config.max_depth:
            return
        origin = origin_of(target)
        state = self._state_for(origin)
        item = FrontierItem(target, urlkey(target), depth)
        if state.rules is not None and not state.rules.allowed(path_and_query(target)):
            self.logs.append(log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            state.stats.robots_skipped += 1
            return
        state.frontier.add(target, item.urlkey, depth)

    def _due_states(self) -> list[_OriginState]:
        """返回已就绪且队列非空、当前已到期的 origin；无到期则返回全部候选。"""
        candidates = [state for state in self._states.values()
            if state.ready and state.frontier]
        if not candidates:
            return []
        now = self.clock.now()
        due = [state for state in candidates
            if self.registry.get(state.origin).next_allowed_at() <= now]
        return due or candidates

    def run(self) -> CrawlResult:
        for seed in self.config.seed_urls:
            target = normalize_url(seed)
            self._ensure_origin(origin_of(target))
            self.enqueue(seed, None, 0, seed)
        pending: dict[Future, tuple[_OriginState, FrontierItem]] = {}
        with ThreadPoolExecutor(max_workers=self.config.max_concurrency) as pool:
            while pending or (self._attempts < self.config.max_pages
                              and any(state.frontier for state in self._states.values())):
                self._pump(pool, pending)
        return CrawlResult(hits=self.hits, logs=self.logs, rejected=self.rejected,
            origin_stats=self.origin_stats)

    def _pump(self, pool: ThreadPoolExecutor,
              pending: dict[Future, tuple[_OriginState, FrontierItem]]) -> None:
        slots = self.config.max_concurrency - len(pending)
        budget = self.config.max_pages - self._attempts
        while slots > 0 and budget > 0:
            ready = self._due_states()
            if not ready:
                break
            state = min(ready, key=lambda item: (
                self.registry.get(item.origin).next_allowed_at(), item.origin))
            limiter = self.registry.get(state.origin)
            if limiter.next_allowed_at() > self.clock.now() and \
                    slots < self.config.max_concurrency:
                break
            item = state.frontier.pop_batch(1)[0]
            start = limiter.wait()
            state.stats.record_start(start - self._start)
            future = pool.submit(fetch_once, item.url, self.config)
            pending[future] = (state, item)
            slots -= 1
            budget -= 1
            self._attempts += 1
        if not pending:
            return
        self._collect(pending, block=False)
        if pending:
            blocked = next(iter(pending))
            blocked_state, blocked_item = pending.pop(blocked)
            self._resolve(blocked, blocked_state, blocked_item)
            self._collect(pending, block=False)

    def _collect(self, pending: dict[Future, tuple[_OriginState, FrontierItem]],
                 block: bool) -> None:
        """收割已完成的 future；所有抓取异常都在此转成审计日志。"""
        for future in tuple(pending):
            if not block and not future.done():
                continue
            state, item = pending.pop(future)
            self._resolve(future, state, item)

    def _resolve(self, future: Future, state: _OriginState, item: FrontierItem) -> None:
        try:
            future.result()
        except BodyTooLarge as exc:
            self.logs.append(log_for(item, None, "", exc.size, "skipped", "body_too_large"))
            return
        except FetchTimeout:
            self.logs.append(log_for(item, None, "", 0, "error", "timeout"))
            return
        except (FetchError, PolicyError) as exc:
            self.logs.append(log_for(item, None, "", 0, "error", exc.code.lower()))
            return
        result = future.result()
        self._handle_result(result, item)

    def _handle_result(self, result: FetchResult, item: FrontierItem) -> None:
        """分类一条 HTTP 响应，并按需发现重定向或 HTML 链接。"""
        location = result.redirect_location
        if location:
            self._discover(urljoin(result.url, location), item.depth + 1, result.url)
        if result.mime not in self.config.allowed_mime:
            self.logs.append(log_for(item, result.status, result.mime, result.size,
                "skipped", "mime_not_allowed"))
            return
        self.hits.append(result)
        self.logs.append(log_for(item, result.status, result.mime, result.size, "archived"))
        if result.mime == "text/html" and not result.is_gzip and not location:
            for link in extract_links(result.body):
                self._discover(urljoin(result.url, link), item.depth + 1, result.url)

    def _discover(self, raw_url: str, depth: int, source: str) -> None:
        """重定向/链接发现：先过白名单，再确保目标 origin 策略已加载。"""
        try:
            target = normalize_url(raw_url, source)
        except PagefreezeError:
            return
        if not is_loopback_target(target):
            if target not in self._handled:
                self._handled.add(target)
                self.rejected.append(RejectedTarget(source_url=source, target=target,
                    reason="TARGET_NOT_LOOPBACK"))
            return
        self._ensure_origin(origin_of(target))
        self.enqueue(raw_url, source, depth, source)


def run_crawl(config: CrawlConfig, clock: Clock | None = None) -> CrawlResult:
    """执行一次有界抓取；结果与线程调度顺序无关，交给写入层排序。"""
    return CrawlEngine(config, clock).run()
