"""完整性核验：正常通过，以及截断、篡改、删除三条损坏路径。"""
from __future__ import annotations

import zlib
from pathlib import Path
from pagefreeze.httpmsg import parse_response
from pagefreeze.util import gzip_compress
from pagefreeze.verify import verify_archive
from pagefreeze.warc import parse_records, scan_records, serialize_record


def _raw(path: Path) -> bytes:
    return zlib.decompress(path.read_bytes(), 16 + zlib.MAX_WBITS)


def test_clean_archive_passes(archive_copy: Path) -> None:
    result = verify_archive(archive_copy, archive_copy / "index.cdx")
    assert result.status == "pass"
    assert result.issues == []
    assert all(check.status == "pass" for check in result.checks)


def test_truncated_gzip_is_located(archive_copy: Path) -> None:
    segment = sorted(archive_copy.glob("seg-*.warc.gz"))[-1]
    segment.write_bytes(segment.read_bytes()[:-8])
    result = verify_archive(archive_copy, archive_copy / "index.cdx")
    assert result.status == "fail"
    target = next(issue for issue in result.issues if issue.code == "GZIP_TRUNCATED")
    assert target.file == segment.name
    assert isinstance(target.record_index, int)
    codes = {issue.code for issue in result.issues}
    assert "INDEX_MISSING_RECORD" not in codes
    coverage = next(item for item in result.coverage if item.file == segment.name)
    assert coverage.gzip_eof is False
    assert coverage.complete is False
    # 仅去掉 gzip trailer 时 WARC 字节仍完整，已解析记录不应产生悬空或未判定结论。
    assert "INDEX_RECORD_UNRESOLVED" not in codes
    assert result.stats["records"] > 0


def test_truncated_gzip_stream_leaves_unresolved_tail(archive_copy: Path) -> None:
    """gzip 流本身被截短、丢失尾部 WARC 内容时，未读尾部只能未判定。"""
    segment = sorted(archive_copy.glob("seg-*.warc.gz"))[-1]
    payload = segment.read_bytes()
    segment.write_bytes(payload[:len(payload) // 2])  # 砍掉流中段，真实丢失尾部记录
    result = verify_archive(archive_copy, archive_copy / "index.cdx")
    assert result.status == "fail"
    codes = {issue.code for issue in result.issues}
    assert "GZIP_TRUNCATED" in codes or "GZIP_INVALID" in codes
    assert "INDEX_MISSING_RECORD" not in codes
    assert "INDEX_RECORD_UNRESOLVED" in codes
    coverage = next(item for item in result.coverage if item.file == segment.name)
    assert coverage.complete is False
    assert coverage.parse_offset < coverage.raw_size


def test_one_byte_tamper_breaks_digest(archive_copy: Path) -> None:
    segment = sorted(archive_copy.glob("seg-*.warc.gz"))[0]
    raw = _raw(segment)
    records = parse_records(raw)
    record = next(item for item in records if item.record_type == "response")
    message = parse_response(record.block)
    body_offset = len(record.block) - len(message.body)
    tampered_block = (record.block[:body_offset]
        + bytes([record.block[body_offset] ^ 0x01])
        + record.block[body_offset + 1:])
    tampered = serialize_record(record.headers, tampered_block)
    segment.write_bytes(gzip_compress(raw[:record.offset] + tampered + raw[record.offset + record.length:]))
    result = verify_archive(archive_copy, archive_copy / "index.cdx")
    assert result.status == "fail"
    codes = {issue.code for issue in result.issues}
    assert "BLOCK_DIGEST_MISMATCH" in codes
    target = next(issue for issue in result.issues if issue.code == "BLOCK_DIGEST_MISMATCH")
    assert target.file == segment.name
    assert target.record_index == record.record_index


def test_deleted_record_leaves_unresolved_tail(archive_copy: Path) -> None:
    """卷尾记录被切坏时 WARC 解析停在边界：未读尾部不能被当作确认缺失。"""
    segment = sorted(archive_copy.glob("seg-*.warc.gz"))[-1]
    raw = _raw(segment)
    records, problem = scan_records(raw)
    assert problem is None
    last = records[-1]
    # 在末条记录内部截断：解析器只能给出此前缀，剩余部分属于未验证尾部。
    segment.write_bytes(gzip_compress(raw[:last.offset + last.length // 2]))
    result = verify_archive(archive_copy, archive_copy / "index.cdx")
    assert result.status == "fail"
    codes = {issue.code for issue in result.issues}
    assert "INDEX_RECORD_UNRESOLVED" in codes
    assert "INDEX_MISSING_RECORD" not in codes
    unresolved = next(issue for issue in result.issues
        if issue.code == "INDEX_RECORD_UNRESOLVED")
    assert unresolved.file == segment.name
    coverage = next(item for item in result.coverage if item.file == segment.name)
    assert coverage.complete is False
    assert coverage.parse_offset == last.offset
