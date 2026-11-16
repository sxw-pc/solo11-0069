"""Sitemap 有界采集的端到端策略与安全测试（双回环 origin 夹具）。"""
from __future__ import annotations

from pagefreeze.config import FIFO_MERGED, SITEMAP_PRIORITY
from pagefreeze.frontier import KIND_PAGE, KIND_ROBOTS, KIND_SITEMAP
from sitemap_helpers import make_config, run, urls


def test_same_origin_sitemap_fetched_cross_origin_rejected_preconnect(
    sitemap_site,
) -> None:
    result = run(sitemap_site)
    # 跨 origin 声明在连接前拒绝：第二 origin 从未收到任何请求（零外网连接）。
    assert sitemap_site.requests_b == []
    foreign = next(item for item in result.rejected
        if item.target == f"{sitemap_site.base_b}/foreign.xml")
    assert foreign.reason == "SITEMAP_CROSS_ORIGIN"
    assert foreign.source_kind == KIND_SITEMAP
    assert foreign.source_url == sitemap_site.robots_url
    # 同域 Sitemap 被抓取，类别可与页面、robots 区分，XML 不会被当 HTML 提链接。
    kinds = result.source_breakdown()
    assert kinds[KIND_SITEMAP]["archived"] >= 2  # root urlset + index + child
    assert kinds[KIND_ROBOTS]["archived"] == 1
    assert all(log.mime == "application/xml"
        for log in result.logs if log.kind == KIND_SITEMAP and log.outcome == "archived")


def test_urlset_locs_resolved_deduplicated_and_robots_checked(sitemap_site) -> None:
    result = run(sitemap_site)
    archived = {log.url for log in result.logs if log.outcome == "archived"}
    # 根相对、纯相对（相对 sitemap 所在目录）与绝对 loc 都被正确解析。
    for url in (f"{sitemap_site.base_a}/sm/one",
        f"{sitemap_site.base_a}/sitemaps/sm/two",
        f"{sitemap_site.base_a}/sm/three"):
        assert url in archived
    # 重复 loc 去重：服务器只收到一次 /sm/one。
    assert sitemap_site.requests_a.count("/sm/one") == 1
    # robots 禁止的 loc 有跳过记录且从未发连接。
    blocked = next(log for log in result.logs
        if log.url == f"{sitemap_site.base_a}/private/blocked")
    assert blocked.outcome == "skipped"
    assert blocked.reason == "robots_disallowed"
    assert blocked.parent_sitemap == f"{sitemap_site.base_a}/sitemaps/root.xml"
    assert "/private/blocked" not in sitemap_site.requests_a
    # 来源与 loc 父文件可回查。
    one_log = next(log for log in result.logs
        if log.url == f"{sitemap_site.base_a}/sm/one")
    assert one_log.source_url == f"{sitemap_site.base_a}/sitemaps/root.xml"
    assert one_log.parent_sitemap == f"{sitemap_site.base_a}/sitemaps/root.xml"


def test_sitemap_index_recurses_and_cycle_requested_once(sitemap_site) -> None:
    result = run(sitemap_site, sitemap_max_depth=2)
    archived = {log.url for log in result.logs if log.outcome == "archived"}
    # index -> child -> /sm/four；loop 指回 index，循环被 visited 截断。
    assert f"{sitemap_site.base_a}/sitemaps/child.xml" in archived
    assert f"{sitemap_site.base_a}/sm/four" in archived
    assert sitemap_site.requests_a.count("/sitemaps/index.xml") == 1
    assert sitemap_site.requests_a.count("/sitemaps/loop.xml") == 1
    # 子表里的页面同样带父文件溯源。
    four_log = next(log for log in result.logs
        if log.url == f"{sitemap_site.base_a}/sm/four")
    assert four_log.parent_sitemap == f"{sitemap_site.base_a}/sitemaps/child.xml"


def test_malformed_sitemap_logged_and_other_declarations_continue(
    sitemap_site, monkeypatch,
) -> None:
    from pagefreeze import crawl as crawl_module
    from pagefreeze.fetch import FetchResult
    from pagefreeze.httpmsg import mime_type

    broken_robots = (
        "User-agent: *\nDisallow: /private/\n"
        f"Sitemap: {sitemap_site.base_a}/sitemaps/broken.xml\n"
        f"Sitemap: {sitemap_site.base_a}/sitemaps/root.xml\n"
    )
    original_fetch = crawl_module.fetch_once

    def fake_fetch(url, cfg, limiter):
        if url == sitemap_site.robots_url:
            headers = (("Content-Type", "text/plain; charset=utf-8"),)
            return FetchResult(url=url, status=200, reason="OK", version="HTTP/1.1",
                headers=headers, body=broken_robots.encode("utf-8"),
                mime=mime_type(headers))
        return original_fetch(url, cfg, limiter)

    monkeypatch.setattr(crawl_module, "fetch_once", fake_fetch)
    result = crawl_module.run_crawl(make_config(sitemap_site))
    broken = next(log for log in result.logs
        if log.url == f"{sitemap_site.base_a}/sitemaps/broken.xml")
    assert broken.kind == KIND_SITEMAP
    assert broken.outcome == "skipped"
    assert broken.reason == "sitemap_parse_error"
    # 损坏地图的 loc 不被提取；有效地图与 HTML 链接照常采集。
    assert f"{sitemap_site.base_a}/sm/never" not in urls(result)
    assert f"{sitemap_site.base_a}/sm/one" in urls(result)
    assert f"{sitemap_site.base_a}/nav/a" in urls(result)


