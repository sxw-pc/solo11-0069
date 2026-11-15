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
