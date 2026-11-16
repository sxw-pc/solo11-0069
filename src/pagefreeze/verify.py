"""归档完整性核验：结构、摘要、索引定位、revisit 指向与截断。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from .archive_io import ArchiveLoad, load_archive
from .cdx import CdxRow, bind_index_row, read_index
from .errors import PagefreezeError
from .httpmsg import parse_response
from .digest import digest_bytes
from .warc import REVISIT_PROFILE, WarcRecord

_REQUIRED = ("WARC-Type", "WARC-Record-ID", "WARC-Date",
    "WARC-Block-Digest", "WARC-Payload-Digest", "Content-Length",
)


@dataclass(frozen=True)
class VerifyIssue:
    """定位到文件与记录序号的问题。"""
    file: str
    record_index: int | None
    code: str
    message: str


@dataclass(frozen=True)
class VerifyCheck:
    """一项高层检查的汇总状态。"""
    name: str
    status: str
    detail: str
    issue_count: int


@dataclass
class VerifyResult:
    """核验结论与可归档的统计。"""
    status: str
    stats: dict[str, int]
    checks: list[VerifyCheck]
    issues: list[VerifyIssue]


def _issue(issues: list[VerifyIssue], file: str, index: int | None, code: str, message: str) -> None:
    issues.append(VerifyIssue(file=file, record_index=index, code=code, message=message))


def _check_record(record: WarcRecord, algorithm: str, issues: list[VerifyIssue]) -> None:
    for header in _REQUIRED:
        if record.header(header) is None:
            _issue(issues, record.filename, record.record_index, "WARC_HEADER_MISSING",
                   f"missing {header}")
    declared = record.header("Content-Length")
    if declared is not None:
        try:
            if int(declared) != len(record.block):
                _issue(issues, record.filename, record.record_index,
                       "CONTENT_LENGTH_MISMATCH", "Content-Length does not match block")
        except ValueError:
            _issue(issues, record.filename, record.record_index,
                   "CONTENT_LENGTH_MISMATCH", "Content-Length is not an integer")
    block_digest = record.header("WARC-Block-Digest")
    if block_digest and block_digest != digest_bytes(record.block, algorithm):
        _issue(issues, record.filename, record.record_index,
               "BLOCK_DIGEST_MISMATCH", "WARC-Block-Digest does not match block")
    payload_digest = record.header("WARC-Payload-Digest")
    if not payload_digest:
        return
    kind = record.record_type
    try:
        if kind == "warcinfo":
            expected = digest_bytes(record.block, algorithm)
        elif kind == "request":
            expected = digest_bytes(b"", algorithm)
        elif kind == "response":
            expected = digest_bytes(parse_response(record.block).body, algorithm)
        elif kind == "revisit":
            parse_response(record.block)
            expected = payload_digest
        else:
            _issue(issues, record.filename, record.record_index,
                   "WARC_TYPE_UNKNOWN", f"unknown WARC-Type: {kind}")
            return
    except PagefreezeError as exc:
        _issue(issues, record.filename, record.record_index, exc.code, exc.message)
        return
    if payload_digest != expected:
        _issue(issues, record.filename, record.record_index,
               "PAYLOAD_DIGEST_MISMATCH", "WARC-Payload-Digest does not match payload")


def _index_size(loaded: ArchiveLoad) -> dict[str, int]:
    return {segment.file: len(segment.records) for segment in loaded.segments}


def _check_index_row(
    row: CdxRow, record: WarcRecord, algorithm: str,
                     issues: list[VerifyIssue]) -> None:
    # 逐行绑定规则与回放服务共用 cdx.bind_index_row，保证两个入口判定一致。
    for code, message in bind_index_row(row, record):
        _issue(issues, row.filename, record.record_index, code, message)


def _check_index(
    loaded: ArchiveLoad, index_path: Path, algorithm: str,
                 issues: list[VerifyIssue]) -> int:
    rows: list[CdxRow]
    try:
        rows = read_index(index_path)
    except PagefreezeError as exc:
        _issue(issues, index_path.name, 0, exc.code, exc.message)
        return 0
    by_location = {(record.filename, record.offset): record for record in loaded.records}
    expected = [record for record in loaded.records if record.record_type in ("response", "revisit")]
    found: set[tuple[str, int]] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    size_map = _index_size(loaded)
    for row in rows:
        key = (row.urlkey, row.timestamp, row.url)
        if key in seen_keys:
            _issue(issues, index_path.name, 0, "INDEX_DUPLICATE_ENTRY",
                   "duplicate CDX key")
        seen_keys.add(key)
        location = (row.filename, row.offset)
        record = by_location.get(location)
        if record is None:
            _issue(issues, row.filename, size_map.get(row.filename, 0),
                   "INDEX_MISSING_RECORD", "index entry does not locate a record")
            continue
        found.add(location)
        _check_index_row(row, record, algorithm, issues)
    for record in expected:
        if (record.filename, record.offset) not in found:
            _issue(issues, record.filename, record.record_index,
                   "INDEX_ENTRY_MISSING", "response/revisit record has no CDX entry")
    if len(rows) != len(expected):
        _issue(issues, index_path.name, 0, "INDEX_ENTRY_COUNT_MISMATCH",
               "CDX entry count does not match response/revisit count")
    return len(rows)


def _check_revisits(loaded: ArchiveLoad, issues: list[VerifyIssue]) -> None:
    by_id = {record.record_id: record for record in loaded.records if record.record_id}
    for record in loaded.records:
        if record.record_type != "revisit":
            continue
        target_id = record.header("WARC-Refers-To")
        if not target_id:
            _issue(issues, record.filename, record.record_index,
                   "REVISIT_TARGET_MISSING", "revisit lacks WARC-Refers-To")
            continue
        target = by_id.get(target_id)
        if target is None:
            _issue(issues, record.filename, record.record_index,
                   "REVISIT_TARGET_MISSING", "WARC-Refers-To is not in this archive")
            continue
        if record.header("WARC-Payload-Digest") != target.header("WARC-Payload-Digest"):
            _issue(issues, record.filename, record.record_index,
                   "REVISIT_DIGEST_MISMATCH", "revisit payload digest differs from target")
        if (record.header("WARC-Profile") or "") != REVISIT_PROFILE:
            _issue(issues, record.filename, record.record_index,
                   "REVISIT_PROFILE_INVALID", "unexpected WARC-Profile")


def _checks(issues: list[VerifyIssue]) -> list[VerifyCheck]:
    groups = {"archive_structure": {
            "ARCHIVE_EMPTY", "FILE_READ_ERROR", "WARCINFO_MISSING", "WARC_MALFORMED",
            "WARC_HEADER_MISSING", "WARC_TYPE_UNKNOWN", "WARC_RECORD_ID_DUPLICATE",
            "HTTP_MESSAGE_MALFORMED", "GZIP_EMPTY", "GZIP_TRAILING_DATA",
        },
        "truncation": {"GZIP_TRUNCATED", "GZIP_INVALID", "WARC_TRUNCATED"},
        "digests": {"CONTENT_LENGTH_MISMATCH", "BLOCK_DIGEST_MISMATCH",
                    "PAYLOAD_DIGEST_MISMATCH"},
        "index_consistency": {"CDX_MISSING", "CDX_MALFORMED", "CDX_READ_ERROR", "CDX_RECORD_INVALID",
            "INDEX_MISSING_RECORD", "INDEX_ENTRY_MISSING", "INDEX_ENTRY_COUNT_MISMATCH",
            "INDEX_LENGTH_MISMATCH", "INDEX_URL_MISMATCH", "INDEX_URLKEY_MISMATCH",
            "INDEX_TIMESTAMP_MISMATCH", "INDEX_STATUS_MISMATCH", "INDEX_MIME_MISMATCH",
            "INDEX_DIGEST_MISMATCH", "INDEX_RECORD_TYPE_MISMATCH", "INDEX_DUPLICATE_ENTRY",
        },
        "revisit_links": {"REVISIT_TARGET_MISSING", "REVISIT_DIGEST_MISMATCH", "REVISIT_PROFILE_INVALID",
        },
    }
    result: list[VerifyCheck] = []
    for name, codes in groups.items():
        failed = [issue for issue in issues if issue.code in codes]
        status = "fail" if failed else "pass"
        detail = f"{len(failed)} issue(s)" if failed else "ok"
        result.append(VerifyCheck(name=name, status=status, detail=detail, issue_count=len(failed)))
    return result


def verify_archive(archive_dir: Path, index_path: Path, algorithm: str = "sha256") -> VerifyResult:
    """对归档卷与索引做全量交叉核验。"""
    loaded = load_archive(archive_dir)
    issues: list[VerifyIssue] = []
    if not loaded.segments:
        _issue(issues, archive_dir.name, 0, "ARCHIVE_EMPTY", "no .warc.gz segment found")
    for problem in loaded.problems:
        _issue(issues, problem.file, problem.record_index, problem.code, problem.message)
    if loaded.records and loaded.records[0].record_type != "warcinfo":
        _issue(issues, loaded.records[0].filename, loaded.records[0].record_index,
               "WARCINFO_MISSING", "first record is not warcinfo")
    seen_ids: set[str] = set()
    stats = {"segments": len(loaded.segments),
        "records": len(loaded.records),
        "warcinfo": 0,
        "requests": 0,
        "responses": 0,
        "revisits": 0,
        "raw_bytes": 0,
        "gzip_bytes": 0,
    }
    for segment in loaded.segments:
        stats["raw_bytes"] += segment.raw_size
        stats["gzip_bytes"] += segment.gzip_size
    for record in loaded.records:
        kind = record.record_type
        key = {"warcinfo": "warcinfo",
            "request": "requests",
            "response": "responses",
            "revisit": "revisits",
        }.get(kind)
        if key is not None:
            stats[key] = stats.get(key, 0) + 1
        if record.record_id in seen_ids:
            _issue(issues, record.filename, record.record_index,
                   "WARC_RECORD_ID_DUPLICATE", "duplicate WARC-Record-ID")
        seen_ids.add(record.record_id)
        _check_record(record, algorithm, issues)
    index_entries = _check_index(loaded, index_path, algorithm, issues)
    _check_revisits(loaded, issues)
    checks = _checks(issues)
    stats["index_entries"] = index_entries
    status = "pass" if not issues else "fail"
    return VerifyResult(status=status, stats=stats, checks=checks, issues=issues)
