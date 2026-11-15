"""报告层测试：按 origin 的 Crawl-delay 台账进入 JSON/Markdown，且 JSON 确定性。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from pagefreeze.config import ArchiveConfig, OutputPaths
from pagefreeze.crawl import CrawlResult, FetchLog, OriginStats, RejectedTarget
from pagefreeze.report import build_summary, write_reports
from pagefreeze.verify import VerifyResult

_ARCHIVE = ArchiveConfig(archive_dir="archive", segment_size_bytes=16384,
    digest_algorithm="sha256", index_filename="index.cdx",
    ledger_filename="ledger.sqlite3", report_dir="reports",
    uuid_namespace=uuid.UUID("6f1c5a9e-2d47-5b83-8a10-3c4d5e6f7a8b"))


def _paths(out: Path) -> OutputPaths:
    settings = SimpleNamespace(archive=_ARCHIVE)
    from pagefreeze.config import paths_for
    return paths_for(settings, out)


def _crawl_result() -> CrawlResult:
    slow = OriginStats(origin="http://127.0.0.1:18001", request_count=4,
        request_starts=[0.0, 1.0, 2.0, 3.0], robots_skipped=1,
        effective_interval_ms=1000, robots_delay_ms=1000)
    fast = OriginStats(origin="http://127.0.0.1:18002", request_count=3,
        request_starts=[0.05, 0.15, 0.25], robots_skipped=0,
        effective_interval_ms=100, robots_delay_ms=0)
    logs = [
        FetchLog(url="http://127.0.0.1:18001/private", urlkey="x", depth=0,
            status=None, mime="", size=0, outcome="skipped",
            reason="robots_disallowed"),
        FetchLog(url="http://127.0.0.1:18001/", urlkey="y", depth=0,
            status=200, mime="text/html", size=12, outcome="archived"),
    ]
    return CrawlResult(hits=[], logs=logs,
        rejected=[RejectedTarget("http://127.0.0.1:18001/",
            "http://203.0.113.9/x", "TARGET_NOT_LOOPBACK")],
        origin_stats={slow.origin: slow, fast.origin: fast})


def _summary(crawl: CrawlResult, paths: OutputPaths) -> dict[str, object]:
    verify = VerifyResult(status="pass", stats={}, checks=[], issues=[])
    write = SimpleNamespace(segments=[], records=[], requests=0, responses=0,
        revisits=0, raw_bytes=0)
    return build_summary(run_id="run-report", timestamp="20240115000000",
        config_digest="sha256:abc", crawl=crawl, write=write,
        verify=verify, paths=paths)


def _write(crawl: CrawlResult, paths: OutputPaths) -> dict[str, object]:
    summary = _summary(crawl, paths)
    write_reports(paths, summary, live_crawl=crawl)
    return summary


def test_report_json_contains_per_origin_delay_and_skips(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(_crawl_result(), paths)
    payload = json.loads(paths.report_json.read_text("utf-8"))
    origins = {item["origin"]: item for item in payload["crawl"]["origins"]}
    slow = origins["http://127.0.0.1:18001"]
    fast = origins["http://127.0.0.1:18002"]
    assert slow["effective_interval_ms"] == 1000
    assert slow["robots_delay_ms"] == 1000
    assert slow["robots_skipped"] == 1
    assert slow["request_count"] == 4
    assert "request_starts" not in slow
    assert fast["effective_interval_ms"] == 100
    assert fast["robots_delay_ms"] == 0
    assert payload["crawl"]["skip_reasons"] == {"robots_disallowed": 1}


def test_markdown_report_lists_actual_request_starts(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write(_crawl_result(), paths)
    markdown = paths.report_md.read_text("utf-8")
    assert "http://127.0.0.1:18001" in markdown
    assert "http://127.0.0.1:18002" in markdown
    assert "1000" in markdown and "起始时间(相对秒)" in markdown
    assert "0, 1, 2, 3" in markdown
    assert "0.05, 0.15, 0.25" in markdown
    assert "robots 跳过" in markdown


def test_report_json_is_stable_across_runs_despite_wall_clock(tmp_path: Path) -> None:
    first = _crawl_result()
    second = _crawl_result()
    second.origin_stats["http://127.0.0.1:18001"].request_starts = [0.9, 1.9, 2.9, 3.9]
    left_paths, right_paths = _paths(tmp_path / "a"), _paths(tmp_path / "b")
    write_reports(left_paths, _summary(first, left_paths), live_crawl=first)
    write_reports(right_paths, _summary(second, right_paths), live_crawl=second)
    assert left_paths.report_json.read_bytes() == right_paths.report_json.read_bytes()
    left_md = left_paths.report_md.read_text("utf-8")
    assert "0, 1, 2, 3" in left_md
    right_md = right_paths.report_md.read_text("utf-8")
    assert "0.9, 1.9, 2.9, 3.9" in right_md


def test_regenerated_report_handles_missing_live_stats(tmp_path: Path) -> None:
    from pagefreeze.report import regenerate_reports
    from pagefreeze.ledger import write_run
    paths = _paths(tmp_path)
    summary = _summary(_crawl_result(), paths)
    write_run(paths.ledger_path, run_id="run-report", timestamp="20240115000000",
        config_digest="sha256:abc", status="pass", summary=summary,
        fetches=[], rejected=[], records=[], checks=[])
    regenerated = regenerate_reports(paths)
    assert regenerated["crawl"]["origins"][0]["origin"] == "http://127.0.0.1:18001"
    markdown = paths.report_md.read_text("utf-8")
    assert "见台账" in markdown
