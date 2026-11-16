"""归档完整性核验：结构、摘要、索引定位、revisit 指向与截断。

核验逐卷进行：一卷读完即做记录级检查并只把轻量元数据写入临时 SQLite，正文随卷
释放，峰值内存不随分卷数量累计。跨卷关系（CDX 定位、revisit 回指）结合每卷的
gzip EOF 与 WARC 解析边界分类：完整扫描范围内查不到才是 confirmed missing；
目标可能落入截断卷未读尾部时记为 unresolved，绝不宣称已证实缺失。
"""
from __future__ import annotations

from pathlib import Path
from .archive_io import iter_segments
from .cdx import iter_index
from .errors import PagefreezeError
from .httpmsg import mime_type, parse_response
from .policy import normalize_url, urlkey
from .digest import digest_bytes
from .util import parse_warc_date, timestamp14
from .warc import REVISIT_PROFILE, WarcRecord
from .verify_checks import build_checks
from .verify_store import VerificationStore
from .verify_types import SegmentCoverage, VerifyCheck, VerifyIssue, VerifyResult

__all__ = ("SegmentCoverage", "VerifyCheck", "VerifyIssue", "VerifyResult",
    "verify_archive")

_REQUIRED = ("WARC-Type", "WARC-Record-ID", "WARC-Date",
    "WARC-Block-Digest", "WARC-Payload-Digest", "Content-Length",
)
_INDEX_TYPES = ("response", "revisit")


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


def _record_metadata(record: WarcRecord) -> dict[str, object]:
    """预计算 CDX 对照所需字段；HTTP/日期解析失败也稳定留痕，正文不离开本卷。"""
    row: dict[str, object] = {
        "file": record.filename, "record_index": record.record_index,
        "offset": record.offset, "length": record.length,
        "record_id": record.record_id, "record_type": record.record_type,
        "payload_digest": record.header("WARC-Payload-Digest") or "",
        "block_digest": record.header("WARC-Block-Digest") or "",
        "refers_to": record.header("WARC-Refers-To") or "",
        "profile": record.header("WARC-Profile") or "",
        "target_uri": record.header("WARC-Target-URI") or "",
        "warc_date": record.header("WARC-Date") or "",
        "http_valid": 0, "http_status": None, "http_mime": None,
        "normalized_url": None, "urlkey": None, "ts14": None,
        "error_code": None, "error_message": None,
    }
    try:
        message = parse_response(record.block)
        normalized = normalize_url(row["target_uri"])  # type: ignore[arg-type]
        parsed_date = parse_warc_date(row["warc_date"])  # type: ignore[arg-type]
    except PagefreezeError as exc:
        row["error_code"] = exc.code
        row["error_message"] = exc.message
        return row
    row.update(http_valid=1, http_status=int(message.status or 0),
        http_mime=mime_type(message.headers), normalized_url=normalized,
        urlkey=urlkey(normalized), ts14=timestamp14(parsed_date))
    return row