def test_strategy_a_prioritizes_sitemap_pages_under_page_budget(sitemap_site) -> None:
    result = run(sitemap_site, max_pages=4, strategy=SITEMAP_PRIORITY)
    page_urls = [log.url for log in result.logs
        if log.kind == KIND_PAGE and log.outcome == "archived"]
    assert page_urls[0] == f"{sitemap_site.base_a}/"
    after_home = page_urls[1:]
    assert len(after_home) == 3
    assert all("/sm/" in url for url in after_home)
    assert not any("/nav/" in url for url in page_urls)


def test_strategy_b_spends_budget_on_bfs_navigation_first(sitemap_site) -> None:
    result = run(sitemap_site, max_pages=4, strategy=FIFO_MERGED)
    page_urls = [log.url for log in result.logs
        if log.kind == KIND_PAGE and log.outcome == "archived"]
    assert page_urls[0] == f"{sitemap_site.base_a}/"
    assert {url for url in page_urls if "/nav/" in url} == {
        f"{sitemap_site.base_a}/nav/a",
        f"{sitemap_site.base_a}/nav/b",
        f"{sitemap_site.base_a}/nav/c",
    }
    assert not any("/sm/" in url for url in page_urls)
    # 同预算下两种入口策略归档的业务页面集合确实不同。
    result_a = run(sitemap_site, max_pages=4, strategy=SITEMAP_PRIORITY)
    pages_a = {log.url for log in result_a.logs
        if log.kind == KIND_PAGE and log.outcome == "archived"}
    pages_b = {log.url for log in result.logs
        if log.kind == KIND_PAGE and log.outcome == "archived"}
    assert pages_a != pages_b
    assert any("/sm/" in url for url in pages_a)
    assert any("/nav/" in url for url in pages_b)


def test_each_loc_requested_at_most_once_and_no_external_connections(
    sitemap_site,
) -> None:
    result = run(sitemap_site)
    assert len(sitemap_site.requests_a) == len(set(sitemap_site.requests_a))
    assert sitemap_site.requests_b == []
    for log in result.logs:
        assert "127.0.0.1" in log.url
    for item in result.rejected:
        assert item.source_url
        assert "127.0.0.1" in item.source_url


def test_report_breakdown_totals_match_logs_and_hits(sitemap_site) -> None:
    result = run(sitemap_site)
    breakdown = result.source_breakdown()
    assert sum(row["attempted"] for row in breakdown.values()) == len(result.logs)
    assert sum(row["archived"] for row in breakdown.values()) == len(result.hits)
    page_sources = result.page_source_breakdown()
    root_sm = page_sources[f"{sitemap_site.base_a}/sitemaps/root.xml"]
    assert root_sm["attempted"] >= 3
    # 种子桶包含首页及其 HTML 链接发现的导航页。
    seed_bucket = page_sources[f"{sitemap_site.base_a}/"]
    assert seed_bucket["archived"] >= 1
    assert f"{sitemap_site.base_a}/" in page_sources


def test_sitemap_recursion_depth_limit_is_enforced(sitemap_site) -> None:
    result = run(sitemap_site, sitemap_max_depth=0)
    depth_skips = [log for log in result.logs if log.reason == "sitemap_depth_limit"]
    assert depth_skips
    assert all(log.kind == KIND_SITEMAP for log in depth_skips)
    skipped_urls = {log.url for log in depth_skips}
    assert f"{sitemap_site.base_a}/sitemaps/child.xml" in skipped_urls
    # 深度 0：子表只记跳过、不抓取，child 内的页面不可达。
    assert not any(log.outcome == "archived" and log.url.endswith("child.xml")
        for log in result.logs)
    assert f"{sitemap_site.base_a}/sm/four" not in urls(result)


def test_sitemap_disabled_when_robots_respect_is_off(sitemap_site) -> None:
    result = run(sitemap_site, respect_robots=False)
    assert all(log.kind != KIND_SITEMAP for log in result.logs)
    assert all(log.kind != KIND_ROBOTS for log in result.logs)
    # 没有 robots 预取，第一个请求就是首页。
    assert sitemap_site.requests_a[0] == "/"


def test_non_xml_sitemap_response_is_skipped_without_link_extraction(
    sitemap_site, monkeypatch,
) -> None:
    from pagefreeze import crawl as crawl_module
    from pagefreeze.fetch import FetchResult
    from pagefreeze.httpmsg import mime_type

    robots_text = (
        "User-agent: *\n"
        f"Sitemap: {sitemap_site.base_a}/sitemaps/not-xml.xml\n"
    )
    original_fetch = crawl_module.fetch_once

    def fake_fetch(url, cfg, limiter):
        if url == sitemap_site.robots_url:
            headers = (("Content-Type", "text/plain; charset=utf-8"),)
            return FetchResult(url=url, status=200, reason="OK", version="HTTP/1.1",
                headers=headers, body=robots_text.encode("utf-8"),
                mime=mime_type(headers))
        return original_fetch(url, cfg, limiter)

    monkeypatch.setattr(crawl_module, "fetch_once", fake_fetch)
    result = crawl_module.run_crawl(make_config(sitemap_site))
    bad = next(log for log in result.logs
        if log.url == f"{sitemap_site.base_a}/sitemaps/not-xml.xml")
    assert bad.kind == KIND_SITEMAP
    assert bad.outcome == "skipped"
    assert bad.reason == "mime_not_allowed"
    # HTML 地图里的 /nav 链接绝不能借道 Sitemap 入口进入 frontier。
    assert not any(log.source_url == f"{sitemap_site.base_a}/sitemaps/not-xml.xml"
        for log in result.logs if log.url.endswith("/nav/a"))
