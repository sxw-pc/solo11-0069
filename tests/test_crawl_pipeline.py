"""集成测试：有界抓取、场景覆盖、台账、golden 与确定性。"""
from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from pagefreeze.cdx import read_index
from pagefreeze.config import load_settings
from pagefreeze.pipeline import PipelineResult, execute_demo
from pagefreeze.digest import sha256_file
from pagefreeze.util import parse_clock
from conftest import FIXED_NOW, ROOT


def test_demo_scenarios_and_artifacts(demo_result: PipelineResult) -> None:
    crawl = demo_result.crawl
    write = demo_result.write
    assert (len(write.records), write.requests, write.responses, write.revisits) == (41, 20, 19, 1)
    assert len(write.segments) == 2
    assert len(crawl.rejected) == 2
    rejected_targets = {item.target: item for item in crawl.rejected}
    assert rejected_targets["http://203.0.113.7/outside"].reason == "TARGET_NOT_LOOPBACK"
    assert rejected_targets["http://203.0.113.7/sitemap-offsite.xml"].reason == "SITEMAP_CROSS_ORIGIN"
    assert rejected_targets["http://203.0.113.7/sitemap-offsite.xml"].source_kind == "sitemap"
    reasons = crawl.skip_reasons()
    assert reasons["robots_disallowed"] == 1
    assert reasons["body_too_large"] == 1
    assert reasons["timeout"] == 1
    assert any("private/secret" in log.url for log in crawl.logs
               if log.reason == "robots_disallowed")
    assert any("/slow?slow=1" in log.url for log in crawl.logs if log.reason == "timeout")
    # Sitemap 声明的页面无 HTML 链接可达，且来源/loc 父文件可回查。
    sm_logs = [log for log in crawl.logs if log.parent_sitemap is not None]
    assert {log.url for log in sm_logs} >= {
        "http://127.0.0.1:18080/sm/alpha",
        "http://127.0.0.1:18080/sm/beta",
        "http://127.0.0.1:18080/sm/gamma",
    }
    assert all(log.source_url == "http://127.0.0.1:18080/sitemap.xml" for log in sm_logs)
    assert all(log.kind == "page" for log in sm_logs)
    sitemap_logs = [log for log in crawl.logs if log.kind == "sitemap"]
    assert [log.url for log in sitemap_logs] == ["http://127.0.0.1:18080/sitemap.xml"]
    assert sitemap_logs[0].source_url == "http://127.0.0.1:18080/robots.txt"
    rows = read_index(demo_result.paths.index_path)
    assert len(rows) == 20
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
    assert report["crawl"]["rejected_targets"] == 2
    assert report["crawl"]["sitemap_entry_strategy"] == "sitemap_priority"
    assert report["crawl"]["request_kinds"]["sitemap"]["archived"] == 1
    assert report["crawl"]["request_kinds"]["robots"]["archived"] == 1
    # 报告分类归档数之和必须与 WARC request 记录数（每个命中 URL 一条请求）相符。
    archived_by_kind = sum(
        row["archived"] for row in report["crawl"]["request_kinds"].values())
    assert archived_by_kind == report["archive"]["requests"] == write.requests
    assert (sum(row["attempted"] for row in report["crawl"]["request_kinds"].values())
        == report["crawl"]["attempted"])
    assert report["verify"]["status"] == "pass"
    assert str(demo_result.paths.root) not in demo_result.paths.report_md.read_text("utf-8")
    connection = sqlite3.connect(demo_result.paths.ledger_path)
    try:
        tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"runs", "fetches", "rejected_targets", "records", "verify_results"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM rejected_targets").fetchone()[0] == 2
        kind_rows = dict(connection.execute("SELECT kind, COUNT(*) FROM fetches GROUP BY kind"))
        assert kind_rows["sitemap"] == 1
        assert kind_rows["robots"] == 1
        sm_rows = connection.execute(
            "SELECT url, source_url, parent_sitemap FROM fetches "
            "WHERE parent_sitemap IS NOT NULL ORDER BY url").fetchall()
        assert sm_rows[0][1] == "http://127.0.0.1:18080/sitemap.xml"
        assert all(row[2] == "http://127.0.0.1:18080/sitemap.xml" for row in sm_rows)
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
