"""SQLite 运行台账：runs/fetches/rejected_targets/records/verify_results。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from .errors import LedgerError
from .util import stable_json

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fetches (id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        url TEXT NOT NULL,
        urlkey TEXT NOT NULL,
        depth INTEGER NOT NULL,
        status INTEGER,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rejected_targets (id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        target TEXT NOT NULL,
        reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        type TEXT NOT NULL,
        url TEXT NOT NULL,
        urlkey TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        filename TEXT NOT NULL,
        offset INTEGER NOT NULL,
        length INTEGER NOT NULL,
        digest TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS verify_results (id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        check_name TEXT NOT NULL,
        status TEXT NOT NULL,
        issues INTEGER NOT NULL,
        detail TEXT NOT NULL
    )""",
)


def open_ledger(path: Path) -> sqlite3.Connection:
    """打开台账并建表；WAL 关闭，保持单文件易复制。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.commit()
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise LedgerError("LEDGER_OPEN_ERROR", f"cannot open ledger: {exc}") from exc


def write_run(
    path: Path,
    *,
    run_id: str,
    timestamp: str,
    config_digest: str,
    status: str,
    summary: dict[str, Any],
    fetches: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    records: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> None:
    """写入一次完整运行；同一 run_id 重跑时先清理旧行，避免重复。"""
    connection = open_ledger(path)
    try:
        with connection:
            for table in ("fetches", "rejected_targets", "records", "verify_results", "runs"):
                connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            connection.execute("INSERT INTO runs(run_id, timestamp, config_digest, status, summary_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, timestamp, config_digest, status, stable_json(summary)),
            )
            connection.executemany(
                "INSERT INTO fetches(run_id,url,urlkey,depth,status,mime,size,outcome,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(run_id, row["url"], row["urlkey"], row["depth"], row.get("status"),
                     row["mime"], row["size"], row["outcome"], row.get("reason"))
                    for row in fetches],
            )
            connection.executemany(
                "INSERT INTO rejected_targets(run_id,source_url,target,reason) VALUES (?,?,?,?)",
                [(run_id, row["source_url"], row["target"], row["reason"]) for row in rejected],
            )
            connection.executemany(
                "INSERT INTO records(run_id,record_id,type,url,urlkey,timestamp,filename,"
                "offset,length,digest) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(run_id, row["record_id"], row["type"], row["url"], row["urlkey"],
                     row["timestamp"], row["filename"], row["offset"], row["length"],
                     row["digest"])
                    for row in records],
            )
            connection.executemany("INSERT INTO verify_results(run_id,check_name,status,issues,detail) "
                "VALUES (?,?,?,?,?)",
                [(run_id, row["name"], row["status"], row["issues"], row["detail"])
                    for row in checks],
            )
    except sqlite3.Error as exc:
        raise LedgerError("LEDGER_WRITE_ERROR", f"cannot write ledger: {exc}") from exc
    finally:
        connection.close()


def latest_summary(path: Path) -> dict[str, Any] | None:
    """读取最近一次运行的报告摘要；空台账返回 None。"""
    if not path.is_file():
        return None
    connection = open_ledger(path)
    try:
        row = connection.execute("SELECT summary_json FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise LedgerError("LEDGER_READ_ERROR", f"cannot read ledger: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except json.JSONDecodeError as exc:
        raise LedgerError("LEDGER_READ_ERROR", "invalid summary JSON") from exc
    if not isinstance(value, dict):
        raise LedgerError("LEDGER_READ_ERROR", "summary must be an object")
    return value
