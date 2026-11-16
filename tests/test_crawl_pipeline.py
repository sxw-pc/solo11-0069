"""集成测试：有界抓取、场景覆盖、gzip 链接、台账、golden 与确定性。"""
from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path
from pagefreeze.cdx import read_index
from pagefreeze.config import load_settings
from pagefreeze.crawl import LINK_DISCOVERY_SKIP, run_crawl
from pagefreeze.gzipbody import MAX_DECODED_BYTES
from pagefreeze.httpmsg import header_value, parse_response
from pagefreeze.mocksite.gzip_fixtures import oversize_body
from pagefreeze.pipeline import PipelineResult, execute_crawl, execute_demo
from pagefreeze.digest import sha256_file
from pagefreeze.util import parse_clock
from conftest import FIXED_NOW, ROOT


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


def _gzip_settings(site: str, path: str, *, max_pages: int = 8, max_depth: int = 2,
    seeds: tuple[str, ...] = ()):
    base = load_settings(ROOT / "config")
    crawl = replace(base.crawl,
        seed_urls=(site + path, *seeds),
        max_pages=max_pages,
        max_depth=max_depth,
        respect_robots=False,
    )
    return replace(base, crawl=crawl)


def _hit_urls(value) -> set[str]:
    crawl = value.crawl if isinstance(value, PipelineResult) else value
    return {hit.url for hit in crawl.hits}


def test_complete_gzip_page_discovers_all_links_in_order(gzip_site: str, tmp_path: Path
) -> None:
    result = execute_crawl(_gzip_settings(gzip_site, "/gz/complete.html"), tmp_path,
        parse_clock(FIXED_NOW))
    urls = _hit_urls(result)
    assert {gzip_site + "/guide", gzip_site + "/appendix"} <= urls
    assert gzip_site + "/gz/%E4%B8%AD%E6%96%87/%E9%A1%B5%E9%9D%A2.html" in urls
    assert result.crawl.link_skips == []
    # 同一 href 出现两次（含 fragment）只入队并归档一次。
    assert sum(1 for hit in result.crawl.hits if hit.url == gzip_site + "/guide") == 1
    assert result.verify.status == "pass"


def test_corrupt_gzip_contributes_zero_links_but_stays_replayable(
    gzip_site: str, tmp_path: Path
) -> None:
    url = gzip_site + "/gz/bad-crc.html"
    result = execute_crawl(_gzip_settings(gzip_site, "/gz/bad-crc.html"), tmp_path,
        parse_clock(FIXED_NOW))
    assert _hit_urls(result) == {url}
    assert [skip.detail for skip in result.crawl.link_skips] == ["GZIP_CRC_MISMATCH"]
    assert result.crawl.link_skips[0].reason == LINK_DISCOVERY_SKIP
    assert result.crawl.skipped == 0
    rows = read_index(result.paths.index_path)
    row = next(item for item in rows if item.url == url)
    raw = zlib.decompress((result.paths.archive_dir / row.filename).read_bytes(),
        16 + zlib.MAX_WBITS)
    from pagefreeze.warc import parse_records
    record = parse_records(raw[row.offset:row.offset + row.length])[0]
    message = parse_response(record.block)
    assert row.status == 200
    assert header_value(message.headers, "Content-Encoding") == "gzip"
    assert message.body.startswith(b"\x1f\x8b")
    assert b"/guide" not in message.body
    assert result.verify.status == "pass"


def test_truncated_and_oversized_gzip_are_skipped_whole(gzip_site: str) -> None:
    for path, detail in (("/gz/truncated.html", "GZIP_TRUNCATED"),
                         ("/gz/bad-isize.html", "GZIP_ISIZE_MISMATCH"),
                         ("/gz/oversize.html", "GZIP_TOO_LARGE")):
        result = run_crawl(_gzip_settings(gzip_site, path).crawl)
        assert _hit_urls(result) == {gzip_site + path}
        assert [skip.detail for skip in result.link_skips] == [detail]
        assert result.skipped == 0
    assert len(oversize_body()) < load_settings(ROOT / "config").crawl.max_body_bytes
    assert MAX_DECODED_BYTES == 1024 * 1024


def test_non_html_gzip_and_plain_html_are_unchanged(gzip_site: str) -> None:
    css = run_crawl(_gzip_settings(gzip_site, "/gz/style.css", max_depth=1).crawl)
    assert _hit_urls(css) == {gzip_site + "/gz/style.css"}
    assert css.link_skips == []
    plain = run_crawl(_gzip_settings(gzip_site, "/gz/plain.html").crawl)
    assert plain.link_skips == []
    assert {gzip_site + "/guide", gzip_site + "/appendix"} <= _hit_urls(plain)


def test_corrupt_prefix_does_not_consume_page_budget(gzip_site: str) -> None:
    """坏 gzip 不把前缀链接放进 frontier，剩余预算留给正常站点项。"""
    settings = _gzip_settings(gzip_site, "/gz/bad-crc.html", max_pages=3,
        seeds=(gzip_site + "/gz/after.html",))
    settings = replace(settings, crawl=replace(settings.crawl, max_concurrency=1))
    crawl = run_crawl(settings.crawl)
    urls = [log.url for log in crawl.logs]
    assert urls == [gzip_site + "/gz/bad-crc.html", gzip_site + "/gz/after.html"]
    assert _hit_urls(crawl) == set(urls)
    assert crawl.skipped == 0
    assert all("/guide" not in url for url in urls)
