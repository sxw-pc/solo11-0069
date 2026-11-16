"""端到端 Sitemap 采集：双 origin、入口策略 A/B、预算、去重、循环与零外网。"""
from __future__ import annotations

from pagefreeze.config import CrawlConfig, SITEMAP_PRIORITY
from pagefreeze.crawl import run_crawl
from sitemap_fixtures import SitemapSite

__all__ = ["make_config", "urls", "run", "SitemapSite"]


def make_config(site: SitemapSite, *, max_pages: int = 40,
                strategy: str = SITEMAP_PRIORITY,
                sitemap_max_depth: int = 2,
                respect_robots: bool = True) -> CrawlConfig:
    return CrawlConfig(
        seed_urls=(f"{site.base_a}/",),
        max_depth=3,
        max_pages=max_pages,
        max_concurrency=2,
        request_interval_ms=0,
        timeout_seconds=5.0,
        max_body_bytes=1_000_000,
        allowed_mime=("text/html", "text/css", "text/plain",
            "image/png", "application/xml"),
        respect_robots=respect_robots,
        user_agent="pagefreeze-test/0.1",
        sitemap_entry_strategy=strategy,
        sitemap_max_depth=sitemap_max_depth,
    )


def urls(result) -> set[str]:
    return {log.url for log in result.logs}


def run(site: SitemapSite, **overrides):
    return run_crawl(make_config(site, **overrides))
