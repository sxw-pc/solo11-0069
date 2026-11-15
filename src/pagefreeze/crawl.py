"""有界 BFS 抓取：白名单、robots、并发上限、页数/深度与跳过留痕。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Callable
from urllib.parse import urljoin
from .config import CrawlConfig
from .crawl_audit import CrawlResult, FetchLog, RejectedTarget
from .errors import BodyTooLarge, FetchError, FetchTimeout, PagefreezeError, PolicyError
from .fetch import FetchResult, fetch_once
from .frontier import Frontier, FrontierItem
from .links import (
    ResourceRef, discover_css_resources, discover_html_resources, extract_links,
    resolve_reference,
)
from .policy import (
    RobotsRules, is_loopback_target, normalize_url, parse_robots,
    path_and_query, robots_url_for, urlkey,
)
from .ratelimit import RateLimiter

_CSS_KINDS = frozenset({"css-import", "css-url"})


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
        resource_depth=item.resource_depth,
        source_url=item.source_url,
        kind=item.kind,
        raw_reference=item.raw_reference,
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


def _finalize_resources(
    references: list[ResourceRef],
    logs: list[FetchLog],
    rejected: list[RejectedTarget],
) -> list[ResourceRef]:
    """把入队时的 ``queued`` 结果替换为最终抓取/跳过结果。"""
    by_url: dict[str, FetchLog] = {}
    for log in logs:
        by_url.setdefault(log.url, log)
    rejected_by_url = {item.target: item for item in rejected}
    finalized: list[ResourceRef] = []
    for reference in references:
        if reference.target is None or reference.outcome != "queued":
            finalized.append(reference)
            continue
        target = reference.target
        if target in rejected_by_url:
            finalized.append(replace(reference,
                outcome="rejected", reason=rejected_by_url[target].reason))
            continue
        log = by_url.get(target)
        if log is None:
            finalized.append(replace(reference, outcome="skipped", reason="not_fetched"))
        elif log.outcome == "archived":
            finalized.append(replace(reference, outcome="archived", reason=None))
        else:
            finalized.append(replace(reference, outcome=log.outcome, reason=log.reason))
    return finalized


def run_crawl(config: CrawlConfig) -> CrawlResult:
    """执行一次有界抓取；结果与线程调度顺序无关，交给写入层排序。"""
    limiter = RateLimiter(config.request_interval_ms)
    hits: list[FetchResult] = []
    logs: list[FetchLog] = []
    rejected: list[RejectedTarget] = []
    resources: list[ResourceRef] = []
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
            rejected.append(RejectedTarget(source_url=source, target=target,
                reason="TARGET_NOT_LOOPBACK"))
            return
        if depth > config.max_depth:
            return
        item = FrontierItem(target, urlkey(target), depth)
        if robots is not None and not robots.allowed(path_and_query(target)):
            logs.append(_log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            return
        frontier.add(target, item.urlkey, depth)

    def enqueue_resource(
        reference: ResourceRef,
        page_depth: int,
        chain: tuple[str, ...] = (),
    ) -> None:
        """资源引用边：先规范化再做回环/robots 校验，同一 URL 只入队一次。

        ``chain`` 是解析到本引用为止的 CSS 祖先链；CSS 目标已在链上说明
        出现导入环，记录明确的 cycle 结果并终止，不再请求同一资源。
        """
        if reference.target is None:
            resources.append(reference)
            return
        target = reference.target
        if reference.kind in _CSS_KINDS and target in chain:
            resources.append(replace(reference,
                outcome="skipped", reason="import_cycle", cycle=True, duplicate=True))
            return
        if reference.depth > config.resource_depth:
            resources.append(replace(reference,
                outcome="skipped", reason="resource_depth_limit"))
            return
        if target in handled:
            resources.append(replace(reference, outcome="duplicate", duplicate=True))
            return
        handled.add(target)
        if not is_loopback_target(target):
            rejected.append(RejectedTarget(source_url=reference.source_url,
                target=target,
                reason="TARGET_NOT_LOOPBACK",
                kind=reference.kind,
                raw_reference=reference.raw,
            ))
            resources.append(replace(reference,
                outcome="rejected", reason="TARGET_NOT_LOOPBACK"))
            return
        item = FrontierItem(target,
            urlkey(target),
            page_depth,
            resource_depth=reference.depth,
            source_url=reference.source_url,
            kind=reference.kind,
            raw_reference=reference.raw,
            lineage=chain,
        )
        if robots is not None and not robots.allowed(path_and_query(target)):
            logs.append(_log_for(item, None, "", 0, "skipped", "robots_disallowed"))
            resources.append(replace(reference,
                outcome="skipped", reason="robots_disallowed"))
            return
        frontier.add(target,
            item.urlkey,
            page_depth,
            resource_depth=reference.depth,
            source_url=reference.source_url,
            kind=reference.kind,
            raw_reference=reference.raw,
            lineage=chain,
        )
        resources.append(replace(reference, outcome="queued"))

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
                _handle_result(result, item, config, enqueue, enqueue_resource,
                    hits, logs, resources)
        if frontier:
            for item in frontier.drain():
                logs.append(_log_for(item, None, "", 0, "skipped", "max_pages"))
    return CrawlResult(hits=hits,
        logs=logs,
        rejected=rejected,
        resources=_finalize_resources(resources, logs, rejected),
    )


def _handle_result(
    result: FetchResult,
    item: FrontierItem,
    config: CrawlConfig,
    enqueue: Callable[[str, str | None, int, str], None],
    enqueue_resource: Callable[[ResourceRef, int, tuple[str, ...]], None],
    hits: list[FetchResult],
    logs: list[FetchLog],
    resources: list[ResourceRef],
) -> None:
    """分类一条 HTTP 响应，并按需发现重定向、导航链接或静态资源。"""
    location = result.redirect_location
    if location:
        if item.is_resource:
            edge = resolve_reference(location, "redirect", result.url, item.resource_depth)
            if edge.depth > config.resource_depth:
                resources.append(replace(edge, outcome="skipped",
                    reason="resource_depth_limit"))
            else:
                enqueue_resource(edge, item.depth, item.lineage)
        else:
            next_depth = item.depth + 1
            enqueue(urljoin(result.url, location), None, next_depth, result.url)
    if result.mime not in config.allowed_mime:
        logs.append(_log_for(item, result.status, result.mime, result.size, "skipped", "mime_not_allowed"))
        return
    hits.append(result)
    logs.append(_log_for(item, result.status, result.mime, result.size, "archived"))
    if location or result.is_gzip:
        return
    discover = config.resource_depth >= 1
    if result.mime == "text/html" and not item.is_resource:
        if discover:
            # 资源条目的页面深度沿用引用它的文档，资源深度单独计数。
            _discover(discover_html_resources(result.body, result.url, 1),
                enqueue_resource, resources, item.depth, item.lineage)
        for link in extract_links(result.body):
            enqueue(urljoin(result.url, link), None, item.depth + 1, result.url)
    elif discover and result.mime == "text/css":
        # 无论 CSS 由导航还是资源发现得到，都只解析文本、不执行其中任何内容。
        # 导航链接得到的 CSS 视作第 1 层资源，其内部引用仍需 resource_depth>=2。
        child_depth = max(item.resource_depth, 1) + 1
        _discover(discover_css_resources(result.body, result.url, child_depth),
            enqueue_resource, resources, item.depth, (*item.lineage, result.url))


def _discover(
    references: list[ResourceRef],
    enqueue_resource: Callable[[ResourceRef, int, tuple[str, ...]], None],
    resources: list[ResourceRef],
    page_depth: int = 0,
    chain: tuple[str, ...] = (),
) -> None:
    """逐条处理发现项；单条超限或不可解析不中断其余引用。"""
    for reference in references:
        if reference.outcome == "skipped":
            resources.append(reference)
            continue
        enqueue_resource(reference, page_depth, chain)
