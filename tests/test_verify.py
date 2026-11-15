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


def test_deleted_record_leaves_dangling_index(archive_copy: Path) -> None:
    segment = sorted(archive_copy.glob("seg-*.warc.gz"))[-1]
    raw = _raw(segment)
    records, problem = scan_records(raw)
    assert problem is None
    last = records[-1]
    segment.write_bytes(gzip_compress(raw[:last.offset]))
    result = verify_archive(archive_copy, archive_copy / "index.cdx")
    assert result.status == "fail"
    target = next(issue for issue in result.issues if issue.code == "INDEX_MISSING_RECORD")
    assert target.file == segment.name
    assert isinstance(target.record_index, int)
