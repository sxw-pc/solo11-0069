"""Sitemap 发现编排：robots 声明 → 同源自检 → 有界递归 → loc 入队。

与 :mod:`pagefreeze.sitemap`（纯 XML 解析）分工：本模块负责网络抓取、
robots/回环/MIME/预算约束、循环检测与审计留痕；XML 结构解析完全委托给
``parse_sitemap``，这里不出现任何 XML 处理代码。
"""
from __future__ import annotations

from .audit import FetchLog, RejectedTarget, is_xml_mime, log_for
from .config import CrawlConfig
from .errors import BodyTooLarge, FetchError, PagefreezeError
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem, KIND_PAGE, KIND_SITEMAP
from .policy import (
    RobotsDocument, RobotsRules, is_loopback_target, normalize_url,
    path_and_query, robots_url_for, same_origin, urlkey,
)
from .ratelimit import RateLimiter
from .sitemap import SitemapParseError, parse_sitemap


def discover_sitemaps(
    config: CrawlConfig,
    limiter: RateLimiter,
    logs: list[FetchLog],
    hits: list[FetchResult],
    rejected: list[RejectedTarget],
    handled: set[str],
    robots_doc: RobotsDocument,
    frontier: Frontier,
    robots: RobotsRules | None,
    seed_origin: str,
) -> None:
    """展开 robots.txt 声明的全部 Sitemap，有界递归并检测 URL 循环。"""
    robots_target = robots_url_for(seed_origin)
    queue: list[tuple[str, str, int, str | None]] = []
    queued: set[str] = set()
    for declaration in robots_doc.sitemaps:
        try:
            target = normalize_url(declaration.raw_url)
        except PagefreezeError:
            rejected.append(RejectedTarget(source_url=robots_target,
                target=declaration.raw_url, reason="URL_INVALID",
                source_kind=KIND_SITEMAP))
            continue
        if not same_origin(target, seed_origin):
            rejected.append(RejectedTarget(source_url=robots_target,
                target=target, reason="SITEMAP_CROSS_ORIGIN",
                source_kind=KIND_SITEMAP))
            continue
        queue.append((target, robots_target, 0, None))
        queued.add(target)
    while queue:
        sitemap_url, source_url, sitemap_depth, parent = queue.pop(0)
        queued.discard(sitemap_url)
        if sitemap_url in handled:
            continue
        handled.add(sitemap_url)
        if not is_loopback_target(sitemap_url):
            rejected.append(RejectedTarget(source_url=source_url, target=sitemap_url,
                reason="TARGET_NOT_LOOPBACK", source_kind=KIND_SITEMAP))
            continue
        item = FrontierItem(sitemap_url, urlkey(sitemap_url), 0, kind=KIND_SITEMAP,
            source_url=source_url, parent_sitemap=parent)
        if robots is not None and not robots.allowed(path_and_query(sitemap_url)):
            logs.append(log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            continue
        try:
            result = fetch_once(sitemap_url, config, limiter)
        except BodyTooLarge as exc:
            logs.append(log_for(item, None, "", exc.size, "skipped", "body_too_large"))
            continue
        except FetchError as exc:
            logs.append(log_for(item, None, "", 0, "error", exc.code.lower()))
            continue
        if result.status != 200:
            hits.append(result)
            logs.append(log_for(item, result.status, result.mime, result.size, "archived"))
            continue
        if not is_xml_mime(result.mime):
            # 声明为 Sitemap 的资源却返回非 XML（例如 HTML 错误页）：只留痕、
            # 绝不送入链接提取或 XML 解析，防止把 XML/HTML 互相误判。
            logs.append(log_for(item, result.status, result.mime, result.size,
                "skipped", "mime_not_allowed"))
            continue
        try:
            parsed = parse_sitemap(result.body, sitemap_url)
        except SitemapParseError as exc:
            # 畸形 XML 只记错误：不归档、不提取任何链接，继续其余声明与 HTML frontier。
            logs.append(log_for(item, result.status, result.mime, result.size,
                "skipped", exc.code.lower()))
            continue
        hits.append(result)
        logs.append(log_for(item, result.status, result.mime, result.size, "archived"))
        next_depth = sitemap_depth + 1
        for child in parsed.child_sitemaps:
            if child.loc in handled or child.loc in queued:
                continue
            if next_depth > config.sitemap_max_depth:
                logs.append(FetchLog(url=child.loc, urlkey=urlkey(child.loc), depth=0,
                    status=None, mime="", size=0, outcome="skipped",
                    kind=KIND_SITEMAP, reason="sitemap_depth_limit",
                    source_url=sitemap_url, parent_sitemap=sitemap_url))
                continue
            if not same_origin(child.loc, seed_origin):
                rejected.append(RejectedTarget(source_url=sitemap_url, target=child.loc,
                    reason="SITEMAP_CROSS_ORIGIN", source_kind=KIND_SITEMAP))
                continue
            queue.append((child.loc, sitemap_url, next_depth, sitemap_url))
            queued.add(child.loc)
        for entry in parsed.pages:
            _enqueue_sitemap_page(entry.loc, sitemap_url, frontier,
                handled, rejected, logs, robots)


def _enqueue_sitemap_page(
    raw_target: str,
    sitemap_url: str,
    frontier: Frontier,
    handled: set[str],
    rejected: list[RejectedTarget],
    logs: list[FetchLog],
    robots: RobotsRules | None,
) -> None:
    """Sitemap 页面 loc 走与 HTML 链接相同的规范化、回环与 robots 检查。"""
    try:
        target = normalize_url(raw_target)
    except PagefreezeError:
        rejected.append(RejectedTarget(source_url=sitemap_url, target=raw_target,
            reason="URL_INVALID", source_kind=KIND_SITEMAP))
        return
    if target in handled:
        return
    handled.add(target)
    if not is_loopback_target(target):
        rejected.append(RejectedTarget(source_url=sitemap_url, target=target,
            reason="TARGET_NOT_LOOPBACK", source_kind=KIND_SITEMAP))
        return
    item = FrontierItem(target, urlkey(target), 0, kind=KIND_PAGE,
        source_url=sitemap_url, parent_sitemap=sitemap_url)
    if robots is not None and not robots.allowed(path_and_query(target)):
        logs.append(log_for(item, None, "", 0, "skipped", "robots_disallowed"))
        return
    frontier.add(target, item.urlkey, 0, kind=KIND_PAGE, source_url=sitemap_url,
        parent_sitemap=sitemap_url, priority=True)
