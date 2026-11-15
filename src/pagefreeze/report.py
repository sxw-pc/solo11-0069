"""运行报告：固定键顺序的 JSON 与无模板引擎的 Markdown。"""
from __future__ import annotations

from typing import Any
from .config import OutputPaths
from .crawl import CrawlResult
from .errors import PagefreezeError
from .ledger import latest_summary
from .store import WriteResult
from .digest import files_fingerprint, sha256_file
from .util import size_bucket, stable_json, write_text
from .verify import VerifyResult
_BUCKET_ORDER = ("lt_1k", "1k_10k", "ge_10k")


def build_summary(
    *,
    run_id: str,
    timestamp: str,
    config_digest: str,
    crawl: CrawlResult,
    write: WriteResult,
    verify: VerifyResult,
    paths: OutputPaths,
) -> dict[str, Any]:
    """聚合抓取、去重、体积、核验与文件引用，键顺序固定。"""
    buckets = {name: 0 for name in _BUCKET_ORDER}
    for hit in crawl.hits:
        buckets[size_bucket(hit.size)] += 1
    verify_checks = [{"name": check.name, "status": check.status, "detail": check.detail}
        for check in verify.checks]
    return {"schema": "pagefreeze.report/1",
        "run_id": run_id,
        "timestamp": timestamp,
        "config_digest": config_digest,
        "crawl": {"attempted": len(crawl.logs),
            "archived": len(crawl.hits),
            "unique_urls": len(crawl.logs),
            "bytes": crawl.bytes,
            "rejected_targets": len(crawl.rejected),
            "skipped": len(crawl.logs) - len(crawl.hits),
            "skip_reasons": crawl.skip_reasons(),
            "resource_edges": len(crawl.resources),
            "resource_outcomes": crawl.resource_outcomes(),
            "resource_kinds": crawl.resource_kinds(),
        },
        "archive": {"segments": len(write.segments),
            "records": len(write.records),
            "requests": write.requests,
            "responses": write.responses,
            "revisits": write.revisits,
            "dedup_hits": write.revisits,
            "bytes": write.raw_bytes,
            "size_buckets": buckets,
        },
        "verify": {"status": verify.status,
            "issues": len(verify.issues),
            "checks": verify_checks,
        },
        "files": {"index": paths.index_path.name,
            "ledger": paths.ledger_path.name,
            "verify_json": paths.verify_json.name,
            "verify_md": paths.verify_md.name,
            "report_json": paths.report_json.name,
            "report_md": paths.report_md.name,
        },
    }


def write_reports(paths: OutputPaths, summary: dict[str, Any]) -> None:
    """写出 report.json 与 report.md，不包含绝对路径或系统时间。"""
    write_text(paths.report_json, stable_json(summary))
    crawl = summary["crawl"]
    archive = summary["archive"]
    verify = summary["verify"]
    lines = ["# 封页台归档报告",
        "",
        f"- 运行 ID：`{summary['run_id']}`",
        f"- 抓取时点：{summary['timestamp']}",
        f"- 配置摘要：`{summary['config_digest']}`",
        "",
        "## 抓取",
        "",
        f"- 尝试：{crawl['attempted']}",
        f"- 归档：{crawl['archived']}",
        f"- 回环外拒绝：{crawl['rejected_targets']}",
        f"- 跳过或失败：{crawl['skipped']}",
        f"- 响应体字节：{crawl['bytes']}",
        "",
        "| 跳过原因 | 次数 |",
        "| --- | --- |",
    ]
    reasons = crawl["skip_reasons"]
    if reasons:
        lines.extend(f"| `{name}` | {count} |" for name, count in reasons.items())
    else:
        lines.append("| 无 | 0 |")
    lines.extend([
            "",
            "## 静态资源发现",
            "",
            f"- 引用边：{crawl.get('resource_edges', 0)}",
            "",
            "| 引用类型 | 数量 |",
            "| --- | --- |",
        ])
    kinds = crawl.get("resource_kinds") or {}
    for name, count in kinds.items():
        lines.append(f"| `{name}` | {count} |")
    if not kinds:
        lines.append("| 无 | 0 |")
    lines.extend([
            "",
            "| 引用结果 | 数量 |",
            "| --- | --- |",
        ])
    outcomes = crawl.get("resource_outcomes") or {}
    for name, count in outcomes.items():
        lines.append(f"| `{name}` | {count} |")
    if not outcomes:
        lines.append("| 无 | 0 |")
    lines.extend([
            "",
            "## 归档",
            "",
            f"- 分卷：{archive['segments']}",
            f"- 记录：{archive['records']}",
            f"- request / response / revisit：{archive['requests']} / "
            f"{archive['responses']} / {archive['revisits']}",
            f"- 去重命中：{archive['dedup_hits']}",
            f"- 解压后 WARC 字节：{archive['bytes']}",
            "",
            "| 载荷大小区间 | 数量 |",
            "| --- | --- |",
        ])
    for name in _BUCKET_ORDER:
        lines.append(f"| {name} | {archive['size_buckets'].get(name, 0)} |")
    lines.extend([
            "",
            "## 核验",
            "",
            f"- 结论：**{verify['status']}**",
            f"- 问题数：{verify['issues']}",
            "",
            "| 检查 | 状态 | 详情 |",
            "| --- | --- | --- |",
        ])
    for check in verify["checks"]:
        lines.append(f"| {check['name']} | {check['status']} | {check['detail']} |")
    lines.extend([
            "",
            "## 产物",
            "",
            f"- `{summary['files']['index']}`",
            f"- `{summary['files']['verify_json']}` / `{summary['files']['verify_md']}`",
            f"- `{summary['files']['ledger']}`",
            f"- `{summary['files']['report_json']}` / `{summary['files']['report_md']}`",
        ])
    write_text(paths.report_md, "\n".join(lines) + "\n")


def regenerate_reports(paths: OutputPaths) -> dict[str, Any]:
    """从最新 SQLite 台账摘要重新生成报告文件。"""
    summary = latest_summary(paths.ledger_path)
    if summary is None:
        raise PagefreezeError("REPORT_NO_RUN", "ledger has no completed run")
    report_json = summary.get("files", {}).get("report_json", "report.json")
    if report_json != paths.report_json.name:
        summary["files"]["report_json"] = paths.report_json.name
        summary["files"]["report_md"] = paths.report_md.name
    write_reports(paths, summary)
    return summary


def archive_fingerprint(paths: OutputPaths) -> str:
    """返回分卷联合摘要，供 CLI 打印固定摘要行。"""
    files = sorted(paths.archive_dir.glob("seg-*.warc.gz"), key=lambda item: item.name)
    if not files:
        return ""
    return files_fingerprint(files)


def index_fingerprint(paths: OutputPaths) -> str:
    """返回索引文件摘要。"""
    return sha256_file(paths.index_path) if paths.index_path.is_file() else ""
