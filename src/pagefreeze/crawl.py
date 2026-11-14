"""有界 BFS 抓取：白名单、robots、并发上限、页数/深度与跳过留痕。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin
from .config import CrawlConfig
from .errors import BodyTooLarge, FetchError, FetchTimeout, PagefreezeError, PolicyError
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem
from .links import extract_links
from .policy import (
    RobotsRules, is_loopback_target, normalize_url, parse_robots,
    path_and_query, robots_url_for, urlkey,
)
from .ratelimit import RateLimiter


@dataclass(frozen=True)
class FetchLog:
    """一次抓取尝试（含未发连接的 robots 跳过）的审计记录。"""
    url: str
    urlkey: str
    depth: int
    status: int | None
    mime: str
    size: int
    outcome: str
    reason: str | None = None


@dataclass(frozen=True)
class RejectedTarget:
    """发现但未建立连接的非回环目标。"""
    source_url: str
    target: str
    reason: str


@dataclass
class CrawlResult:
    """一次抓取的全部可归档响应与审计信息。"""
    hits: list[FetchResult]
    logs: list[FetchLog]
    rejected: list[RejectedTarget]

    @property
    def archived(self) -> int:
        return sum(1 for log in self.logs if log.outcome == "archived")

    @property
    def skipped(self) -> int:
        return sum(1 for log in self.logs if log.outcome != "archived")

    @property
    def bytes(self) -> int:
        return sum(hit.size for hit in self.hits)

    def skip_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for log in self.logs:
            if log.outcome != "archived" and log.reason:
                counts[log.reason] = counts.get(log.reason, 0) + 1
        return dict(sorted(counts.items()))


def _log_for(
    item: FrontierItem, status: int | None, mime: str, size: int,
             outcome: str, reason: str | None = None) -> FetchLog:
    return FetchLog(url=item.url,
        urlkey=item.urlkey,
        depth=item.depth,
        status=status,
        mime=mime,
        size=size,
        outcome=outcome,
        reason=reason,
    )


def _load_robots(
    config: CrawlConfig,
    limiter: RateLimiter,
    logs: list[FetchLog],
    hits: list[FetchResult],
) -> RobotsRules | None:
    first = normalize_url(config.seed_urls[0])
    robots_target = robots_url_for(first)
    item = FrontierItem(robots_target, urlkey(robots_target), 0)
    try:
        result = fetch_once(robots_target, config, limiter)
    except FetchError as exc:
        logs.append(_log_for(item, None, "", 0, "error", exc.code.lower()))
        return None
    if result.mime not in config.allowed_mime:
        logs.append(_log_for(item, result.status, result.mime, result.size, "skipped", "mime_not_allowed"))
        return None
    hits.append(result)
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
    if result.status == 200:
        text = result.body.decode("utf-8", errors="replace")
        return parse_robots(text, config.user_agent)
    return None


def run_crawl(config: CrawlConfig) -> CrawlResult:
    """执行一次有界抓取；结果与线程调度顺序无关，交给写入层排序。"""
    limiter = RateLimiter(config.request_interval_ms)
    hits: list[FetchResult] = []
    logs: list[FetchLog] = []
    rejected: list[RejectedTarget] = []
    handled: set[str] = set()
    frontier = Frontier()
    robots: RobotsRules | None = None
    if config.respect_robots:
        robots = _load_robots(config, limiter, logs, hits)
        try:
            robots_target = robots_url_for(config.seed_urls[0])
            handled.add(robots_target)
        except PagefreezeError:
            pass

    def enqueue(raw_url: str, base: str | None, depth: int, source: str) -> None:
        try:
            target = normalize_url(raw_url, base)
        except PagefreezeError:
            return
        if target in handled:
            return
        handled.add(target)
        if not is_loopback_target(target):
            rejected.append(RejectedTarget(source_url=source, target=target, reason="TARGET_NOT_LOOPBACK"))
            return
        if depth > config.max_depth:
            return
        item = FrontierItem(target, urlkey(target), depth)
        if robots is not None and not robots.allowed(path_and_query(target)):
            logs.append(_log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            return
        frontier.add(target, item.urlkey, depth)
    for seed in config.seed_urls:
        enqueue(seed, None, 0, seed)
    attempts = 0
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        while frontier and attempts < config.max_pages:
            remaining = config.max_pages - attempts
            batch = frontier.pop_batch(min(config.max_concurrency, remaining))
            attempts += len(batch)
            futures = [pool.submit(fetch_once, item.url, config, limiter) for item in batch]
            for item, future in zip(batch, futures):
                try:
                    result = future.result()
                except BodyTooLarge as exc:
                    logs.append(_log_for(item, None, "", exc.size, "skipped", "body_too_large"))
                    continue
                except FetchTimeout:
                    logs.append(_log_for(item, None, "", 0, "error", "timeout"))
                    continue
                except (FetchError, PolicyError) as exc:
                    logs.append(_log_for(item, None, "", 0, "error", exc.code.lower()))
                    continue
                _handle_result(result, item, config, enqueue, hits, logs)
    return CrawlResult(hits=hits, logs=logs, rejected=rejected)


def _handle_result(
    result: FetchResult,
    item: FrontierItem,
    config: CrawlConfig,
    enqueue: Callable[[str, str | None, int, str], None],
    hits: list[FetchResult],
    logs: list[FetchLog],
) -> None:
    """分类一条 HTTP 响应，并按需发现重定向或 HTML 链接。"""
    location = result.redirect_location
    if location:
        next_depth = item.depth + 1
        enqueue(urljoin(result.url, location), None, next_depth, result.url)
    if result.mime not in config.allowed_mime:
        logs.append(_log_for(item, result.status, result.mime, result.size, "skipped", "mime_not_allowed"))
        return
    hits.append(result)
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
    if result.mime == "text/html" and not result.is_gzip and not location:
        for link in extract_links(result.body):
            enqueue(urljoin(result.url, link), None, item.depth + 1, result.url)
