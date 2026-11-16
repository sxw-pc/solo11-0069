"""核验的逐条检查逻辑：记录摘要、CDX 交叉核对与 revisit 指向。

本模块只承载“怎么判定一条引用/一条记录”，流式编排与 :class:`VerifyResult`
组装在 :mod:`pagefreeze.verify`。关系结论按卷覆盖边界三分类：

* 命中已解析记录：存在，继续做摘要/字段一致性核对；
* 落空但相关卷完整结束（gzip 正常 EOF 且 WARC 解析到尾）：确认缺失；
* 可能落在截断卷未读尾部：未判定，不得宣称缺失。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from .cdx import CdxRow, iter_index
from .errors import PagefreezeError
from .httpmsg import mime_type, parse_response
from .policy import normalize_url, urlkey
from .digest import digest_bytes
from .util import parse_warc_date, timestamp14
from .warc import REVISIT_PROFILE, WarcRecord
from .verify_store import VerifyStore

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


def issue(issues: list[VerifyIssue], file: str, index: int | None,
          code: str, message: str) -> None:
    issues.append(VerifyIssue(file=file, record_index=index, code=code, message=message))


def check_record(record: WarcRecord, algorithm: str, issues: list[VerifyIssue]) -> None:
    """重算单条记录的结构、Content-Length 与 block/payload 摘要。"""
    for header in _REQUIRED:
        if record.header(header) is None:
            issue(issues, record.filename, record.record_index, "WARC_HEADER_MISSING",
                  f"missing {header}")
    declared = record.header("Content-Length")
    if declared is not None:
        try:
            if int(declared) != len(record.block):
                issue(issues, record.filename, record.record_index,
                      "CONTENT_LENGTH_MISMATCH", "Content-Length does not match block")
        except ValueError:
            issue(issues, record.filename, record.record_index,
                  "CONTENT_LENGTH_MISMATCH", "Content-Length is not an integer")
    block_digest = record.header("WARC-Block-Digest")
    if block_digest and block_digest != digest_bytes(record.block, algorithm):
        issue(issues, record.filename, record.record_index,
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
            issue(issues, record.filename, record.record_index,
                  "WARC_TYPE_UNKNOWN", f"unknown WARC-Type: {kind}")
            return
    except PagefreezeError as exc:
        issue(issues, record.filename, record.record_index, exc.code, exc.message)
        return
    if payload_digest != expected:
        issue(issues, record.filename, record.record_index,
              "PAYLOAD_DIGEST_MISMATCH", "WARC-Payload-Digest does not match payload")


def record_metadata(
    record: WarcRecord,
) -> tuple[str, str, str, int | None, str, str, str]:
    """提取 CDX 交叉核对所需标量；无法解析的字段保留首个稳定错误码，各字段独立兜底。"""
    target = record.header("WARC-Target-URI") or ""
    date_value = record.header("WARC-Date") or ""
    url = key = stamp = ""
    http_status: int | None = None
    mime = ""
    error_code = error_message = ""

    def capture(exc: PagefreezeError) -> None:
        nonlocal error_code, error_message
        if not error_code:
            error_code, error_message = exc.code, exc.message

    try:
        url = normalize_url(target)
        key = urlkey(url)
    except PagefreezeError as exc:
        capture(exc)
    try:
        stamp = timestamp14(parse_warc_date(date_value))
    except PagefreezeError as exc:
        capture(exc)
    try:
        message = parse_response(record.block)
        http_status = int(message.status or 0)
        mime = mime_type(message.headers)
    except PagefreezeError as exc:
        capture(exc)
    return url, key, stamp, http_status, mime, error_code, error_message


def store_record(store: VerifyStore, seq: int, record: WarcRecord) -> None:
    url, key, stamp, http_status, mime, error_code, error_message = record_metadata(record)
    store.add_record((
        seq,
        record.record_index,
        record.filename,
        record.offset,
        record.length,
        record.record_id,
        record.record_type,
        record.header("WARC-Target-URI") or "",
        key,
        stamp,
        record.header("WARC-Payload-Digest") or "",
        record.header("WARC-Block-Digest") or "",
        record.header("WARC-Refers-To") or "",
        record.header("WARC-Profile") or "",
        http_status,
        mime,
        error_code,
        error_message,
    ))


def _check_index_row(
    row: CdxRow, record: sqlite3.Row, issues: list[VerifyIssue],
) -> None:
    if record["meta_error"]:
        issue(issues, row.filename, record["record_index"],
              record["meta_error"], record["meta_message"])
        return
    comparisons = (
        ("length", row.length, record["length"], "INDEX_LENGTH_MISMATCH"),
        ("url", row.url, record["target_url"], "INDEX_URL_MISMATCH"),
        ("urlkey", row.urlkey, record["urlkey"], "INDEX_URLKEY_MISMATCH"),
        ("timestamp", row.timestamp, record["timestamp"], "INDEX_TIMESTAMP_MISMATCH"),
        ("status", row.status, record["http_status"], "INDEX_STATUS_MISMATCH"),
        ("mime", row.mime, record["mime"], "INDEX_MIME_MISMATCH"),
        ("digest", row.digest, record["payload_digest"], "INDEX_DIGEST_MISMATCH"),
    )
    for _field, row_value, record_value, code in comparisons:
        if row_value != record_value:
            issue(issues, row.filename, record["record_index"], code,
                  f"index {_field} differs from record")
    if record["record_type"] not in ("response", "revisit"):
        issue(issues, row.filename, record["record_index"],
              "INDEX_RECORD_TYPE_MISMATCH", "index points at a non-response record")


def _location_status(store: VerifyStore, filename: str, offset: int) -> str:
    """把 (卷, 偏移) 查表结果分类为 hit / confirmed_missing / unresolved。"""
    record = store.location_lookup(filename, offset)
    if record is not None:
        return "hit"
    coverage = store.segment_coverage(filename)
    if coverage is None:
        # CDX 指向归档中不存在的卷：完整扫描范围里确实没有该记录。
        return "confirmed_missing"
    if coverage["readable"] and coverage["fully_covered"]:
        return "confirmed_missing"
    if offset < coverage["parsed_offset"]:
        # 引用落在已成功解析的前缀范围内却查无此记录，是确定的偏移错误。
        return "confirmed_missing"
    return "unresolved"


def check_index(
    store: VerifyStore, index_path: Path, issues: list[VerifyIssue],
) -> tuple[int, int]:
    """逐行流式核对 CDX；返回 (索引行数, 未判定条目数)。"""
    unresolved = 0
    row_count = 0
    try:
        row_iter = iter_index(index_path)
    except PagefreezeError as exc:
        issue(issues, index_path.name, 0, exc.code, exc.message)
        return 0, 0
    size_map = store.segment_sizes()
    seen_keys: set[tuple[str, str, str]] = set()
    found: set[tuple[str, int]] = set()
    try:
        for row in row_iter:
            row_count += 1
            key = (row.urlkey, row.timestamp, row.url)
            if key in seen_keys:
                issue(issues, index_path.name, 0, "INDEX_DUPLICATE_ENTRY",
                      "duplicate CDX key")
            seen_keys.add(key)
            location = (row.filename, row.offset)
            status = _location_status(store, row.filename, row.offset)
            if status == "unresolved":
                unresolved += 1
                issue(issues, row.filename, size_map.get(row.filename, 0),
                      "INDEX_REFERENCE_UNRESOLVED",
                      "index entry may point into the unread tail of a truncated segment")
                continue
            if status == "confirmed_missing":
                issue(issues, row.filename, size_map.get(row.filename, 0),
                      "INDEX_MISSING_RECORD", "index entry does not locate a record")
                continue
            record = store.location_lookup(row.filename, row.offset)
            assert record is not None
            found.add(location)
            _check_index_row(row, record, issues)
    except PagefreezeError as exc:
        issue(issues, index_path.name, 0, exc.code, exc.message)
        return row_count, unresolved
    for record in store.iter_indexable_records():
        if (record["filename"], record["offset"]) not in found:
            issue(issues, record["filename"], record["record_index"],
                  "INDEX_ENTRY_MISSING", "response/revisit record has no CDX entry")
    # 存在未完整读取的卷时，条目总数差异无法证明索引缺漏，只做逐条结论。
    if not store.has_incomplete_segment() and row_count != store.count_indexable():
        issue(issues, index_path.name, 0, "INDEX_ENTRY_COUNT_MISMATCH",
              "CDX entry count does not match response/revisit count")
    return row_count, unresolved


def check_revisits(store: VerifyStore, issues: list[VerifyIssue]) -> int:
    """核对 revisit 指向；返回未判定的 revisit 数。"""
    unresolved = 0
    incomplete = store.has_incomplete_segment()
    for record in store.iter_revisits():
        target_id = record["refers_to"]
        if not target_id:
            issue(issues, record["filename"], record["record_index"],
                  "REVISIT_TARGET_MISSING", "revisit lacks WARC-Refers-To")
            continue
        target = store.lookup_record_id(target_id)
        if target is None:
            if incomplete:
                unresolved += 1
                issue(issues, record["filename"], record["record_index"],
                      "REVISIT_TARGET_UNRESOLVED",
                      "revisit target may live in the unread tail of a truncated segment")
            else:
                issue(issues, record["filename"], record["record_index"],
                      "REVISIT_TARGET_MISSING", "WARC-Refers-To is not in this archive")
            continue
        if record["payload_digest"] != target["payload_digest"]:
            issue(issues, record["filename"], record["record_index"],
                  "REVISIT_DIGEST_MISMATCH", "revisit payload digest differs from target")
        if record["profile"] != REVISIT_PROFILE:
            issue(issues, record["filename"], record["record_index"],
                  "REVISIT_PROFILE_INVALID", "unexpected WARC-Profile")
    return unresolved


def build_checks(issues: list[VerifyIssue]) -> list[VerifyCheck]:
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
            "INDEX_REFERENCE_UNRESOLVED",
        },
        "revisit_links": {"REVISIT_TARGET_MISSING", "REVISIT_DIGEST_MISMATCH",
            "REVISIT_PROFILE_INVALID", "REVISIT_TARGET_UNRESOLVED",
        },
    }
    result: list[VerifyCheck] = []
    for name, codes in groups.items():
        failed = [item for item in issues if item.code in codes]
        status = "fail" if failed else "pass"
        detail = f"{len(failed)} issue(s)" if failed else "ok"
        result.append(VerifyCheck(name=name, status=status, detail=detail,
            issue_count=len(failed)))
    return result
