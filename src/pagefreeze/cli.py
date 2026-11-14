"""命令行入口：demo / crawl / index / verify / replay / serve-site / report。"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from . import cdx
from .config import load_settings, paths_for
from .digest import files_fingerprint
from .errors import ConfigError, PagefreezeError, PolicyError
from .mocksite import create_mock_server
from .pipeline import PipelineResult, execute_crawl, execute_demo
from .policy import normalize_url, validate_bind_host
from .replay import ReplayServer
from .report import archive_fingerprint, index_fingerprint, regenerate_reports
from .util import DEFAULT_DEMO_CLOCK, resolve_clock, stable_json
from .verify import verify_archive
from .verify_report import write_verify_files
_LOGGER = logging.getLogger(__name__)
_RESULT_CODES = {"pass": 0, "fail": 5}


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="crawl.json 或包含两份配置的目录")
    common.add_argument("--out", default="runtime/demo", help="产物根目录")
    common.add_argument("--now", help="14 位 UTC 时间戳或 ISO-8601")
    return common


def build_parser() -> argparse.ArgumentParser:
    """构造全部子命令；每个子命令都有可读的 --help。"""
    common = _common_parser()
    parser = argparse.ArgumentParser(prog="pagefreeze",
        description="封页台：本地网站归档封装、索引、离线回放与完整性核验。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", parents=[common], help="一条命令跑通全流程")
    sub.add_parser("crawl", parents=[common], help="抓取配置中的种子并归档")
    p_index = sub.add_parser("index", parents=[common], help="从归档卷重建 CDX 索引")
    p_index.add_argument("--url", help="按规范化 URL 过滤")
    p_index.add_argument("--urlkey", help="按 urlkey 过滤")
    p_index.add_argument("--from", dest="from_ts", help="起始 14 位时间戳（含）")
    p_index.add_argument("--to", dest="to_ts", help="结束 14 位时间戳（含）")
    sub.add_parser("verify", parents=[common], help="核验归档、索引与 revisit")
    p_report = sub.add_parser("report", parents=[common], help="从台账重建报告")
    p_report.set_defaults(command="report")
    p_replay = sub.add_parser("replay", parents=[common], help="启动离线回放服务")
    p_replay.add_argument("--archive", help="归档目录；默认 <out>/archive")
    p_replay.add_argument("--index", help="索引文件；默认归档目录内 index.cdx")
    p_replay.add_argument("--host", default="127.0.0.1")
    p_replay.add_argument("--port", type=int, default=18081)
    p_site = sub.add_parser("serve-site", parents=[common], help="启动内置 mock 站点")
    p_site.add_argument("--host", default="127.0.0.1")
    p_site.add_argument("--port", type=int, default=18080)
    return parser


def _settings(args: argparse.Namespace):
    return load_settings(args.config)


def _summary_line(prefix: str, result: PipelineResult) -> str:
    write = result.write
    crawl = result.crawl
    return (f"{prefix} records={len(write.records)} requests={write.requests} "
        f"responses={write.responses} revisits={write.revisits} "
        f"rejected={len(crawl.rejected)} skipped={len(crawl.logs) - len(crawl.hits)} "
        f"segments={len(write.segments)} verify={result.verify.status}")


def _cmd_demo(args: argparse.Namespace) -> int:
    now = resolve_clock(args.now, demo_default=DEFAULT_DEMO_CLOCK)
    settings = _settings(args)
    result = execute_demo(settings, args.out, now)
    prefix = "DEMO_OK" if result.verify.status == "pass" else "DEMO_FAIL"
    print(_summary_line(prefix, result))
    print(f"RUN_ID={result.run_id}")
    print(f"ARCHIVE_SHA256={archive_fingerprint(result.paths)}")
    print(f"INDEX_SHA256={index_fingerprint(result.paths)}")
    return _RESULT_CODES[result.verify.status]


def _cmd_crawl(args: argparse.Namespace) -> int:
    now = resolve_clock(args.now)
    settings = _settings(args)
    result = execute_crawl(settings, args.out, now)
    prefix = "CRAWL_OK" if result.verify.status == "pass" else "CRAWL_FAIL"
    print(_summary_line(prefix, result))
    return _RESULT_CODES[result.verify.status]


def _cmd_index(args: argparse.Namespace) -> int:
    settings = _settings(args)
    paths = paths_for(settings, args.out)
    rows = cdx.rows_from_archive(paths.archive_dir)
    cdx.write_index(paths.index_path, rows)
    filters = any((args.url, args.urlkey, args.from_ts, args.to_ts))
    if not filters:
        print(f"INDEX_OK entries={len(rows)}")
        return 0
    selected = rows
    if args.url:
        wanted_url = normalize_url(args.url)
        selected = [row for row in selected if row.url == wanted_url]
    if args.urlkey:
        selected = [row for row in selected if row.urlkey == args.urlkey]
    if args.from_ts:
        selected = [row for row in selected if row.timestamp >= args.from_ts]
    if args.to_ts:
        selected = [row for row in selected if row.timestamp <= args.to_ts]
    print(stable_json([row.to_dict() for row in selected]), end="")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    settings = _settings(args)
    paths = paths_for(settings, args.out)
    result = verify_archive(paths.archive_dir, paths.index_path, settings.archive.digest_algorithm)
    write_verify_files(result, paths.verify_json, paths.verify_md)
    print(f"VERIFY={result.status} issues={len(result.issues)}")
    return _RESULT_CODES[result.status]


def _cmd_replay(args: argparse.Namespace) -> int:
    settings = _settings(args)
    paths = paths_for(settings, args.out)
    archive = Path(args.archive) if args.archive else paths.archive_dir
    index = Path(args.index) if args.index else archive / settings.archive.index_filename
    server = ReplayServer(archive, index, args.host, args.port)
    segments = sorted(archive.glob("seg-*.warc.gz"), key=lambda item: item.name)
    archive_digest = files_fingerprint(segments) if segments else ""
    try:
        print(f"REPLAY_LISTEN={server.host}:{server.port} "
            f"SNAPSHOTS={server.snapshot_count} ARCHIVE_SHA256={archive_digest} "
            f"INDEX_SHA256={server.httpd.index_digest}", flush=True)
        server.serve_forever()
    finally:
        server.close()
    return 0


def _cmd_serve_site(args: argparse.Namespace) -> int:
    validate_bind_host(args.host)
    now = resolve_clock(args.now)
    server = create_mock_server(args.host, args.port, now)
    try:
        host, port = server.server_address[:2]
        print(f"SERVE_SITE_LISTEN={host}:{port}", flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    settings = _settings(args)
    paths = paths_for(settings, args.out)
    summary = regenerate_reports(paths)
    print(f"REPORT_OK run_id={summary.get('run_id', '')} "
        f"verify={summary.get('verify', {}).get('status', 'unknown')}")
    return 0
_COMMANDS = {"demo": _cmd_demo,
    "crawl": _cmd_crawl,
    "index": _cmd_index,
    "verify": _cmd_verify,
    "replay": _cmd_replay,
    "serve-site": _cmd_serve_site,
    "report": _cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    """解析参数并执行；稳定错误码输出到 stderr，返回非零退出码。"""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except ConfigError as exc:
        print(f"ERR {exc.code} {exc.message}", file=sys.stderr)
        return 3
    except PolicyError as exc:
        print(f"ERR {exc.code} {exc.message}", file=sys.stderr)
        return 4
    except PagefreezeError as exc:
        print(f"ERR {exc.code} {exc.message}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("ERR INTERRUPTED", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"ERR OS_ERROR {exc}", file=sys.stderr)
        return 4
