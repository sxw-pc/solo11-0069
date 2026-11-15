"""有界 BFS 抓取：白名单、robots、站点地图入口、并发上限与跳过留痕。

站点地图入口有两种完整策略，由 ``crawl.sitemap_entry`` 选择：

- ``priority``（默认，策略 A）：robots 阶段同步取完站点地图，声明页作为
  独立批次进入 frontier 的中间层——排在种子之后、HTML 发现链接之前。
  预算紧张时优先履行站点声明的目录。
- ``bfs``（策略 B）：站点地图 XML 本身作为不消耗页数预算的 frontier 条目
  进入同一个 FIFO，其 loc 与 HTML 链接一样追加到队尾，保持经典 BFS 覆盖。

两种策略下，站点地图页面 URL 都经过同一套 robots 路径检查、回环策略、
URL 规范化、visited 去重与 depth/max_pages/body/MIME 限制；站点地图文件
本身只对同 origin 声明抓取，递归深度与文件数另有硬上限并检测 URL 循环。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin
from .config import CrawlConfig
from .errors import (
    BodyTooLarge, FetchError, FetchTimeout, PagefreezeError, PolicyError, SitemapError,
)
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem
from .links import extract_links
from .policy import (
    RobotsRules, is_loopback_target, normalize_url, parse_robots,
    parse_robots_sitemaps, path_and_query, robots_url_for, same_origin, urlkey,
)
from .ratelimit import RateLimiter
from .sitemap import parse_sitemap_xml

_TIER_SEED = 0
_TIER_SITEMAP = 1
_TIER_HTML = 2
_SITEMAP_PAGE_DEPTH = 1


@dataclass(frozen=True)
class FetchLog:
    """一次抓取尝试（含未发连接的跳过）的审计记录。

    ``kind`` 区分 robots / sitemap / page 三类请求；``source`` 记录触发该
    抓取的来源（种子、发现链接的页面、声明它的 robots 或父站点地图）。
    """
    url: str
    urlkey: str
    depth: int
    status: int | None
    mime: str
    size: int
    outcome: str
    reason: str | None = None
    kind: str = "page"
    source: str | None = None


@dataclass(frozen=True)
class RejectedTarget:
    """发现但未建立连接的非同 origin / 非回环目标。"""
    source_url: str
    target: str
    reason: str


@dataclass
class CrawlResult:
    """一次抓取的全部可归档响应与审计信息。"""
    hits: list[FetchResult]
    logs: list[FetchLog]
    rejected: list[RejectedTarget]
    entry_strategy: str = "priority"

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

    def kind_counts(self) -> dict[str, dict[str, int]]:
        """按 robots / sitemap / page 汇总尝试、归档与跳过数，供报告归类。"""
        counts: dict[str, dict[str, int]] = {
            kind: {"attempted": 0, "archived": 0, "skipped": 0}
            for kind in ("page", "sitemap", "robots")
        }
        for log in self.logs:
            bucket = counts.setdefault(log.kind,
                {"attempted": 0, "archived": 0, "skipped": 0})
            bucket["attempted"] += 1
            if log.outcome == "archived":
                bucket["archived"] += 1
            else:
                bucket["skipped"] += 1
        return counts


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
        kind=item.kind,
        source=item.source,
    )


def _load_robots(
    config: CrawlConfig,
    limiter: RateLimiter,
    logs: list[FetchLog],
    hits: list[FetchResult],
) -> tuple[RobotsRules | None, tuple[str, ...]]:
    """抓取种子 origin 的 robots.txt，返回规则与原始 Sitemap 声明列表。"""
    first = normalize_url(config.seed_urls[0])
    robots_target = robots_url_for(first)
    item = FrontierItem(robots_target, urlkey(robots_target), 0,
        kind="robots", source=first)
    try:
        result = fetch_once(robots_target, config, limiter)
    except FetchError as exc:
        logs.append(_log_for(item, None, "", 0, "error", exc.code.lower()))
        return None, ()
    if result.mime not in config.allowed_mime:
        logs.append(_log_for(item, result.status, result.mime, result.size,
            "skipped", "mime_not_allowed"))
        return None, ()
    hits.append(result)
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
    if result.status == 200:
        text = result.body.decode("utf-8", errors="replace")
        return parse_robots(text, config.user_agent), parse_robots_sitemaps(text)
    return None, ()


class _SitemapBudget:
    """站点地图抓取的硬边界：文件数上限、循环检测与有限的跳过留痕。"""

    def __init__(self, config: CrawlConfig) -> None:
        self._max_files = config.sitemap_max_files
        self._skip_logs_left = config.sitemap_max_files
        self.seen: set[str] = set()
        self.fetched = 0

    def admit(self, url: str, source: str, depth: int, logs: list[FetchLog]) -> bool:
        """登记一个待抓站点地图；超限时写跳过日志（条数同样受限）并拒绝。"""
        reason: str | None = None
        if url in self.seen:
            reason = "sitemap_cycle"
        elif self.fetched >= self._max_files:
            reason = "sitemap_file_limit"
        if reason is None:
            self.seen.add(url)
            self.fetched += 1
            return True
        if self._skip_logs_left > 0:
            self._skip_logs_left -= 1
            item = FrontierItem(url, urlkey(url), depth, kind="sitemap", source=source)
            logs.append(_log_for(item, None, "", 0, "skipped", reason))
        return False


def run_crawl(config: CrawlConfig) -> CrawlResult:
    """执行一次有界抓取；结果与线程调度顺序无关，交给写入层排序。"""
    limiter = RateLimiter(config.request_interval_ms)
    hits: list[FetchResult] = []
    logs: list[FetchLog] = []
    rejected: list[RejectedTarget] = []
    handled: set[str] = set()
    frontier = Frontier()
    robots: RobotsRules | None = None
    declarations: tuple[str, ...] = ()
    robots_target = robots_url_for(normalize_url(config.seed_urls[0]))
    if config.respect_robots:
        robots, declarations = _load_robots(config, limiter, logs, hits)
        handled.add(robots_target)
    budget = _SitemapBudget(config)

    def enqueue(raw_url: str, base: str | None, depth: int, source: str,
                tier: int = _TIER_HTML) -> None:
        try:
            target = normalize_url(raw_url, base)
        except PagefreezeError:
            return
        if target in handled:
            return
        handled.add(target)
        if not is_loopback_target(target):
            rejected.append(RejectedTarget(source_url=source, target=target,
                reason="TARGET_NOT_LOOPBACK"))
            return
        if depth > config.max_depth:
            return
        item = FrontierItem(target, urlkey(target), depth, kind="page", source=source)
        if robots is not None and not robots.allowed(path_and_query(target)):
            logs.append(_log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            return
        frontier.add(item, tier)

    def admit_sitemap(raw_url: str, base: str | None, source: str,
                      index_depth: int) -> str | None:
        """校验一个站点地图声明，返回可抓取的规范化 URL 或 None。

        跨 origin 声明在连接前记入 rejected；深度超限、循环与文件预算
        超限只写跳过日志，不建立连接。
        """
        try:
            target = normalize_url(raw_url, base)
        except PagefreezeError:
            item_source = source
            logs.append(FetchLog(url=raw_url, urlkey="", depth=index_depth,
                status=None, mime="", size=0, outcome="error",
                reason="sitemap_invalid_url", kind="sitemap", source=item_source))
            return None
        if not same_origin(target, robots_target):
            rejected.append(RejectedTarget(source_url=source, target=target,
                reason="SITEMAP_CROSS_ORIGIN"))
            return None
        if index_depth > config.sitemap_max_depth:
            if budget.skip_allowed():
                item = FrontierItem(target, urlkey(target), index_depth,
                    kind="sitemap", source=source)
                logs.append(_log_for(item, None, "", 0, "skipped", "sitemap_depth_limit"))
            return None
        if not budget.admit(target, source, index_depth, logs):
            return None
        handled.add(target)
        return target

    def fetch_sitemap(url: str, source: str, index_depth: int) -> None:
        """抓取并解析一个站点地图，返回其声明的页面与子表（策略 A 同步用）。"""
        item = FrontierItem(url, urlkey(url), index_depth, kind="sitemap", source=source)
        try:
            result = fetch_once(url, config, limiter)
        except (FetchError, PolicyError) as exc:
            logs.append(_log_for(item, None, "", 0, "error", exc.code.lower()))
            return
        handle_sitemap_result(result, item, index_depth)

    def handle_sitemap_result(result: FetchResult, item: FrontierItem,
                              index_depth: int) -> None:
        """分类站点地图响应：归档允许则归档，200 则解析 loc 并继续入口逻辑。"""
        if result.mime not in config.allowed_mime:
            logs.append(_log_for(item, result.status, result.mime, result.size,
                "skipped", "mime_not_allowed"))
            return
        hits.append(result)
        logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
        if result.status != 200:
            return
        try:
            document = parse_sitemap_xml(result.body)
        except SitemapError as exc:
            logs.append(FetchLog(url=item.url, urlkey=item.urlkey,
                depth=item.depth, status=result.status, mime=result.mime,
                size=result.size, outcome="error", reason="sitemap_malformed",
                kind="sitemap", source=item.source))
            return
        if document.kind == "urlset":
            for loc in document.locs:
                enqueue(loc, result.url, _SITEMAP_PAGE_DEPTH, result.url,
                    _TIER_SITEMAP if config.sitemap_entry == "priority" else _TIER_SEED)
        else:
            for loc in document.locs:
                child = admit_sitemap(loc, result.url, result.url, index_depth + 1)
                if child is None:
                    continue
                if config.sitemap_entry == "priority":
                    fetch_sitemap(child, result.url, index_depth + 1)
                else:
                    frontier.add(FrontierItem(child, urlkey(child), index_depth + 1,
                        kind="sitemap", source=result.url), _TIER_SEED)

    for seed in config.seed_urls:
        enqueue(seed, None, 0, seed, _TIER_SEED)
    for declared in declarations:
        target = admit_sitemap(declared, None, robots_target, 0)
        if target is None:
            continue
        if config.sitemap_entry == "priority":
            fetch_sitemap(target, robots_target, 0)
        else:
            frontier.add(FrontierItem(target, urlkey(target), 0,
                kind="sitemap", source=robots_target), _TIER_SEED)
    attempts = 0
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        while frontier and attempts < config.max_pages:
            remaining = config.max_pages - attempts
            batch = frontier.pop_batch(min(config.max_concurrency, remaining))
            attempts += sum(1 for item in batch if item.kind == "page")
            futures = [(item, pool.submit(fetch_once, item.url, config, limiter))
                for item in batch]
            for item, future in futures:
                if item.kind == "sitemap":
                    try:
                        handle_sitemap_result(future.result(), item, item.depth)
                    except (FetchError, PolicyError) as exc:
                        logs.append(_log_for(item, None, "", 0, "error",
                            exc.code.lower()))
                    continue
                try:
                    result = future.result()
                except BodyTooLarge as exc:
                    logs.append(_log_for(item, None, "", exc.size, "skipped",
                        "body_too_large"))
                    continue
                except FetchTimeout:
                    logs.append(_log_for(item, None, "", 0, "error", "timeout"))
                    continue
                except (FetchError, PolicyError) as exc:
                    logs.append(_log_for(item, None, "", 0, "error", exc.code.lower()))
                    continue
                _handle_result(result, item, config, enqueue, hits, logs)
    return CrawlResult(hits=hits, logs=logs, rejected=rejected,
        entry_strategy=config.sitemap_entry)


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
        logs.append(_log_for(item, result.status, result.mime, result.size,
            "skipped", "mime_not_allowed"))
        return
    hits.append(result)
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
    if result.mime == "text/html" and not result.is_gzip and not location:
        for link in extract_links(result.body):
            enqueue(urljoin(result.url, link), None, item.depth + 1, result.url)
