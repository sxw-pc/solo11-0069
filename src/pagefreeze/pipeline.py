"""全流程编排：demo 启动 mock 站点，crawl 复用同一收尾逻辑。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import uuid
from . import cdx
from .config import OutputPaths, Settings, paths_for
from .crawl import CrawlResult, run_crawl
from .errors import PagefreezeError, PolicyError
from .ledger import write_run
from .mocksite import create_mock_server
from .policy import is_loopback_target, urlkey
from .report import build_summary, write_reports
from .store import WriteResult, write_archive
from .util import parse_warc_date, timestamp14
from .verify import VerifyResult, verify_archive
from .verify_report import write_verify_files


@dataclass
class PipelineResult:
    """一次 demo/crawl 的完整结果，供 CLI 与测试断言。"""
    settings: Settings
    paths: OutputPaths
    now: datetime
    crawl: CrawlResult
    write: WriteResult
    verify: VerifyResult
    summary: dict[str, object]
    run_id: str


def _clean_output(paths: OutputPaths) -> None:
    paths.archive_dir.mkdir(parents=True, exist_ok=True)
    paths.report_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("seg-*.warc.gz", paths.index_path.name):
        for path in paths.archive_dir.glob(pattern):
            path.unlink()
    for path in (paths.ledger_path, paths.verify_json, paths.verify_md,
                 paths.report_json, paths.report_md):
        if path.exists():
            path.unlink()


def _run_id(settings: Settings, now: datetime) -> str:
    name = f"run\n{settings.digest}\n{timestamp14(now)}"
    value = uuid.uuid5(settings.archive.uuid_namespace, name)
    return f"run-{value.hex[:16]}"


def _record_rows(write: WriteResult) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for record in write.records:
        target = record.header("WARC-Target-URI") or ""
        try:
            stamp = timestamp14(parse_warc_date(record.header("WARC-Date") or ""))
            key = urlkey(target) if target else ""
        except PagefreezeError:
            stamp = ""
            key = ""
        rows.append({
                "record_id": record.record_id,
                "type": record.record_type,
                "url": target,
                "urlkey": key,
                "timestamp": stamp,
                "filename": record.filename,
                "offset": record.offset,
                "length": record.length,
                "digest": record.header("WARC-Payload-Digest") or "",
            })
    return rows


def _finalize(
    settings: Settings,
    paths: OutputPaths,
    now: datetime,
    crawl: CrawlResult,
) -> PipelineResult:
    _clean_output(paths)
    write = write_archive(paths.archive_dir,
        settings.archive,
        crawl.hits,
        now,
        settings.digest,
        settings.crawl.user_agent,
    )
    cdx.write_index(paths.index_path, cdx.rows_from_records(write.records))
    verify = verify_archive(paths.archive_dir, paths.index_path, settings.archive.digest_algorithm)
    write_verify_files(verify, paths.verify_json, paths.verify_md)
    run_id = _run_id(settings, now)
    summary = build_summary(run_id=run_id,
        timestamp=timestamp14(now),
        config_digest=settings.digest,
        crawl=crawl,
        write=write,
        verify=verify,
        paths=paths,
    )
    write_run(paths.ledger_path,
        run_id=run_id,
        timestamp=timestamp14(now),
        config_digest=settings.digest,
        status=verify.status,
        summary=summary,
        fetches=[{
                "url": log.url, "urlkey": log.urlkey, "depth": log.depth,
                "status": log.status, "mime": log.mime, "size": log.size,
                "outcome": log.outcome, "reason": log.reason,
            }
            for log in crawl.logs],
        rejected=[{"source_url": item.source_url, "target": item.target, "reason": item.reason}
            for item in crawl.rejected],
        records=_record_rows(write),
        checks=[{
                "name": check.name, "status": check.status,
                "issues": check.issue_count, "detail": check.detail,
            }
            for check in verify.checks],
    )
    write_reports(paths, summary, live_crawl=crawl)
    return PipelineResult(settings=settings,
        paths=paths,
        now=now,
        crawl=crawl,
        write=write,
        verify=verify,
        summary=summary,
        run_id=run_id,
    )


def _seed_bind(seed: str) -> tuple[str, int]:
    parsed = urlsplit(seed)
    if parsed.scheme != "http":
        raise PagefreezeError("DEMO_SCHEME", "demo mock site only serves http URLs")
    host = (parsed.hostname or "").lower()
    if not host or not is_loopback_target(seed):
        raise PolicyError("TARGET_NOT_LOOPBACK", "demo seed must be loopback")
    return host, parsed.port or 80


def execute_demo(
    settings: Settings,
    out_root: str | Path,
    now: datetime,
) -> PipelineResult:
    """启动内置站点、抓取、封装、索引、核验、台账与报告。"""
    paths = paths_for(settings, out_root)
    host, port = _seed_bind(settings.crawl.seed_urls[0])
    server = create_mock_server(host, port, now)
    thread = threading.Thread(target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="pagefreeze-mock",
        daemon=True,
    )
    thread.start()
    try:
        crawl = run_crawl(settings.crawl)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return _finalize(settings, paths, now, crawl)


def execute_crawl(
    settings: Settings,
    out_root: str | Path,
    now: datetime,
) -> PipelineResult:
    """抓取已运行的本地站点，并完成与 demo 相同的收尾流程。"""
    paths = paths_for(settings, out_root)
    crawl = run_crawl(settings.crawl)
    return _finalize(settings, paths, now, crawl)
