"""Crawl-delay 站点级执行的集成测试。

构造两个回环 mock origin：A 声明长 Crawl-delay 且有分页，B 声明短间隔且有
分页；配合固定虚拟时钟验证：
- 同 origin 相邻请求不越过各自有效间隔，跨 origin 不互相占用等待槽；
- 重定向每一跳先过回环白名单，并按最终 origin 的 robots 与限速执行；
- robots 禁止路径不建立连接，原因进入 crawl log/台账/report；
- 重复运行的报告保持稳定。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from pagefreeze.clock import VirtualClock
from pagefreeze.config import CrawlConfig
from pagefreeze.crawl import run_crawl

_MIME = ("text/html", "text/css", "text/plain", "image/png")


@dataclass(frozen=True)
class MockRoute:
    status: int
    content_type: str
    body: bytes
    location: str | None = None


def _page(anchor: str, links: list[str]) -> bytes:
    refs = "".join(f'<a href="{link}">{anchor}-{link}</a>' for link in links)
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"></head>"
        f"<body><h1>{anchor}</h1>{refs}</body></html>").encode("utf-8")


class _ScriptedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        routes: dict[str, MockRoute] = self.server.routes  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        route = routes.get(path)
        if route is None:
            route = MockRoute(404, "text/html; charset=utf-8", _page("missing", []))
        self.send_response_only(route.status)
        self.send_header("Content-Type", route.content_type)
        if route.location is not None:
            self.send_header("Location", route.location)
        self.send_header("Content-Length", str(len(route.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if route.body:
            self.wfile.write(route.body)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        pass


class MockOrigin:
    """带可配置路由的回环 HTTP origin。"""

    def __init__(self, routes: dict[str, MockRoute]) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
        self.server.routes = routes  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.origin = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _paginated_routes(robots: str, pages: int, prefix: str,
                      extra: dict[str, MockRoute] | None = None) -> dict[str, MockRoute]:
    routes: dict[str, MockRoute] = {
        "/robots.txt": MockRoute(200, "text/plain; charset=utf-8", robots.encode()),
    }
    links = [f"{prefix}/p{n}" for n in range(1, pages)]
    routes["/"] = MockRoute(200, "text/html; charset=utf-8", _page(f"{prefix}-root", links))
    for number in range(1, pages):
        routes[f"{prefix}/p{number}"] = MockRoute(200, "text/html; charset=utf-8",
            _page(f"{prefix}-p{number}", []))
    if extra:
        routes.update(extra)
    return routes


def _config(seeds: list[str], *, max_pages: int = 50, interval_ms: int = 100,
            respect: bool = True) -> CrawlConfig:
    return CrawlConfig(seed_urls=tuple(seeds), max_depth=3, max_pages=max_pages,
        max_concurrency=4, request_interval_ms=interval_ms, timeout_seconds=5.0,
        max_body_bytes=1_000_000, allowed_mime=tuple(_MIME), respect_robots=respect,
        user_agent="pagefreeze-test/0.1")


@pytest.fixture()
def two_origins():
    slow_robots = "User-agent: *\nCrawl-delay: 1\nDisallow: /private/\n"
    fast_robots = "User-agent: *\nCrawl-delay: 0\nDisallow: /denied/\n"
    slow = MockOrigin(_paginated_routes(slow_robots, 4, "/slow",
        extra={"/private/secret": MockRoute(200, "text/html; charset=utf-8",
            _page("should-never-be-requested", []))}))
    fast = MockOrigin(_paginated_routes(fast_robots, 4, "/fast",
        extra={"/denied/page": MockRoute(200, "text/html; charset=utf-8",
            _page("should-never-be-requested", []))}))
    yield slow, fast
    slow.close()
    fast.close()


def _starts(result, origin: str) -> list[float]:
    return result.origin_stats[origin].request_starts


def test_per_origin_intervals_are_independent(two_origins) -> None:
    slow, fast = two_origins
    clock = VirtualClock()
    result = run_crawl(_config([f"{slow.origin}/", f"{fast.origin}/"]), clock)
    slow_stats = result.origin_stats[slow.origin]
    fast_stats = result.origin_stats[fast.origin]
    assert slow_stats.effective_interval_ms == 1000
    assert slow_stats.robots_delay_ms == 1000
    assert fast_stats.effective_interval_ms == 100
    assert fast_stats.robots_delay_ms == 0
    slow_gaps = [b - a for a, b in zip(_starts(result, slow.origin), _starts(result, slow.origin)[1:])]
    fast_gaps = [b - a for a, b in zip(_starts(result, fast.origin), _starts(result, fast.origin)[1:])]
    assert slow_gaps and all(gap + 1e-9 >= 1.0 for gap in slow_gaps)
    assert fast_gaps and all(gap + 1e-9 >= 0.1 for gap in fast_gaps)
    assert max(fast_gaps) < 1.0
    slow_jobs = sum(1 for log in result.logs
        if log.url.startswith(slow.origin) and log.outcome == "archived")
    fast_jobs = sum(1 for log in result.logs
        if log.url.startswith(fast.origin) and log.outcome == "archived")
    assert slow_jobs == 5
    assert fast_jobs == 5


def test_fast_origin_is_not_blocked_by_slow_origin_delay(two_origins) -> None:
    slow, fast = two_origins
    clock = VirtualClock()
    result = run_crawl(_config([f"{slow.origin}/", f"{fast.origin}/"]), clock)
    slow_starts = _starts(result, slow.origin)
    fast_starts = _starts(result, fast.origin)
    assert fast_starts[-1] <= slow_starts[-1]
    assert fast_starts[-1] < 0.6


def test_robots_disallowed_paths_never_open_connections(two_origins) -> None:
    slow, fast = two_origins
    clock = VirtualClock()
    config = _config([f"{slow.origin}/private/secret", f"{fast.origin}/denied/page"])
    result = run_crawl(config, clock)
    disallowed = [log for log in result.logs if log.reason == "robots_disallowed"]
    assert {log.url for log in disallowed} == {
        f"{slow.origin}/private/secret", f"{fast.origin}/denied/page"}
    assert all(log.outcome == "skipped" and log.status is None for log in disallowed)
    assert result.origin_stats[slow.origin].robots_skipped == 1
    assert result.origin_stats[fast.origin].robots_skipped == 1
    assert result.skip_reasons()["robots_disallowed"] == 2
    archived_paths = {log.url for log in result.logs if log.outcome == "archived"}
    assert f"{slow.origin}/private/secret" not in archived_paths
    assert f"{fast.origin}/denied/page" not in archived_paths


def test_redirect_every_hop_checked_against_allowlist_and_origin_policy() -> None:
    external_robots = "User-agent: *\nCrawl-delay: 2\n"
    external = MockOrigin(_paginated_routes(external_robots, 2, "/ext",
        extra={"/landing": MockRoute(200, "text/html; charset=utf-8",
            _page("landing", []))}))
    try:
        redirect_routes: dict[str, MockRoute] = {
            "/robots.txt": MockRoute(200, "text/plain; charset=utf-8",
                b"User-agent: *\nCrawl-delay: 0\n"),
            "/start": MockRoute(302, "text/html; charset=utf-8",
                _page("r", []), location=f"{external.origin}/landing"),
            "/outside": MockRoute(302, "text/html; charset=utf-8",
                _page("r", []), location="http://203.0.113.9/away"),
        }
        entry = MockOrigin(redirect_routes)
        try:
            clock = VirtualClock()
            result = run_crawl(_config([f"{entry.origin}/start",
                f"{entry.origin}/outside"]), clock)
            assert any(item.target == "http://203.0.113.9/away"
                and item.reason == "TARGET_NOT_LOOPBACK" for item in result.rejected)
            cross_origin = result.origin_stats[external.origin]
            assert cross_origin.request_count >= 1
            assert cross_origin.effective_interval_ms == 2000
            assert any(log.url == f"{external.origin}/landing"
                and log.outcome == "archived" for log in result.logs)
            assert any(item.target == "http://203.0.113.9/away"
                and item.source_url == f"{entry.origin}/outside"
                for item in result.rejected)
        finally:
            entry.close()
    finally:
        external.close()


def test_redirect_within_allowed_origin_uses_destination_rate_limit() -> None:
    destination_robots = "User-agent: *\nCrawl-delay: 1\n"
    destination = MockOrigin(_paginated_routes(destination_robots, 2, "/d"))
    redirect_routes = {
        "/robots.txt": MockRoute(200, "text/plain; charset=utf-8",
            b"User-agent: *\nCrawl-delay: 0\n"),
        "/bounce": MockRoute(302, "text/html; charset=utf-8", _page("r", []),
            location=f"{destination.origin}/d/p1"),
    }
    entry = MockOrigin(redirect_routes)
    try:
        clock = VirtualClock()
        result = run_crawl(_config([f"{entry.origin}/bounce"]), clock)
        stats = result.origin_stats[destination.origin]
        assert stats.effective_interval_ms == 1000
        assert stats.request_count >= 2
        gaps = [b - a for a, b in zip(stats.request_starts, stats.request_starts[1:])]
        assert all(gap + 1e-9 >= 1.0 for gap in gaps)
    finally:
        entry.close()
        destination.close()


def test_malformed_crawl_delay_does_not_break_other_origin() -> None:
    broken_robots = ("User-agent: *\nCrawl-delay: 1.5\nCrawl-delay: nah\n"
        "Disallow: /no/\n")
    broken = MockOrigin(_paginated_routes(broken_robots, 2, "/b"))
    healthy = MockOrigin(_paginated_routes("User-agent: *\nCrawl-delay: 0\n", 2, "/h"))
    try:
        clock = VirtualClock()
        result = run_crawl(_config([f"{broken.origin}/", f"{healthy.origin}/"]), clock)
        broken_stats = result.origin_stats[broken.origin]
        assert broken_stats.robots_delay_ms is None
        assert broken_stats.effective_interval_ms == 100
        warning_kinds = {item.split(":", 1)[0] for item in broken_stats.robots_warnings}
        assert warning_kinds == {"crawl_delay_decimal_unsupported",
            "crawl_delay_invalid"}
        assert result.origin_stats[healthy.origin].request_count == 3
        healthy_gaps = [b - a for a, b in zip(_starts(result, healthy.origin),
            _starts(result, healthy.origin)[1:])]
        assert all(gap + 1e-9 >= 0.1 for gap in healthy_gaps)
    finally:
        broken.close()
        healthy.close()


def test_robots_fetch_failure_fails_open_without_breaking_other_origin() -> None:
    flaky_routes: dict[str, MockRoute] = {
        "/robots.txt": MockRoute(500, "text/plain; charset=utf-8", b"boom"),
        "/": MockRoute(200, "text/html; charset=utf-8", _page("flaky-root", [])),
    }
    flaky = MockOrigin(flaky_routes)
    healthy = MockOrigin(_paginated_routes("User-agent: *\nCrawl-delay: 0\n", 2, "/h"))
    try:
        clock = VirtualClock()
        result = run_crawl(_config([f"{flaky.origin}/", f"{healthy.origin}/"]), clock)
        assert result.origin_stats[flaky.origin].request_count >= 1
        assert any(log.url.endswith("/robots.txt") and log.outcome == "archived"
            for log in result.logs if log.url.startswith(flaky.origin))
        assert any(log.url == f"{flaky.origin}/" and log.outcome == "archived"
            for log in result.logs)
        assert result.origin_stats[healthy.origin].request_count == 3
    finally:
        flaky.close()
        healthy.close()


def test_max_pages_is_shared_budget_but_never_violates_per_origin_interval(two_origins
) -> None:
    slow, fast = two_origins
    clock = VirtualClock()
    result = run_crawl(_config([f"{slow.origin}/", f"{fast.origin}/"], max_pages=4), clock)
    page_attempts = sum(1 for log in result.logs if log.outcome == "archived"
        and not log.url.endswith("/robots.txt"))
    assert page_attempts == 4
    for origin in (slow.origin, fast.origin):
        starts = _starts(result, origin)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        minimum = 1.0 if origin == slow.origin else 0.1
        assert all(gap + 1e-9 >= minimum for gap in gaps)


def test_same_origin_spacing_holds_under_real_threads_and_clock(two_origins) -> None:
    slow, fast = two_origins
    result = run_crawl(_config([f"{slow.origin}/", f"{fast.origin}/"],
        max_pages=10, interval_ms=50))
    slow_stats = result.origin_stats[slow.origin]
    assert slow_stats.effective_interval_ms == 1000
    gaps = [b - a for a, b in zip(slow_stats.request_starts,
        slow_stats.request_starts[1:])]
    assert gaps and all(gap >= 0.99 for gap in gaps)
    fast_stats = result.origin_stats[fast.origin]
    fast_gaps = [b - a for a, b in zip(fast_stats.request_starts,
        fast_stats.request_starts[1:])]
    assert fast_gaps and all(gap >= 0.045 for gap in fast_gaps)
    assert fast_stats.request_count >= 1


def test_repeated_runs_produce_stable_reports(two_origins, tmp_path: Path) -> None:
    from pagefreeze.report import build_summary, write_reports
    from pagefreeze.config import paths_for, ArchiveConfig
    import uuid
    slow, fast = two_origins
    config = _config([f"{slow.origin}/", f"{fast.origin}/"])
    summaries = []
    for _ in range(2):
        clock = VirtualClock()
        result = run_crawl(config, clock)
        archive = ArchiveConfig(archive_dir="archive", segment_size_bytes=16384,
            digest_algorithm="sha256", index_filename="index.cdx",
            ledger_filename="ledger.sqlite3", report_dir="reports",
            uuid_namespace=uuid.UUID("6f1c5a9e-2d47-5b83-8a10-3c4d5e6f7a8b"))
        from types import SimpleNamespace
        settings = SimpleNamespace(digest="sha256:test", archive=archive)
        out = tmp_path / f"run-{len(summaries)}"
        paths = paths_for(settings, out)
        paths.report_dir.mkdir(parents=True, exist_ok=True)
        from pagefreeze.verify import VerifyResult
        verify = VerifyResult(status="pass", stats={}, checks=[], issues=[])
        summary = build_summary(run_id="run-test", timestamp="20240115000000",
            config_digest="sha256:test", crawl=result,
            write=SimpleNamespace(segments=[], records=[], requests=0, responses=0,
                revisits=0, raw_bytes=0),
            verify=verify, paths=paths)
        write_reports(paths, summary, live_crawl=result)
        summaries.append((paths.report_json.read_text("utf-8"),
            paths.report_md.read_text("utf-8"), result))
    first_json, first_md, result = summaries[0]
    second_json, second_md, _ = summaries[1]
    assert first_json == second_json
    assert slow.origin in first_md and fast.origin in first_md
    assert "起始时间(相对秒)" in first_md
    assert first_md == second_md
    origins = {item["origin"]: item for item in result.origin_summary()}
    assert origins[slow.origin]["effective_interval_ms"] == 1000
    assert origins[fast.origin]["robots_skipped"] == 0
