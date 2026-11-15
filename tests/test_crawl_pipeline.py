"""集成测试：可选资源发现的三种深度、审计一致性，以及原有 demo 闭环。"""
from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import FIXED_NOW, ROOT
from pagefreeze.cdx import read_index
from pagefreeze.config import load_settings
from pagefreeze.digest import sha256_file
from pagefreeze.errors import ConfigError
from pagefreeze.fetch import fetch_once
from pagefreeze.pipeline import PipelineResult, execute_crawl, execute_demo
from pagefreeze.ratelimit import RateLimiter
from pagefreeze.util import parse_clock
from pagefreeze.warc import parse_records


def test_demo_scenarios_and_artifacts(demo_result: PipelineResult) -> None:
    crawl = demo_result.crawl
    write = demo_result.write
    assert (len(write.records), write.requests, write.responses, write.revisits) == (33, 16, 15, 1)
    assert len(write.segments) == 2
    assert len(crawl.rejected) == 1
    assert crawl.rejected[0].target == "http://203.0.113.7/outside"
    reasons = crawl.skip_reasons()
    assert reasons["robots_disallowed"] == 1
    assert reasons["body_too_large"] == 1
    assert reasons["timeout"] == 1
    assert any("private/secret" in log.url for log in crawl.logs
               if log.reason == "robots_disallowed")
    assert any("/slow?slow=1" in log.url for log in crawl.logs if log.reason == "timeout")
    rows = read_index(demo_result.paths.index_path)
    assert len(rows) == 16
    statuses = {row.url: row.status for row in rows}
    assert statuses["http://127.0.0.1:18080/status/404"] == 404
    assert statuses["http://127.0.0.1:18080/status/500"] == 500
    assert demo_result.verify.status == "pass"
    by_url = {row.url: row for row in rows}
    gzip_row = by_url["http://127.0.0.1:18080/gzip"]
    compressed = (demo_result.paths.archive_dir / gzip_row.filename).read_bytes()
    raw = zlib.decompress(compressed, 16 + zlib.MAX_WBITS)
    from pagefreeze.httpmsg import header_value, parse_response
    from pagefreeze.warc import parse_records
    record = parse_records(raw[gzip_row.offset:gzip_row.offset + gzip_row.length])[0]
    message = parse_response(record.block)
    assert header_value(message.headers, "Content-Encoding") == "gzip"
    assert message.body.startswith(b"\x1f\x8b")
    zh_row = by_url["http://127.0.0.1:18080/zh/"]
    raw_zh = zlib.decompress(
        (demo_result.paths.archive_dir / zh_row.filename).read_bytes(), 16 + zlib.MAX_WBITS)
    zh_record = parse_records(raw_zh[zh_row.offset:zh_row.offset + zh_row.length])[0]
    assert "中文编码样例" in parse_response(zh_record.block).body.decode("utf-8")
    for path in (demo_result.paths.index_path, demo_result.paths.verify_json,
        demo_result.paths.verify_md, demo_result.paths.report_json,
        demo_result.paths.report_md, demo_result.paths.ledger_path,
    ):
        assert path.is_file()
    report = json.loads(demo_result.paths.report_json.read_text(encoding="utf-8"))
    assert report["archive"]["dedup_hits"] == 1
    assert report["crawl"]["rejected_targets"] == 1
    assert report["verify"]["status"] == "pass"
    assert str(demo_result.paths.root) not in demo_result.paths.report_md.read_text("utf-8")
    connection = sqlite3.connect(demo_result.paths.ledger_path)
    try:
        tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"runs", "fetches", "rejected_targets", "records", "verify_results"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM rejected_targets").fetchone()[0] == 1
    finally:
        connection.close()

def test_two_fixed_clock_runs_are_byte_identical(demo_result: PipelineResult, tmp_path: Path
) -> None:
    settings = load_settings(ROOT / "config")
    second = execute_demo(settings, tmp_path / "second", parse_clock(FIXED_NOW))
    first_segments = sorted(demo_result.paths.archive_dir.glob("seg-*.warc.gz"))
    second_segments = sorted(second.paths.archive_dir.glob("seg-*.warc.gz"))
    assert [path.name for path in first_segments] == [path.name for path in second_segments]
    for left, right in zip(first_segments, second_segments):
        assert left.read_bytes() == right.read_bytes()
    assert demo_result.paths.index_path.read_bytes() == second.paths.index_path.read_bytes()
    assert demo_result.paths.report_json.read_bytes() == second.paths.report_json.read_bytes()


