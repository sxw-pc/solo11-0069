"""报告测试：入口策略、分类计数与 Markdown 表格的归类展示。"""
from __future__ import annotations

import json
from pagefreeze.config import FIFO_MERGED, SITEMAP_PRIORITY, load_settings, paths_for
from pagefreeze.crawl import CrawlResult, FetchLog, RejectedTarget
from pagefreeze.fetch import FetchResult
from pagefreeze.frontier import KIND_PAGE, KIND_ROBOTS, KIND_SITEMAP
from pagefreeze.report import build_summary, write_reports
from pagefreeze.store import WriteResult
from pagefreeze.verify import VerifyResult
from conftest import ROOT


def _log(url: str, kind: str, outcome: str, source: str | None = None,
         parent: str | None = None, reason: str | None = None) -> FetchLog:
    return FetchLog(url=url, urlkey=f"key:{url}", depth=0,
        status=200 if outcome == "archived" else None,
        mime="application/xml" if kind == KIND_SITEMAP else "text/html",
        size=10, outcome=outcome, kind=kind, reason=reason,
        source_url=source, parent_sitemap=parent)


def _crawl(strategy: str) -> CrawlResult:
    sitemap_url = "http://127.0.0.1:9/sitemap.xml"
    logs = [
        _log("http://127.0.0.1:9/robots.txt", KIND_ROBOTS, "archived"),
        _log(sitemap_url, KIND_SITEMAP, "archived",
            source="http://127.0.0.1:9/robots.txt"),
        _log("http://127.0.0.1:9/sm/a", KIND_PAGE, "archived",
            source=sitemap_url, parent=sitemap_url),
        _log("http://127.0.0.1:9/sm/b", KIND_PAGE, "archived",
            source=sitemap_url, parent=sitemap_url),
        _log("http://127.0.0.1:9/", KIND_PAGE, "archived", source="http://127.0.0.1:9/"),
        _log("http://127.0.0.1:9/nav/a", KIND_PAGE, "archived",
            source="http://127.0.0.1:9/"),
        _log("http://127.0.0.1:9/private/x", KIND_PAGE, "skipped",
            source=sitemap_url, parent=sitemap_url, reason="robots_disallowed"),
        _log(sitemap_url + "-broken", KIND_SITEMAP, "skipped",
            source="http://127.0.0.1:9/robots.txt",
            reason="sitemap_parse_error"),
    ]
    hits = [FetchResult(url=log.url, status=200, reason="OK", version="HTTP/1.1",
        headers=((),), body=b"x" * 10, mime=log.mime)
        for log in logs if log.outcome == "archived"]
    rejected = [
        RejectedTarget(source_url="http://127.0.0.1:9/robots.txt",
            target="http://203.0.113.7/foreign.xml",
            reason="SITEMAP_CROSS_ORIGIN", source_kind=KIND_SITEMAP),
    ]
    return CrawlResult(hits=hits, logs=logs, rejected=rejected,
        sitemap_strategy=strategy)


def _summary(strategy: str, tmp_path):
    settings = load_settings(ROOT / "config")
    paths = paths_for(settings, tmp_path)
    verify = VerifyResult(status="pass", stats={}, checks=[], issues=[])
    return build_summary(run_id="run-x", timestamp="20240115000000",
        config_digest="sha256:0", crawl=_crawl(strategy), write=WriteResult(),
        verify=verify, paths=paths), paths


def test_report_breaks_down_attempts_by_request_kind(tmp_path) -> None:
    summary, _ = _summary(SITEMAP_PRIORITY, tmp_path)
    crawl = summary["crawl"]
    assert crawl["sitemap_entry_strategy"] == SITEMAP_PRIORITY
    assert crawl["request_kinds"] == {
        "page": {"attempted": 5, "archived": 4, "skipped": 1},
        "sitemap": {"attempted": 2, "archived": 1, "skipped": 1},
        "robots": {"attempted": 1, "archived": 1, "skipped": 0},
    }
    # 分类合计与顶层口径一致。
    assert sum(row["attempted"] for row in crawl["request_kinds"].values()) == crawl["attempted"]
    assert sum(row["archived"] for row in crawl["request_kinds"].values()) == crawl["archived"]
    # 页面再按来源拆分。
    sm_source = crawl["page_sources"]["http://127.0.0.1:9/sitemap.xml"]
    assert sm_source == {"attempted": 3, "archived": 2, "skipped": 1}
    assert crawl["rejected_targets"] == 1


def test_markdown_report_records_strategy_and_kind_tables(tmp_path) -> None:
    summary, paths = _summary(FIFO_MERGED, tmp_path)
    write_reports(paths, summary)
    markdown = paths.report_md.read_text(encoding="utf-8")
    assert "fifo_merged" in markdown
    assert "sitemap_priority 独立优先批次" in markdown
    assert "| sitemap | 2 | 1 | 1 |" in markdown
    assert "| robots | 1 | 1 | 0 |" in markdown
    assert "| page | 5 | 4 | 1 |" in markdown
    assert "sitemap_parse_error" in markdown
    # JSON 与 Markdown 数字一致，且不含绝对路径。
    on_disk = json.loads(paths.report_json.read_text(encoding="utf-8"))
    assert on_disk["crawl"]["request_kinds"]["sitemap"]["archived"] == 1
    assert str(tmp_path) not in markdown


def test_strategy_value_is_visible_not_implied(tmp_path) -> None:
    for strategy in (SITEMAP_PRIORITY, FIFO_MERGED):
        summary, _ = _summary(strategy, tmp_path)
        assert summary["crawl"]["sitemap_entry_strategy"] == strategy
