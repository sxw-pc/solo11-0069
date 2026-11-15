"""报告与台账：资源发现计数、稳定键序与 Markdown 一致性。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import FIXED_NOW, ROOT
from pagefreeze.config import load_settings
from pagefreeze.pipeline import PipelineResult
from pagefreeze.report import regenerate_reports
from pagefreeze.util import parse_clock


def test_summary_reports_resource_edges(demo_result: PipelineResult) -> None:
    summary = json.loads(demo_result.paths.report_json.read_text(encoding="utf-8"))
    crawl = summary["crawl"]
    edges = demo_result.crawl.resources
    assert crawl["resource_edges"] == len(edges)
    assert crawl["resource_outcomes"] == demo_result.crawl.resource_outcomes()
    assert crawl["resource_kinds"] == demo_result.crawl.resource_kinds()
    assert sum(crawl["resource_outcomes"].values()) == len(edges)
    # demo 首页直接引用一个样式表与一张图片；两者都只归档一次。
    assert crawl["resource_edges"] >= 2
    styles = [ref for ref in edges if ref.kind == "stylesheet"]
    assert styles and styles[0].target.endswith("/assets/site.css")
    assert styles[0].source_url == "http://127.0.0.1:18080/"
    assert styles[0].outcome == "archived"


def test_resource_counts_match_fetch_and_archive_counts(
    demo_result: PipelineResult,
) -> None:
    crawl = demo_result.crawl
    archived_edges = [ref for ref in crawl.resources if ref.outcome == "archived"]
    archived_targets = {ref.target for ref in archived_edges}
    for target in archived_targets:
        logs = [log for log in crawl.logs if log.url == target]
        assert len(logs) == 1
        assert logs[0].outcome == "archived"
    summary = json.loads(demo_result.paths.report_json.read_text(encoding="utf-8"))
    resource_hits = [hit for hit in crawl.hits if any(
        hit.url == ref.target and ref.outcome == "archived" for ref in crawl.resources)]
    assert summary["crawl"]["archived"] == len(crawl.hits)
    assert "resource_edges" in summary["crawl"]
    assert set(summary["crawl"]["resource_outcomes"]) == {
        "archived", "duplicate", "error", "rejected", "skipped"}
    assert resource_hits and all(hit.url for hit in resource_hits)


def test_markdown_section_lists_kinds_and_outcomes(demo_result: PipelineResult) -> None:
    text = demo_result.paths.report_md.read_text(encoding="utf-8")
    assert "## 静态资源发现" in text
    crawl = demo_result.crawl
    for kind, count in crawl.resource_kinds().items():
        assert f"| `{kind}` | {count} |" in text
    for outcome, count in crawl.resource_outcomes().items():
        assert f"| `{outcome}` | {count} |" in text


def test_report_regeneration_preserves_resource_counts(
    demo_result: PipelineResult, tmp_path: Path
) -> None:
    connection = sqlite3.connect(demo_result.paths.ledger_path)
    try:
        summary_json = connection.execute(
            "SELECT summary_json FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        resource_rows = connection.execute(
            "SELECT source_url, target, kind, outcome, resource_depth, "
            "duplicate, cycle FROM resource_refs").fetchall()
    finally:
        connection.close()
    original = json.loads(summary_json)
    assert original["crawl"]["resource_edges"] == len(resource_rows)
    assert all(row[0] for row in resource_rows)
    assert set(row[3] for row in resource_rows) <= {
        "archived", "skipped", "error", "duplicate", "rejected"}
    regenerated = regenerate_reports(demo_result.paths)
    assert regenerated["crawl"]["resource_edges"] == len(resource_rows)
    assert regenerated["crawl"]["resource_outcomes"] == original["crawl"]["resource_outcomes"]


def test_report_is_byte_stable_under_fixed_clock(
    demo_result: PipelineResult, tmp_path: Path
) -> None:
    from pagefreeze.pipeline import execute_demo
    settings = load_settings(ROOT / "config")
    second = execute_demo(settings, tmp_path / "again", parse_clock(FIXED_NOW))
    assert demo_result.paths.report_json.read_bytes() == second.paths.report_json.read_bytes()
    assert demo_result.paths.report_md.read_bytes() == second.paths.report_md.read_bytes()
