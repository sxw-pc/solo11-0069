"""有界 BFS 抓取：白名单、robots、并发上限、页数/深度与跳过留痕。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin
from .config import CrawlConfig
from .errors import BodyTooLarge, FetchError, FetchTimeout, PagefreezeError, PolicyError
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem
from .gzipbody import GzipBodyError, decode_gzip_members
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


LINK_DISCOVERY_SKIP = "link_discovery_skip"


@dataclass(frozen=True)
class LinkDiscoverySkip:
    """已归档页面因实体不完整而放弃本轮全部链接发现。

    ``reason`` 是稳定且固定的 ``link_discovery_skip``；``detail`` 保留具体
    诊断（截断 / CRC / ISIZE / 超限），两者都不表示抓取失败。
    """
    url: str
    urlkey: str
    depth: int
    reason: str
    detail: str


@dataclass
class CrawlResult:
    """一次抓取的全部可归档响应与审计信息。"""
    hits: list[FetchResult]
    logs: list[FetchLog]
    rejected: list[RejectedTarget]
    link_skips: list[LinkDiscoverySkip] = field(default_factory=list)

    @property
    def archived(self) -> int:
        return sum(1 for log in self.logs if log.outcome == "archived")

    @property
    def skipped(self) -> int:
        """未归档的抓取尝试数；链接发现跳过是已归档页面的审计项，不在此列。"""
        return sum(1 for log in self.logs if log.outcome != "archived")

    @property
    def bytes(self) -> int:
        return sum(hit.size for hit in self.hits)

    def skip_reasons(self) -> dict[str, int]:
        """稳定跳过原因统计，包含抓取未归档原因与链接发现跳过。"""
        counts: dict[str, int] = {}
        for log in self.logs:
            if log.outcome != "archived" and log.reason:
                counts[log.reason] = counts.get(log.reason, 0) + 1
        for skip in self.link_skips:
            counts[skip.reason] = counts.get(skip.reason, 0) + 1
        return dict(sorted(counts.items()))

    def fetch_skip_reasons(self) -> dict[str, int]:
        """只统计抓取层未归档原因，不含链接发现跳过。"""
        counts: dict[str, int] = {}
        for log in self.logs:
            if log.outcome != "archived" and log.reason:
                counts[log.reason] = counts.get(log.reason, 0) + 1
        return dict(sorted(counts.items()))

    def link_skip_reasons(self) -> dict[str, int]:
        """链接发现跳过原因；与抓取失败分开统计。"""
        counts: dict[str, int] = {}
        for skip in self.link_skips:
            counts[skip.reason] = counts.get(skip.reason, 0) + 1
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
    link_skips: list[LinkDiscoverySkip] = []
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
                _handle_result(result, item, config, enqueue, hits, logs, link_skips)
    return CrawlResult(hits=hits, logs=logs, rejected=rejected, link_skips=link_skips)


def _stage_links(result: FetchResult) -> tuple[list[str] | None, str | None]:
    """完整验证响应实体后才提取 href；失败返回 ``(None, 稳定原因码)``。

    路线 A：任一 gzip 成员截断、CRC/ISIZE 错误、尾随非成员字节或解压
    超限时，整个响应贡献零个链接。本函数只读 ``result``，不触碰
    handled / rejected / frontier / logs 等跨响应状态，因此失败时
    不存在需要回滚的部分提交。
    """
    if not result.is_gzip:
        return extract_links(result.body), None
    try:
        document = decode_gzip_members(result.body)
    except GzipBodyError as exc:
        return None, exc.code
    return extract_links(document), None


def _handle_result(
    result: FetchResult,
    item: FrontierItem,
    config: CrawlConfig,
    enqueue: Callable[[str, str | None, int, str], None],
    hits: list[FetchResult],
    logs: list[FetchLog],
    link_skips: list[LinkDiscoverySkip] | None = None,
) -> list[LinkDiscoverySkip]:
    """分类一条 HTTP 响应，并按需发现重定向或 HTML 链接。

    链接先整批暂存：只有实体完整验证通过后，才按出现顺序交给 ``enqueue``
    做规范化、回环、robots 与 frontier 变更。返回本次新增的链接发现跳过。
    """
    skips = link_skips if link_skips is not None else []
    first_new = len(skips)
    location = result.redirect_location
    if location:
        next_depth = item.depth + 1
        enqueue(urljoin(result.url, location), None, next_depth, result.url)
    if result.mime not in config.allowed_mime:
        logs.append(_log_for(item, result.status, result.mime, result.size, "skipped", "mime_not_allowed"))
        return skips[first_new:]
    hits.append(result)
    links: list[str] | None = None
    reason: str | None = None
    if result.mime == "text/html" and not location:
        links, code = _stage_links(result)
        if code is not None:
            reason = LINK_DISCOVERY_SKIP
            skips.append(LinkDiscoverySkip(url=result.url,
                urlkey=item.urlkey,
                depth=item.depth,
                reason=reason,
                detail=code,
            ))
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived", reason))
    if links:
        for link in links:
            enqueue(urljoin(result.url, link), None, item.depth + 1, result.url)
    return skips[first_new:]
