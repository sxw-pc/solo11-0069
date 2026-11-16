"""归档完整性核验编排：逐卷流式扫描、覆盖边界与结论组装。

核验按卷流式进行：每卷解析后只把轻量元数据写入临时 SQLite（见
:mod:`pagefreeze.verify_store`），完整记录与正文随卷释放，峰值内存不随卷数累计。
逐条判定逻辑在 :mod:`pagefreeze.verify_checks`，关系结论按卷覆盖边界分类（语义 B）：

* 引用命中已解析记录：存在，可继续做摘要/字段一致性核对；
* 引用落空但相关卷已完整结束（gzip 正常 EOF 且 WARC 解析到尾）：确认缺失，
  保留原有 ``INDEX_MISSING_RECORD`` / ``REVISIT_TARGET_MISSING`` 分类；
* 引用可能落在截断卷未读尾部：仅报 ``*_UNRESOLVED``，不得宣称缺失。

损坏卷已解析前缀中的确定错误（摘要、字段不一致等）照常报告，不降级为未知。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from .archive_io import iter_segments
from .warc import WarcRecord
from .verify_store import VerifyStore
from .verify_checks import (
    VerifyCheck,
    VerifyIssue,
    build_checks,
    check_index,
    check_record,
    check_revisits,
    issue,
    store_record,
)

__all__ = ["VerifyIssue", "VerifyCheck", "VerifyResult", "verify_archive"]


@dataclass
class VerifyResult:
    """核验结论与可归档的统计。"""
    status: str
    stats: dict[str, int]
    checks: list[VerifyCheck]
    issues: list[VerifyIssue]


def verify_archive(archive_dir: Path, index_path: Path, algorithm: str = "sha256") -> VerifyResult:
    """对归档卷与索引做全量交叉核验，逐卷流式处理并按覆盖边界下结论。"""
    issues: list[VerifyIssue] = []
    stats = {"segments": 0,
        "records": 0,
        "warcinfo": 0,
        "requests": 0,
        "responses": 0,
        "revisits": 0,
        "truncated_segments": 0,
        "unresolved": 0,
        "raw_bytes": 0,
        "gzip_bytes": 0,
    }
    segment_count = 0
    first_record: WarcRecord | None = None
    seen_ids: set[str] = set()
    with VerifyStore() as store:
        for seq, segment in enumerate(iter_segments(archive_dir)):
            segment_count += 1
            stats["raw_bytes"] += segment.raw_size
            stats["gzip_bytes"] += segment.gzip_size
            if not segment.fully_covered:
                stats["truncated_segments"] += 1
            store.add_segment(
                seq=seq,
                filename=segment.file,
                gzip_size=segment.gzip_size,
                raw_size=segment.raw_size,
                record_count=len(segment.records),
                gzip_complete=segment.gzip_complete,
                readable=segment.readable,
                parsed_offset=segment.parsed_offset,
                fully_covered=segment.fully_covered,
            )
            for problem in segment.problems:
                issue(issues, problem.file, problem.record_index, problem.code, problem.message)
            for record in segment.records:
                if first_record is None:
                    first_record = record
                if record.record_id in seen_ids:
                    issue(issues, record.filename, record.record_index,
                          "WARC_RECORD_ID_DUPLICATE", "duplicate WARC-Record-ID")
                seen_ids.add(record.record_id)
                check_record(record, algorithm, issues)
                store_record(store, seq, record)
        store.commit()
        stats["segments"] = segment_count
        stats["records"] = store.count_records()
        type_counts = store.type_counts()
        stats["warcinfo"] = type_counts.get("warcinfo", 0)
        stats["requests"] = type_counts.get("request", 0)
        stats["responses"] = type_counts.get("response", 0)
        stats["revisits"] = type_counts.get("revisit", 0)
        if segment_count == 0:
            issue(issues, archive_dir.name, 0, "ARCHIVE_EMPTY", "no .warc.gz segment found")
        elif first_record is not None and first_record.record_type != "warcinfo":
            issue(issues, first_record.filename, first_record.record_index,
                  "WARCINFO_MISSING", "first record is not warcinfo")
        index_entries, index_unresolved = check_index(store, index_path, issues)
        revisit_unresolved = check_revisits(store, issues)
    stats["index_entries"] = index_entries
    stats["unresolved"] = index_unresolved + revisit_unresolved
    checks = build_checks(issues)
    # 既有 pass/fail 语义不变：有截断、未判定引用或确认错误时一律不得 pass。
    status = "pass" if not issues else "fail"
    return VerifyResult(status=status, stats=stats, checks=checks, issues=issues)
