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
    for token in ("records=33", "requests=16", "responses=15", "revisits=1", "verify=pass"):
        assert token in first_line
    assert (out / "archive" / "index.cdx").is_file()
    assert len(list((out / "archive").glob("seg-*.warc.gz"))) == 2
    assert (out / "reports" / "report.json").is_file()
    connection = sqlite3.connect(out / "ledger.sqlite3")
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 33
    finally:
        connection.close()
