"""CLI 冒烟：帮助、稳定错误码、非回环绑定与一次真实 demo。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from conftest import ROOT

COMMANDS = ["demo", "crawl", "index", "verify", "replay", "serve-site", "report"]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PAGEFREEZE_CLOCK"] = "20240115000000"
    return subprocess.run([sys.executable, "-m", "pagefreeze", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_every_subcommand_help_exits_zero() -> None:
    for command in [*COMMANDS, ""]:
        args = ["--help"] if not command else [command, "--help"]
        completed = _run(*args)
        assert completed.returncode == 0, (command, completed.stderr)
        assert "usage:" in completed.stdout


def test_invalid_config_has_stable_code(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    crawl = json.loads((config / "crawl.json").read_text(encoding="utf-8"))
    crawl["max_depth"] = 99
    (config / "crawl.json").write_text(json.dumps(crawl, ensure_ascii=False), encoding="utf-8")
    completed = _run("verify", "--config", str(config), "--out", str(tmp_path / "out"))
    assert completed.returncode == 3
    assert "CFG_DEPTH_RANGE" in completed.stderr


def test_non_loopback_bind_is_rejected() -> None:
    completed = _run("serve-site", "--host", "203.0.113.9", "--port", "0")
    assert completed.returncode == 4
    assert "BIND_NOT_LOOPBACK" in completed.stderr


def test_demo_subprocess_writes_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    completed = _run("demo", "--now", "20240115000000", "--out", str(out))
    assert completed.returncode == 0, completed.stderr
    first_line = completed.stdout.splitlines()[0]
    assert first_line.startswith("DEMO_OK ")
    for token in ("records=41", "requests=20", "responses=19", "revisits=1", "verify=pass"):
        assert token in first_line
    assert (out / "archive" / "index.cdx").is_file()
    assert len(list((out / "archive").glob("seg-*.warc.gz"))) == 2
    assert (out / "reports" / "report.json").is_file()
    connection = sqlite3.connect(out / "ledger.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 41
    finally:
        connection.close()


def test_sitemap_strategy_override_changes_budget_allocation(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    crawl = json.loads((config / "crawl.json").read_text(encoding="utf-8"))
    crawl["max_pages"] = 4
    (config / "crawl.json").write_text(json.dumps(crawl, ensure_ascii=False),
        encoding="utf-8")
    out_a = tmp_path / "out-a"
    out_b = tmp_path / "out-b"
    run_a = _run("demo", "--now", "20240115000000", "--config", str(config),
        "--out", str(out_a), "--sitemap-strategy", "sitemap_priority")
    run_b = _run("demo", "--now", "20240115000000", "--config", str(config),
        "--out", str(out_b), "--sitemap-strategy", "fifo_merged")
    assert run_a.returncode == 0 and run_b.returncode == 0
    report_a = json.loads((out_a / "reports" / "report.json").read_text("utf-8"))
    report_b = json.loads((out_b / "reports" / "report.json").read_text("utf-8"))
    assert report_a["crawl"]["sitemap_entry_strategy"] == "sitemap_priority"
    assert report_b["crawl"]["sitemap_entry_strategy"] == "fifo_merged"
    # 同 max_pages=4 下，A 把名额给 sitemap 页，B 给首页 BFS 导航页。
    sm_key = "http://127.0.0.1:18080/sitemap.xml"
    assert report_a["crawl"]["page_sources"][sm_key]["archived"] == 3
    assert sm_key not in report_b["crawl"]["page_sources"] or \
        report_b["crawl"]["page_sources"][sm_key]["archived"] == 0


def test_invalid_sitemap_strategy_in_config_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    crawl_path = config / "crawl.json"
    crawl = json.loads(crawl_path.read_text(encoding="utf-8"))
    crawl["sitemap_entry_strategy"] = "bogus"
    crawl_path.write_text(json.dumps(crawl, ensure_ascii=False), encoding="utf-8")
    completed = _run("demo", "--config", str(config), "--out", str(tmp_path / "out"))
    assert completed.returncode == 3
    assert "CFG_SITEMAP_STRATEGY" in completed.stderr


def test_invalid_sitemap_depth_in_config_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    crawl_path = config / "crawl.json"
    crawl = json.loads(crawl_path.read_text(encoding="utf-8"))
    crawl["sitemap_max_depth"] = 9
    crawl_path.write_text(json.dumps(crawl, ensure_ascii=False), encoding="utf-8")
    completed = _run("demo", "--config", str(config), "--out", str(tmp_path / "out"))
    assert completed.returncode == 3
    assert "CFG_SITEMAP_DEPTH_RANGE" in completed.stderr