def _check_index_row(row, meta: dict[str, object], issues: list[VerifyIssue]) -> None:
    if row.length != meta["length"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_LENGTH_MISMATCH", "index length differs from record length")
    if not meta["http_valid"]:
        _issue(issues, row.filename, meta["record_index"],
               str(meta["error_code"]), str(meta["error_message"]))
        return
    if row.url != meta["normalized_url"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_URL_MISMATCH", "index URL differs from record")
    if row.urlkey != meta["urlkey"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_URLKEY_MISMATCH", "index urlkey differs from record")
    if row.timestamp != meta["ts14"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_TIMESTAMP_MISMATCH", "index timestamp differs from record")
    if row.status != meta["http_status"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_STATUS_MISMATCH", "index status differs from HTTP block")
    if row.mime != meta["http_mime"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_MIME_MISMATCH", "index MIME differs from HTTP block")
    if row.digest != meta["payload_digest"]:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_DIGEST_MISMATCH", "index digest differs from record")
    if meta["record_type"] not in _INDEX_TYPES:
        _issue(issues, row.filename, meta["record_index"],
               "INDEX_RECORD_TYPE_MISMATCH", "index points at a non-response record")


def _classify_location(store: VerificationStore, row) -> tuple[str, dict[str, object] | None]:
    """把一次 CDX 定位分为 found / confirmed missing / 可能在未读尾部。"""
    meta = store.location(row.filename, row.offset)
    if meta is not None:
        return "found", meta
    segment = store.segment(row.filename)
    if segment is not None and not segment["complete"] and row.offset >= segment["parse_offset"]:
        return "unresolved", segment
    return "missing", segment


def _check_index(
    store: VerificationStore, index_path: Path, issues: list[VerifyIssue],
) -> int:
    found: set[tuple[str, int]] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    entry_count = 0
    try:
        rows = iter_index(index_path)
        for row in rows:
            entry_count += 1
            key = (row.urlkey, row.timestamp, row.url)
            if key in seen_keys:
                _issue(issues, index_path.name, 0, "INDEX_DUPLICATE_ENTRY",
                       "duplicate CDX key")
            seen_keys.add(key)
            kind, meta = _classify_location(store, row)
            if kind == "found":
                assert meta is not None
                found.add((row.filename, row.offset))
                _check_index_row(row, meta, issues)
            elif kind == "unresolved":
                assert meta is not None
                _issue(issues, row.filename, meta["record_count"],
                       "INDEX_RECORD_UNRESOLVED",
                       "index entry may point beyond the parsed segment boundary")
            else:
                record_index = meta["record_count"] if meta is not None else 0
                _issue(issues, row.filename, record_index,
                       "INDEX_MISSING_RECORD", "index entry does not locate a record")
    except PagefreezeError as exc:
        # 索引本身不可读：只报读取错误，不再做依赖该索引的反向/计数核对。
        _issue(issues, index_path.name, 0, exc.code, exc.message)
        return entry_count
    expected = store.indexable_locations()
    for file, offset in sorted(expected):
        if (file, offset) not in found:
            meta = store.location(file, offset)
            record_index = meta["record_index"] if meta else None
            _issue(issues, file, record_index,
                   "INDEX_ENTRY_MISSING", "response/revisit record has no CDX entry")
    if not store.has_incomplete_segment() and entry_count != len(expected):
        _issue(issues, index_path.name, 0, "INDEX_ENTRY_COUNT_MISMATCH",
               "CDX entry count does not match response/revisit count")
    return entry_count


def _check_revisits(store: VerificationStore, issues: list[VerifyIssue]) -> None:
    incomplete = store.has_incomplete_segment()
    for record in store.revisits():
        target_id = record["refers_to"]
        if not target_id:
            _issue(issues, record["file"], record["record_index"],
                   "REVISIT_TARGET_MISSING", "revisit lacks WARC-Refers-To")
            continue
        target = store.by_id(target_id)
        if target is None:
            if incomplete:
                _issue(issues, record["file"], record["record_index"],
                       "REVISIT_TARGET_UNRESOLVED",
                       "revisit target may lie beyond a parsed segment boundary")
            else:
                _issue(issues, record["file"], record["record_index"],
                       "REVISIT_TARGET_MISSING",
                       "WARC-Refers-To is not in this archive")
            continue
        if record["payload_digest"] != target["payload_digest"]:
            _issue(issues, record["file"], record["record_index"],
                   "REVISIT_DIGEST_MISMATCH", "revisit payload digest differs from target")
        if record["profile"] != REVISIT_PROFILE:
            _issue(issues, record["file"], record["record_index"],
                   "REVISIT_PROFILE_INVALID", "unexpected WARC-Profile")


def verify_archive(archive_dir: Path, index_path: Path, algorithm: str = "sha256") -> VerifyResult:
    """对归档卷与索引做逐卷流式全量交叉核验。"""
    issues: list[VerifyIssue] = []
    stats = {"segments": 0, "records": 0, "warcinfo": 0, "requests": 0,
        "responses": 0, "revisits": 0, "raw_bytes": 0, "gzip_bytes": 0}
    seen_ids: set[str] = set()
    first_location: tuple[str, int] | None = None
    first_is_warcinfo = False
    with VerificationStore() as store:
        for segment in iter_segments(archive_dir):
            stats["segments"] += 1
            stats["raw_bytes"] += segment.raw_size
            stats["gzip_bytes"] += segment.gzip_size
            for problem in segment.problems:
                _issue(issues, problem.file, problem.record_index, problem.code, problem.message)
            segment_seq = store.add_segment(segment)
            for record in segment.records:
                if first_location is None:
                    # 只留轻量定位与类型，不持有首条记录的正文。
                    first_location = (record.filename, record.record_index)
                    first_is_warcinfo = record.record_type == "warcinfo"
                kind = record.record_type
                stats_key = {"warcinfo": "warcinfo", "request": "requests",
                    "response": "responses", "revisit": "revisits"}.get(kind)
                if stats_key is not None:
                    stats[stats_key] += 1
                stats["records"] += 1
                if record.record_id in seen_ids:
                    _issue(issues, record.filename, record.record_index,
                           "WARC_RECORD_ID_DUPLICATE", "duplicate WARC-Record-ID")
                seen_ids.add(record.record_id)
                _check_record(record, algorithm, issues)
                store.add_record(segment_seq, _record_metadata(record))
            store.commit()
        if stats["segments"] == 0:
            _issue(issues, archive_dir.name, 0, "ARCHIVE_EMPTY", "no .warc.gz segment found")
        if first_location is not None and not first_is_warcinfo:
            _issue(issues, first_location[0], first_location[1],
                   "WARCINFO_MISSING", "first record is not warcinfo")
        index_entries = _check_index(store, index_path, issues)
        _check_revisits(store, issues)
        coverage = [SegmentCoverage(file=item["file"], gzip_eof=bool(item["gzip_eof"]),
            parse_offset=item["parse_offset"], raw_size=item["raw_size"],
            complete=bool(item["complete"]), record_count=item["record_count"])
            for item in store.segments()]
    checks = build_checks(issues)
    stats["index_entries"] = index_entries
    status = "pass" if not issues else "fail"
    return VerifyResult(status=status, stats=stats, checks=checks,
        issues=issues, coverage=coverage)
