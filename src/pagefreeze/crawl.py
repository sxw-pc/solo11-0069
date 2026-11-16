"""有界 BFS 抓取：白名单、robots、Sitemap 入口、并发上限与跳过留痕。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin
from .audit import FetchLog, RejectedTarget, log_for
from .config import CrawlConfig, SITEMAP_PRIORITY
from .errors import BodyTooLarge, FetchError, FetchTimeout, PagefreezeError, PolicyError
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem, KIND_PAGE, KIND_ROBOTS, KIND_SITEMAP
from .links import extract_links
from .policy import (
    RobotsDocument, RobotsRules, is_loopback_target, normalize_url, parse_robots,
    path_and_query, robots_url_for, urlkey,
)
from .ratelimit import RateLimiter
from .sitemap_crawl import discover_sitemaps

# 向后兼容：历史调用方从 ``pagefreeze.crawl`` 导入这两个审计类型。
__all__ = [
    "FetchLog", "RejectedTarget", "CrawlResult", "run_crawl", "KIND_PAGE",
    "KIND_ROBOTS", "KIND_SITEMAP",
]


@dataclass
class CrawlResult:
    """一次抓取的全部可归档响应与审计信息。"""
    hits: list[FetchResult]
    logs: list[FetchLog]
    rejected: list[RejectedTarget]
    sitemap_strategy: str = SITEMAP_PRIORITY

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

    def _kind_rows(self) -> dict[str, dict[str, int]]:
        rows = {kind: {"attempted": 0, "archived": 0, "skipped": 0}
            for kind in (KIND_PAGE, KIND_SITEMAP, KIND_ROBOTS)}
        for log in self.logs:
            bucket = rows.setdefault(log.kind, {"attempted": 0, "archived": 0, "skipped": 0})
            bucket["attempted"] += 1
            if log.outcome == "archived":
                bucket["archived"] += 1
            else:
                bucket["skipped"] += 1
        return rows

    def source_breakdown(self) -> dict[str, dict[str, int]]:
        """按请求类别（页面 / 站点地图 / robots）统计尝试、归档与跳过。"""
        return self._kind_rows()

    def page_source_breakdown(self) -> dict[str, dict[str, int]]:
        """页面类请求再按发现来源（HTML 链接 / Sitemap loc / 种子）拆分。"""
        rows: dict[str, dict[str, int]] = {}
        for log in self.logs:
            if log.kind != KIND_PAGE:
                continue
            source = log.source_url or "seed"
            bucket = rows.setdefault(source, {"attempted": 0, "archived": 0, "skipped": 0})
            bucket["attempted"] += 1
            if log.outcome == "archived":
                bucket["archived"] += 1
            else:
                bucket["skipped"] += 1
        return dict(sorted(rows.items()))


def _log_for(
    item: FrontierItem, status: int | None, mime: str, size: int,
             outcome: str, reason: str | None = None) -> FetchLog:
    # 委托给共享审计辅助，保持本模块内部调用名不变。
    return log_for(item, status, mime, size, outcome, reason)


def _load_robots(
    config: CrawlConfig,
    limiter: RateLimiter,
    logs: list[FetchLog],
    hits: list[FetchResult],
) -> RobotsDocument | None:
    first = normalize_url(config.seed_urls[0])
    robots_target = robots_url_for(first)
    item = FrontierItem(robots_target, urlkey(robots_target), 0, kind=KIND_ROBOTS)
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
    priority_first = config.sitemap_entry_strategy == SITEMAP_PRIORITY
    frontier = Frontier(priority_first=priority_first)
    robots: RobotsRules | None = None
    robots_doc: RobotsDocument | None = None
    if config.respect_robots:
        robots_doc = _load_robots(config, limiter, logs, hits)
        try:
            robots_target = robots_url_for(config.seed_urls[0])
            handled.add(robots_target)
        except PagefreezeError:
            pass
        if robots_doc is not None:
            robots = robots_doc.rules

    def enqueue(raw_url: str, base: str | None, depth: int, source: str,
                *, kind: str = KIND_PAGE, parent_sitemap: str | None = None,
                priority: bool = False, head: bool = False) -> None:
        try:
            target = normalize_url(raw_url, base)
        except PagefreezeError:
            return
        if target in handled:
            return
        handled.add(target)
        if not is_loopback_target(target):
            rejected.append(RejectedTarget(source_url=source, target=target,
                reason="TARGET_NOT_LOOPBACK", source_kind=kind))
            return
        if depth > config.max_depth:
            return
        item = FrontierItem(target, urlkey(target), depth, kind=kind,
            source_url=source, parent_sitemap=parent_sitemap)
        if robots is not None and not robots.allowed(path_and_query(target)):
            logs.append(_log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            return
        frontier.add(target, item.urlkey, depth, kind=kind, source_url=source,
            parent_sitemap=parent_sitemap, priority=priority, head=head)

    for seed in config.seed_urls:
        enqueue(seed, None, 0, seed, head=True)
    if robots_doc is not None:
        discover_sitemaps(config, limiter, logs, hits, rejected, handled,
            robots_doc, frontier, robots, normalize_url(config.seed_urls[0]))
    attempts = 0
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        while frontier and attempts < config.max_pages:
            remaining = config.max_pages - attempts
            batch = frontier.pop_batch(min(config.max_concurrency, remaining))
            page_items = [item for item in batch if item.kind == KIND_PAGE]
            attempts += len(page_items)
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
    return CrawlResult(hits=hits, logs=logs, rejected=rejected,
        sitemap_strategy=config.sitemap_entry_strategy)



def _handle_result(
    result: FetchResult,
    item: FrontierItem,
    config: CrawlConfig,
    enqueue: Callable[..., None],
    hits: list[FetchResult],
    logs: list[FetchLog],
) -> None:
    """分类一条 HTTP 响应，并按需发现重定向或 HTML 链接。"""
    location = result.redirect_location
    if location:
        next_depth = item.depth + 1
        enqueue(urljoin(result.url, location), None, next_depth, result.url,
            kind=item.kind)
    if result.mime not in config.allowed_mime:
        logs.append(_log_for(item, result.status, result.mime, result.size, "skipped", "mime_not_allowed"))
        return
    hits.append(result)
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
    if result.mime == "text/html" and not result.is_gzip and not location:
        for link in extract_links(result.body):
            enqueue(urljoin(result.url, link), None, item.depth + 1, result.url)
