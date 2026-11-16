"""报告与台账：链接发现跳过单独统计，不混入抓取失败。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from conftest import FIXED_NOW, ROOT
from pagefreeze.config import load_settings
from pagefreeze.crawl import LINK_DISCOVERY_SKIP
from pagefreeze.pipeline import PipelineResult, execute_crawl
from pagefreeze.report import regenerate_reports
from pagefreeze.util import parse_clock


def _settings(site: str, path: str):
    base = load_settings(ROOT / "config")
    crawl = replace(base.crawl,
        seed_urls=(site + path,),
        respect_robots=False,
        max_pages=4,
        max_depth=2,
    )
    return replace(base, crawl=crawl)


def test_demo_report_has_zero_link_skips(demo_result: PipelineResult) -> None:
    summary = json.loads(demo_result.paths.report_json.read_text(encoding="utf-8"))
    crawl = summary["crawl"]
    assert crawl["link_discovery_skipped"] == 0
    assert crawl["link_skip_reasons"] == {}
    # demo 的既有跳过原因仍是抓取层原因。
    assert set(crawl["fetch_skip_reasons"]) == {
        "robots_disallowed", "body_too_large", "timeout"}
    assert crawl["skipped"] == demo_result.crawl.skipped
    assert "链接发现跳过（已归档但不扩展）：0" in (
        demo_result.paths.report_md.read_text(encoding="utf-8"))


def test_bad_gzip_page_is_reported_as_archived_not_failed(
    gzip_site: str, tmp_path: Path
) -> None:
    result = execute_crawl(_settings(gzip_site, "/gz/bad-crc.html"), tmp_path,
        parse_clock(FIXED_NOW))
    summary = json.loads(result.paths.report_json.read_text(encoding="utf-8"))
    crawl = summary["crawl"]
    assert crawl["archived"] == 1
    assert crawl["skipped"] == 0
    assert crawl["fetch_skip_reasons"] == {}
    assert crawl["link_discovery_skipped"] == 1
    assert crawl["link_skip_reasons"] == {LINK_DISCOVERY_SKIP: 1}
    assert crawl["skip_reasons"] == {LINK_DISCOVERY_SKIP: 1}
    text = result.paths.report_md.read_text(encoding="utf-8")
    assert "| 链接发现跳过原因 | 次数 |" in text
    assert f"| `{LINK_DISCOVERY_SKIP}` | 1 |" in text
    assert result.verify.status == "pass"


def test_ledger_keeps_link_skip_in_dedicated_table(
    gzip_site: str, tmp_path: Path
) -> None:
    url = gzip_site + "/gz/truncated.html"
    execute_crawl(_settings(gzip_site, "/gz/truncated.html"), tmp_path,
        parse_clock(FIXED_NOW))
    ledger = tmp_path / "ledger.sqlite3"
    connection = sqlite3.connect(ledger)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        skip_rows = connection.execute(
            "SELECT url, urlkey, depth, reason, detail FROM link_discovery_skips"
        ).fetchall()
        fetch_rows = connection.execute(
            "SELECT outcome, reason FROM fetches").fetchall()
    finally:
        connection.close()
    assert "link_discovery_skips" in tables
    assert len(skip_rows) == 1
    assert skip_rows[0][0] == url
    assert skip_rows[0][3:] == (LINK_DISCOVERY_SKIP, "GZIP_TRUNCATED")
    assert fetch_rows == [("archived", LINK_DISCOVERY_SKIP)]


def test_regenerated_report_preserves_link_skip_counts(
    gzip_site: str, tmp_path: Path
) -> None:
    result = execute_crawl(_settings(gzip_site, "/gz/bad-isize.html"), tmp_path,
        parse_clock(FIXED_NOW))
    original = json.loads(result.paths.report_json.read_text(encoding="utf-8"))
    regenerated = regenerate_reports(result.paths)
    assert regenerated["crawl"]["link_discovery_skipped"] == 1
    assert regenerated["crawl"]["link_skip_reasons"] == original["crawl"]["link_skip_reasons"]
    assert regenerated["crawl"]["skipped"] == original["crawl"]["skipped"] == 0