def test_golden_summary(demo_result: PipelineResult) -> None:
    golden_path = ROOT / "fixtures" / "expected" / "golden.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert golden["now"] == FIXED_NOW
    assert golden["records"] == len(demo_result.write.records)
    assert golden["responses"] == demo_result.write.responses
    assert golden["revisits"] == demo_result.write.revisits
    assert golden["rejected"] == len(demo_result.crawl.rejected)
    assert golden["segments"] == len(demo_result.write.segments)
    assert golden["verify"] == demo_result.verify.status
    assert golden["index_sha256"] == sha256_file(demo_result.paths.index_path)
    for name, digest in golden["segment_sha256"].items():
        assert digest == sha256_file(demo_result.paths.archive_dir / name)



def _settings(resource_site: str, resource_depth: int, max_pages: int = 40,
    respect_robots: bool = True,
):
    base = load_settings(ROOT / "config")
    crawl = replace(base.crawl,
        seed_urls=(resource_site + "/res/",),
        resource_depth=resource_depth,
        max_pages=max_pages,
        max_depth=3,
        respect_robots=respect_robots,
    )
    return replace(base, crawl=crawl)


def _resource_logs(crawl, outcome=None):
    return [log for log in crawl.logs
            if log.resource_depth and (outcome is None or log.outcome == outcome)]


def _warc_targets(paths) -> list[str]:
    """按 WARC 顺序返回全部 response/revisit 的 Target-URI。"""
    targets: list[str] = []
    for segment in sorted(paths.archive_dir.glob("seg-*.warc.gz")):
        raw = zlib.decompress(segment.read_bytes(), 16 + zlib.MAX_WBITS)
        for record in parse_records(raw, segment.name):
            if record.record_type in ("response", "revisit"):
                targets.append(record.header("WARC-Target-URI") or "")
    return targets


def _records_by_url(paths) -> dict[str, list]:
    result: dict[str, list] = {}
    for segment in sorted(paths.archive_dir.glob("seg-*.warc.gz")):
        raw = zlib.decompress(segment.read_bytes(), 16 + zlib.MAX_WBITS)
        for record in parse_records(raw, segment.name):
            if record.record_type in ("response", "revisit"):
                result.setdefault(record.header("WARC-Target-URI") or "", []).append(record)
    return result


def test_depth_zero_keeps_only_navigation_baseline(resource_site: str, tmp_path: Path) -> None:
    settings = _settings(resource_site, resource_depth=0)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    assert crawl.resources == []
    assert crawl.resource_outcomes() == {
        "archived": 0, "duplicate": 0, "error": 0, "rejected": 0, "skipped": 0}
    assert _resource_logs(crawl) == []
    # 只有导航 BFS 抓到的两个 HTML 文档 + robots。
    html_hits = [hit for hit in crawl.hits if hit.mime == "text/html"]
    assert len(html_hits) == 2
    assert all("/res/style.css" not in hit.url for hit in crawl.hits)
    assert result.verify.status == "pass"


def test_depth_one_fetches_only_direct_html_dependencies(
    resource_site: str, tmp_path: Path
) -> None:
    settings = _settings(resource_site, resource_depth=1)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    fetched = {hit.url for hit in crawl.hits}
    assert resource_site + "/res/style.css" in fetched
    assert resource_site + "/res/theme/alt.css" in fetched
    assert resource_site + "/res/img/logo.png" in fetched
    assert resource_site + "/res/img/logo-2x.png" in fetched
    # 直接闭包之外的 CSS 依赖一律不抓取，但边仍被记录。
    assert resource_site + "/res/img/bg.png" not in fetched
    assert resource_site + "/res/nested/import.css" not in fetched
    indirect = [ref for ref in crawl.resources
                if ref.kind in ("css-url", "css-import") and ref.outcome != "archived"]
    assert indirect
    assert all(ref.outcome == "skipped" for ref in indirect)
    limited = [ref for ref in indirect if ref.reason != "data_uri"]
    assert limited and all(ref.reason == "resource_depth_limit" for ref in limited)
    # 直接依赖的 CSS 内部引用只留痕，不因它们触发新的请求。
    assert not any(ref.outcome == "archived" and ref.kind in ("css-url", "css-import")
                   for ref in crawl.resources)
    # 每个已抓资源都能回查到引用它的页面或 CSS。
    for url in (resource_site + "/res/style.css", resource_site + "/res/img/logo.png"):
        assert crawl.references_for(url)
        assert any(ref.outcome == "archived" for ref in crawl.references_for(url))


