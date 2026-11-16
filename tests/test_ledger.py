"""Sitemap 溯源字段的 SQLite 台账测试：写入、回查与旧表结构迁移。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from pagefreeze.crawl import FetchLog, RejectedTarget
from pagefreeze.frontier import KIND_PAGE, KIND_ROBOTS, KIND_SITEMAP
from pagefreeze.ledger import open_ledger, write_run


def _fetch_rows() -> list[dict]:
    return [
        {"url": "http://127.0.0.1:9/robots.txt", "urlkey": "k1", "depth": 0,
            "status": 200, "mime": "text/plain", "size": 40, "outcome": "archived",
            "reason": None, "kind": KIND_ROBOTS, "source_url": None,
            "parent_sitemap": None},
        {"url": "http://127.0.0.1:9/sitemaps/root.xml", "urlkey": "k2", "depth": 0,
            "status": 200, "mime": "application/xml", "size": 80, "outcome": "archived",
            "reason": None, "kind": KIND_SITEMAP,
            "source_url": "http://127.0.0.1:9/robots.txt", "parent_sitemap": None},
        {"url": "http://127.0.0.1:9/sm/one", "urlkey": "k3", "depth": 0,
            "status": 200, "mime": "text/html", "size": 12, "outcome": "archived",
            "reason": None, "kind": KIND_PAGE,
            "source_url": "http://127.0.0.1:9/sitemaps/root.xml",
            "parent_sitemap": "http://127.0.0.1:9/sitemaps/root.xml"},
        {"url": "http://127.0.0.1:9/private/blocked", "urlkey": "k4", "depth": 0,
            "status": None, "mime": "", "size": 0, "outcome": "skipped",
            "reason": "robots_disallowed", "kind": KIND_PAGE,
            "source_url": "http://127.0.0.1:9/sitemaps/root.xml",
            "parent_sitemap": "http://127.0.0.1:9/sitemaps/root.xml"},
    ]


def _rejected_rows() -> list[dict]:
    return [
        {"source_url": "http://127.0.0.1:9/robots.txt",
            "target": "http://203.0.113.7/foreign.xml",
            "reason": "SITEMAP_CROSS_ORIGIN", "source_kind": KIND_SITEMAP},
        {"source_url": "http://127.0.0.1:9/", "target": "http://203.0.113.7/outside",
            "reason": "TARGET_NOT_LOOPBACK", "source_kind": KIND_PAGE},
    ]


def test_write_run_persists_kind_and_traceability_columns(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.sqlite3"
    write_run(ledger, run_id="run-1", timestamp="20240115000000",
        config_digest="sha256:abc", status="pass",
        summary={"schema": "pagefreeze.report/1"}, fetches=_fetch_rows(),
        rejected=_rejected_rows(), records=[], checks=[])
    connection = sqlite3.connect(ledger)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(fetches)")}
        assert {"kind", "source_url", "parent_sitemap"} <= columns
        by_url = dict(connection.execute("SELECT url, kind FROM fetches"))
        assert by_url["http://127.0.0.1:9/sitemaps/root.xml"] == KIND_SITEMAP
        assert by_url["http://127.0.0.1:9/robots.txt"] == KIND_ROBOTS
        trace = connection.execute(
            "SELECT source_url, parent_sitemap FROM fetches WHERE url = ?",
            ("http://127.0.0.1:9/sm/one",)).fetchone()
        assert trace == ("http://127.0.0.1:9/sitemaps/root.xml",
            "http://127.0.0.1:9/sitemaps/root.xml")
        rejected = dict(connection.execute(
            "SELECT target, source_kind FROM rejected_targets"))
        assert rejected["http://203.0.113.7/foreign.xml"] == KIND_SITEMAP
        assert rejected["http://203.0.113.7/outside"] == KIND_PAGE
    finally:
        connection.close()


def test_open_ledger_migrates_legacy_schema_without_sitemap_columns(tmp_path: Path) -> None:
    ledger = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(ledger)
    try:
        connection.execute(
            "CREATE TABLE fetches (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
            "url TEXT, urlkey TEXT, depth INTEGER, status INTEGER, mime TEXT, "
            "size INTEGER, outcome TEXT, reason TEXT)")
        connection.execute(
            "CREATE TABLE rejected_targets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT, source_url TEXT, target TEXT, reason TEXT)")
        connection.commit()
    finally:
        connection.close()
    migrated = open_ledger(ledger)
    try:
        fetch_columns = {row[1] for row in migrated.execute("PRAGMA table_info(fetches)")}
        rejected_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(rejected_targets)")}
        assert {"kind", "source_url", "parent_sitemap"} <= fetch_columns
        assert "source_kind" in rejected_columns
        # 迁移列有默认值，旧行按普通页面归类。
        migrated.execute(
            "INSERT INTO fetches(run_id,url,urlkey,depth,mime,size,outcome) "
            "VALUES ('r','u','k',0,'text/html',1,'archived')")
        migrated.commit()
        kind = migrated.execute("SELECT kind FROM fetches WHERE url='u'").fetchone()[0]
        assert kind == KIND_PAGE
    finally:
        migrated.close()


def test_fetch_log_defaults_remain_backward_compatible() -> None:
    log = FetchLog(url="u", urlkey="k", depth=0, status=None, mime="", size=0,
        outcome="skipped", reason="timeout")
    assert log.kind == KIND_PAGE
    assert log.source_url is None
    assert log.parent_sitemap is None
    rejected = RejectedTarget(source_url="s", target="t", reason="TARGET_NOT_LOOPBACK")
    assert rejected.source_kind == KIND_PAGE