def test_depth_two_expands_css_and_dedupes_shared_targets(
    resource_site: str, tmp_path: Path
) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    fetched = {hit.url for hit in crawl.hits}
    assert resource_site + "/res/img/bg.png" in fetched
    assert resource_site + "/res/nested/import.css" in fetched
    assert resource_site + "/res/img/accent.png" in fetched
    # 共享资源被多个父资源引用，但只抓取、只归档一次。
    shared = resource_site + "/res/img/shared.png"
    request_logs = [log for log in crawl.logs if log.url == shared]
    assert len(request_logs) == 1
    edges = [ref for ref in crawl.resources if ref.target == shared]
    assert len(edges) >= 2
    assert sum(1 for edge in edges if edge.outcome == "archived") == 1
    assert sum(1 for edge in edges if edge.duplicate) == len(edges) - 1
    records = _records_by_url(result.paths)
    assert len(records[shared]) == 1
    rows = read_index(result.paths.index_path)
    assert sum(1 for row in rows if row.url == shared) == 1


def test_depth_two_cycle_and_depth_cutoff_are_explicit(
    resource_site: str, tmp_path: Path
) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    cutoff = [ref for ref in crawl.resources if ref.reason == "resource_depth_limit"]
    assert cutoff, "expected references beyond resource_depth=2 to be recorded"
    assert all(ref.outcome == "skipped" for ref in cutoff)
    assert all(ref.depth == 3 for ref in cutoff)
    # 深度 2 时 cycle-a 是合法导入目标，只抓一次；环在 depth 3 才闭合。
    cycle_target = resource_site + "/res/theme/cycle-a.css"
    assert len([log for log in crawl.logs if log.url == cycle_target]) == 1
    assert not any(ref.cycle for ref in crawl.resources)


def test_depth_three_reports_import_cycle_without_refetching(
    resource_site: str, tmp_path: Path
) -> None:
    settings = _settings(resource_site, resource_depth=3)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    cycles = crawl.cycles()
    assert len(cycles) == 1
    edge = cycles[0]
    assert edge.reason == "import_cycle"
    assert edge.outcome == "skipped"
    assert edge.target == resource_site + "/res/theme/cycle-a.css"
    assert edge.source_url == resource_site + "/res/theme/cycle-b.css"
    logs = [log for log in crawl.logs if log.url == edge.target]
    assert len(logs) == 1, "cyclic import must not be requested twice"
    assert logs[0].outcome == "archived"


def test_external_css_rejected_before_connection(resource_site: str, tmp_path: Path) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    external = [item for item in crawl.rejected if item.target.startswith("http://203.0.113.7/")]
    assert {item.target for item in external} == {
        "http://203.0.113.7/ext.css", "http://203.0.113.7/outside"}
    ext_css = next(item for item in external if item.kind == "stylesheet")
    assert ext_css.source_url == resource_site + "/res/"
    assert ext_css.raw_reference == "http://203.0.113.7/ext.css"
    assert not any("203.0.113.7" in log.url for log in crawl.logs)
    edge = next(ref for ref in crawl.resources if ref.target == ext_css.target)
    assert edge.outcome == "rejected"


def test_data_uri_robots_and_mime_limit_are_recorded(
    resource_site: str, tmp_path: Path
) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    data_uris = [ref for ref in crawl.resources if ref.reason == "data_uri"]
    assert data_uris and all(ref.outcome == "skipped" and ref.target is None
                             for ref in data_uris)
    blocked = [ref for ref in crawl.resources
               if ref.target == resource_site + "/private/blocked.png"]
    assert blocked and blocked[0].reason == "robots_disallowed"
    huge = [ref for ref in crawl.resources
            if ref.target == resource_site + "/res/img/huge.png"]
    assert huge and huge[0].outcome == "skipped" and huge[0].reason == "body_too_large"


def test_resource_edges_and_counts_are_consistent(resource_site: str, tmp_path: Path) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    summary = result.summary["crawl"]
    assert summary["resource_edges"] == len(crawl.resources)
    assert sum(summary["resource_outcomes"].values()) == len(crawl.resources)
    archived_edges = [ref for ref in crawl.resources if ref.outcome == "archived"]
    assert all(ref.target is not None for ref in archived_edges)
    archived_targets = {ref.target for ref in archived_edges}
    assert archived_targets <= {hit.url for hit in crawl.hits}
    # 每条 archived 边对应唯一一次请求；duplicate 边不额外产生请求。
    for target in archived_targets:
        assert len([log for log in crawl.logs if log.url == target]) == 1
    records = _records_by_url(result.paths)
    for target in archived_targets:
        assert len(records[target]) == 1
    assert result.verify.status == "pass"
    # WARC 中每个已归档资源恰好出现一次，且 WARC 目标集合与抓取命中一致。
    warc_targets = _warc_targets(result.paths)
    for target in archived_targets:
        assert warc_targets.count(target) == 1
    assert set(warc_targets) == {hit.url for hit in crawl.hits}


def test_ledger_records_resource_edges(resource_site: str, tmp_path: Path) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    connection = sqlite3.connect(result.paths.ledger_path)
    try:
        rows = connection.execute(
            "SELECT source_url, raw_reference, target, kind, resource_depth, "
            "outcome, reason, duplicate, cycle FROM resource_refs").fetchall()
    finally:
        connection.close()
    assert len(rows) == len(result.crawl.resources)
    assert any(row[6] == "resource_depth_limit" for row in rows)
    assert any(row[3] == "srcset" for row in rows)
    assert all(row[0] for row in rows)


def test_fixed_clock_runs_are_byte_identical(resource_site: str, tmp_path: Path) -> None:
    settings = _settings(resource_site, resource_depth=2)
    first = execute_crawl(settings, tmp_path / "a", parse_clock(FIXED_NOW))
    second = execute_crawl(settings, tmp_path / "b", parse_clock(FIXED_NOW))
    segments_a = sorted(first.paths.archive_dir.glob("seg-*.warc.gz"))
    segments_b = sorted(second.paths.archive_dir.glob("seg-*.warc.gz"))
    assert [path.name for path in segments_a] == [path.name for path in segments_b]
    for left, right in zip(segments_a, segments_b):
        assert left.read_bytes() == right.read_bytes()
    assert first.paths.index_path.read_bytes() == second.paths.index_path.read_bytes()
    assert first.paths.report_json.read_bytes() == second.paths.report_json.read_bytes()


def test_max_pages_limits_resources_with_explicit_skip(
    resource_site: str, tmp_path: Path
) -> None:
    # robots 预取也占用预算；关闭 robots 后 6 页预算全部用于页面与资源。
    settings = _settings(resource_site, resource_depth=2, max_pages=6,
        respect_robots=False)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    assert len(crawl.hits) <= 6
    assert "max_pages" in crawl.skip_reasons()
    unfinished = [ref for ref in crawl.resources
                  if ref.outcome == "skipped" and ref.reason in ("not_fetched", "max_pages")]
    assert unfinished
    # 页数上限不能变成一次无界抓取：请求总数恰好被预算截断。
    assert sum(1 for log in crawl.logs if log.outcome == "archived") <= 6
    assert len(crawl.hits) <= 6


def test_resource_depth_config_validation(resource_site: str, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    source_crawl = (ROOT / "config" / "crawl.json").read_text(encoding="utf-8")
    source_archive = (ROOT / "config" / "archive.json").read_text(encoding="utf-8")
    (config / "archive.json").write_text(source_archive, encoding="utf-8")
    for value, code in ((99, "CFG_RESOURCE_DEPTH_RANGE"),
                        ("2", "CFG_TYPE"), (-1, "CFG_RESOURCE_DEPTH_RANGE")):
        if isinstance(value, str):
            rendered = json.dumps(value)
        else:
            rendered = str(value)
        payload = source_crawl.replace('"resource_depth": 2',
            f'"resource_depth": {rendered}')
        (config / "crawl.json").write_text(payload, encoding="utf-8")
        with pytest.raises(ConfigError) as caught:
            load_settings(config)
        assert caught.value.code == code
    (config / "crawl.json").write_text(source_crawl, encoding="utf-8")
    assert load_settings(config).crawl.resource_depth == 2


def test_offline_run_never_connects_outside_loopback(resource_site: str) -> None:
    limiter = RateLimiter(0)
    config = load_settings(ROOT / "config").crawl
    for url in ("http://203.0.113.7/ext.css", "http://example.invalid/a.css"):
        with pytest.raises(Exception) as caught:
            fetch_once(url, config, limiter)
        assert getattr(caught.value, "code", "") == "TARGET_NOT_LOOPBACK"


def test_archived_resource_urls_are_traceable_to_source(
    resource_site: str, tmp_path: Path
) -> None:
    settings = _settings(resource_site, resource_depth=2)
    result = execute_crawl(settings, tmp_path, parse_clock(FIXED_NOW))
    crawl = result.crawl
    for reference in crawl.resources:
        if reference.outcome != "archived":
            continue
        assert reference.target and reference.source_url and reference.raw
        assert reference.depth in (1, 2)
        source_log = next(log for log in crawl.logs if log.url == reference.target)
        assert source_log.source_url == reference.source_url
        assert source_log.kind == reference.kind
        assert source_log.resource_depth == reference.depth
    # 每个已归档的 CSS 内部引用都以该 CSS 自身（而非 HTML 页面）为来源。
    html_docs = {hit.url for hit in crawl.hits if hit.mime == "text/html"}
    css_sources = {ref.source_url for ref in crawl.resources if ref.depth == 2}
    assert any(source.endswith(".css") for source in css_sources)
    assert not css_sources & html_docs
