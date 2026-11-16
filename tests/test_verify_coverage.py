"""逐卷核验语义：确认缺失/未判定分类、截断前缀确定问题、跨卷 revisit 与内存。

这些用例手工构造多分卷归档，精确控制 gzip EOF 与 WARC 解析边界，验证：
- 完整扫描范围内的悬空引用仍报 confirmed missing；
- 目标可能位于截断卷未读尾部时只能 unresolved，不能宣称缺失；
- 截断不降低已解析前缀中确定的摘要错误；
- 其他完整卷的确定问题照常报告；
- 大正文峰值内存不随分卷数量累计；
- 临时库在正常与异常路径都被清理，问题顺序稳定。
"""
from __future__ import annotations

import glob
import os
import sqlite3
import tempfile
import tracemalloc
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
import pytest
from pagefreeze.cdx import CdxRow, write_index
from pagefreeze.digest import digest_bytes
from pagefreeze.fetch import FetchResult
from pagefreeze.util import gzip_compress
from pagefreeze.verify import verify_archive
from pagefreeze.verify_store import VerificationStore
from pagefreeze.warc import WarcRecord, serialize_record, scan_records
from pagefreeze.warc_build import make_response_record, make_revisit_record, make_warcinfo

_ALGORITHM = "sha256"
_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")
_NOW = datetime(2024, 1, 15, tzinfo=timezone.utc)


def _verify_temp_dirs() -> list[str]:
    return glob.glob(os.path.join(tempfile.gettempdir(), "pagefreeze-verify-*"))


def _hit(url: str, body: bytes) -> FetchResult:
    return FetchResult(url=url, status=200, reason="OK", version="HTTP/1.1",
        headers=(("Content-Type", "text/html; charset=utf-8"),), body=body,
        mime="text/html")


def _response(url: str, body: bytes) -> WarcRecord:
    return make_response_record(_hit(url, body), url.split("//", 1)[1], _NOW,
        _NAMESPACE, _ALGORITHM)


def _revisit(url: str, refers_to: str, payload_digest: str) -> WarcRecord:
    return make_revisit_record(_hit(url, b""), url.split("//", 1)[1], _NOW,
        refers_to, payload_digest, _NAMESPACE, _ALGORITHM)


def _write_segment(directory: Path, number: int, records: list[WarcRecord]) -> Path:
    filename = f"seg-{number:05d}.warc.gz"
    offset = 0
    parts: list[bytes] = []
    for index, record in enumerate(records):
        record.filename = filename
        record.offset = offset
        record.record_index = index
        raw_record = serialize_record(record.headers, record.block)
        record.length = len(raw_record)
        parts.append(raw_record)
        offset += len(raw_record)
    path = directory / filename
    path.write_bytes(gzip_compress(b"".join(parts)))
    return path


def _cdx_rows(records: list[WarcRecord]) -> list[CdxRow]:
    from pagefreeze.cdx import row_from_record
    return [row for record in records if (row := row_from_record(record)) is not None]


def _read_cdx(path: Path) -> list[CdxRow]:
    from pagefreeze.cdx import read_index
    return read_index(path)


def _build_two_segment_archive(directory: Path, extra: WarcRecord | None = None
) -> tuple[WarcRecord, WarcRecord]:
    """卷0 放目标 response T（可选追加 extra），卷1 放引用 T 的 revisit R。"""
    directory.mkdir(parents=True)
    target = _response("http://127.0.0.1/target", b"target body payload")
    warcinfo = make_warcinfo(_NOW, "cfg-digest", _NAMESPACE, _ALGORITHM)
    segment0_records = [warcinfo, target]
    if extra is not None:
        segment0_records.append(extra)
    _write_segment(directory, 0, segment0_records)
    revisit = _revisit("http://127.0.0.1/revisit", target.record_id,
        target.header("WARC-Payload-Digest") or "")
    _write_segment(directory, 1, [revisit])
    index_records = [*segment0_records, revisit]
    write_index(directory / "index.cdx", _cdx_rows(index_records))
    return target, revisit


def _issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_clean_synthetic_archive_passes(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _build_two_segment_archive(archive)
    result = verify_archive(archive, archive / "index.cdx")
    assert result.status == "pass"
    assert result.issues == []
    assert all(item.complete for item in result.coverage)
    assert [item.file for item in result.coverage] == ["seg-00000.warc.gz", "seg-00001.warc.gz"]


def test_dangling_reference_on_complete_scan_is_confirmed_missing(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    target, revisit = _build_two_segment_archive(archive)
    rows = _read_cdx(archive / "index.cdx")
    # 完整卷中插入一条指向不存在偏移的 CDX 行；扫描范围完整，缺失可被证实。
    rows.append(CdxRow(urlkey="com,example)/ghost", timestamp="20240115000000",
        url="http://example.com/ghost", status=200, mime="text/html",
        digest=digest_bytes(b"ghost"), offset=999_999, length=42,
        filename="seg-00001.warc.gz"))
    write_index(archive / "index.cdx", rows)
    result = verify_archive(archive, archive / "index.cdx")
    missing = [issue for issue in result.issues if issue.code == "INDEX_MISSING_RECORD"]
    assert len(missing) == 1
    assert missing[0].file == "seg-00001.warc.gz"
    assert not any(issue.code == "INDEX_RECORD_UNRESOLVED" for issue in result.issues)
    assert result.status == "fail"


def test_unknown_cdx_filename_is_confirmed_missing(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _build_two_segment_archive(archive)
    rows = _read_cdx(archive / "index.cdx")
    rows.append(CdxRow(urlkey="com,example)/ghost", timestamp="20240115000000",
        url="http://example.com/ghost", status=200, mime="text/html",
        digest=digest_bytes(b"ghost"), offset=0, length=42,
        filename="seg-09999.warc.gz"))
    write_index(archive / "index.cdx", rows)
    result = verify_archive(archive, archive / "index.cdx")
    assert any(issue.code == "INDEX_MISSING_RECORD"
        and issue.file == "seg-09999.warc.gz" for issue in result.issues)


def test_truncated_segment_keeps_revisit_unresolved_not_missing(tmp_path: Path) -> None:
    """H1 反例：目标 T 所在早期卷被截断，后续卷 revisit 引用不得被判坏引用。"""
    archive = tmp_path / "archive"
    target, revisit = _build_two_segment_archive(archive)
    segment0 = archive / "seg-00000.warc.gz"
    raw = zlib.decompress(segment0.read_bytes(), 16 + zlib.MAX_WBITS)
    # 在 T 的 record 边界之前截断：T 完全落入未读尾部。
    cut = target.offset + 4
    segment0.write_bytes(gzip_compress(raw[:cut]))
    result = verify_archive(archive, archive / "index.cdx")
    codes = _issue_codes(result)
    assert "REVISIT_TARGET_UNRESOLVED" in codes
    assert "REVISIT_TARGET_MISSING" not in codes
    unresolved = next(issue for issue in result.issues
        if issue.code == "REVISIT_TARGET_UNRESOLVED")
    assert unresolved.file == "seg-00001.warc.gz"
    assert unresolved.record_index == revisit.record_index
    assert any(code in codes for code in
        ("GZIP_TRUNCATED", "WARC_TRUNCATED", "GZIP_INVALID", "WARC_MALFORMED"))
    assert result.status == "fail"
    coverage = next(item for item in result.coverage if item.file == segment0.name)
    assert coverage.complete is False
    assert coverage.parse_offset == target.offset


def test_truncation_does_not_downgrade_deterministic_digest_error(tmp_path: Path) -> None:
    """H3 语义 B：卷在损坏记录之后截断时，已解析前缀中的确定摘要错仍按原分类报告。"""
    archive = tmp_path / "archive"
    trailing = _response("http://127.0.0.1/trailing", b"trailing payload bytes")
    target, revisit = _build_two_segment_archive(archive, extra=trailing)
    segment0 = archive / "seg-00000.warc.gz"
    raw = zlib.decompress(segment0.read_bytes(), 16 + zlib.MAX_WBITS)
    records, problem = scan_records(raw)
    assert problem is None
    tampered = _tamper_payload(records, target)
    rebuilt = raw[:target.offset] + tampered + raw[target.offset + target.length:]
    cut = len(rebuilt) - 4  # 在末尾 trailing 记录内部截断，target 仍完整位于前缀
    assert cut > target.offset + len(tampered)
    segment0.write_bytes(gzip_compress(rebuilt[:cut]))
    result = verify_archive(archive, archive / "index.cdx")
    codes = _issue_codes(result)
    assert "BLOCK_DIGEST_MISMATCH" in codes
    assert "PAYLOAD_DIGEST_MISMATCH" in codes
    assert "REVISIT_TARGET_MISSING" not in codes
    coverage = next(item for item in result.coverage if item.file == segment0.name)
    assert coverage.complete is False


def _tamper_payload(records: list[WarcRecord], target: WarcRecord) -> bytes:
    from pagefreeze.httpmsg import parse_response
    record = next(item for item in records if item.record_id == target.record_id)
    message = parse_response(record.block)
    body_offset = len(record.block) - len(message.body)
    tampered_block = (record.block[:body_offset]
        + bytes([record.block[body_offset] ^ 0x01])
        + record.block[body_offset + 1:])
    return serialize_record(record.headers, tampered_block)


def test_other_segment_errors_survive_a_truncated_segment(tmp_path: Path) -> None:
    """一卷截断不掩盖其他完整卷中已证实的问题（profile 非法等）。"""
    archive = tmp_path / "archive"
    target, revisit = _build_two_segment_archive(archive)
    # 卷0 目标记录完整解析，仅在其后追加一个会被切掉的尾巴，制造截断边界。
    segment0 = archive / "seg-00000.warc.gz"
    raw = zlib.decompress(segment0.read_bytes(), 16 + zlib.MAX_WBITS)
    tail = b"WARC/1.0\r\nX"
    segment0.write_bytes(gzip_compress(raw + tail))
    segment1 = archive / "seg-00001.warc.gz"
    raw1 = zlib.decompress(segment1.read_bytes(), 16 + zlib.MAX_WBITS)
    records1, _ = scan_records(raw1)
    bad = records1[0]
    bad.headers = [(key, value if key != "WARC-Profile" else "wrong-profile")
        for key, value in bad.headers]
    fixed = serialize_record(bad.headers, bad.block)
    segment1.write_bytes(gzip_compress(raw1[:bad.offset] + fixed
        + raw1[bad.offset + bad.length:]))
    result = verify_archive(archive, archive / "index.cdx")
    codes = _issue_codes(result)
    assert "REVISIT_PROFILE_INVALID" in codes
    assert "WARC_MALFORMED" in codes or "WARC_TRUNCATED" in codes


def test_cross_segment_revisit_resolves_when_complete(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    target, revisit = _build_two_segment_archive(archive)
    result = verify_archive(archive, archive / "index.cdx")
    assert result.stats["revisits"] == 1
    revisit_issues = [issue for issue in result.issues
        if issue.code.startswith("REVISIT_")]
    assert revisit_issues == []


def test_duplicate_cdx_key_is_reported(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _build_two_segment_archive(archive)
    rows = _read_cdx(archive / "index.cdx")
    # 直接追加同键行，绕过写入端去重，核验端必须识别重复 key。
    lines = [row.to_line() for row in rows]
    lines.append(rows[0].to_line())
    (archive / "index.cdx").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_archive(archive, archive / "index.cdx")
    assert any(issue.code == "INDEX_DUPLICATE_ENTRY" for issue in result.issues)


def test_malformed_index_does_not_cascade_to_entry_missing(tmp_path: Path) -> None:
    """索引不可读只报读取类错误，不再追加依赖该索引的反向漏列噪声。"""
    archive = tmp_path / "archive"
    _build_two_segment_archive(archive)
    (archive / "index.cdx").write_text("broken index line\n", encoding="utf-8")
    result = verify_archive(archive, archive / "index.cdx")
    codes = _issue_codes(result)
    assert "CDX_MALFORMED" in codes
    assert "INDEX_ENTRY_MISSING" not in codes
    assert "INDEX_ENTRY_COUNT_MISMATCH" not in codes
    assert result.status == "fail"


def test_temporary_store_is_cleaned_on_success_and_failure(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    _build_two_segment_archive(archive)
    verify_archive(archive, archive / "index.cdx")
    assert _verify_temp_dirs() == []
    real_connect = sqlite3.connect

    def boom(path, *args, **kwargs):
        if str(path).endswith("verify.sqlite3"):
            raise sqlite3.OperationalError("injected store failure")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", boom)
    with pytest.raises(Exception):
        verify_archive(archive, archive / "index.cdx")
    assert _verify_temp_dirs() == []


def test_issue_order_is_stable_across_runs(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    target, revisit = _build_two_segment_archive(archive)
    segment0 = archive / "seg-00000.warc.gz"
    raw = zlib.decompress(segment0.read_bytes(), 16 + zlib.MAX_WBITS)
    segment0.write_bytes(gzip_compress(raw[:target.offset + 4]))
    first = verify_archive(archive, archive / "index.cdx")
    second = verify_archive(archive, archive / "index.cdx")
    assert [(i.file, i.record_index, i.code) for i in first.issues] == \
        [(i.file, i.record_index, i.code) for i in second.issues]


def test_peak_memory_does_not_grow_with_segment_count(tmp_path: Path) -> None:
    """大正文分卷越多，逐卷核验的峰值内存应趋于平台，而非随全量正文线性累计。"""
    body = os.urandom(128 * 1024)  # 不可压缩：gzip 后体量与正文同量级

    def build(directory: Path, count: int) -> None:
        directory.mkdir()
        records: list[WarcRecord] = []
        for number in range(count):
            record = _response(f"http://127.0.0.1/{number}", body + os.urandom(16))
            _write_segment(directory, number, [record])
            records.append(record)
        write_index(directory / "index.cdx", _cdx_rows(records))

    def peak(directory: Path) -> int:
        tracemalloc.start()
        verify_archive(directory, directory / "index.cdx")
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return peak_bytes

    peaks: dict[int, int] = {}
    raw_totals: dict[int, int] = {}
    for count in (2, 4, 8):
        archive_dir = tmp_path / f"archive-{count}"
        build(archive_dir, count)
        peaks[count] = peak(archive_dir)
        raw_totals[count] = sum(item.stat().st_size
            for item in archive_dir.glob("seg-*.warc.gz"))
    # 正文总量翻倍但峰值保持平台：8 卷峰值相对 4 卷的增量远小于新增 4 卷正文。
    added_payload = 4 * len(body)
    assert peaks[8] - peaks[4] < added_payload // 4
    # 8 卷的原始体量必须显著大于其核验峰值，证明正文没有全部驻留内存。
    assert raw_totals[8] > peaks[8] * 1.5
    # 全部峰值保持同一量级（平台），不随卷数发散。
    assert peaks[8] < max(peaks.values()) * 1.25


def test_verification_store_context_closes_even_when_unused() -> None:
    with VerificationStore() as store:
        assert store.path.parent.is_dir()
    assert not _verify_temp_dirs()


def test_coverage_reaches_json_and_markdown(tmp_path: Path) -> None:
    from pagefreeze.verify_report import result_to_dict, write_verify_files
    archive = tmp_path / "archive"
    target, _ = _build_two_segment_archive(archive)
    segment0 = archive / "seg-00000.warc.gz"
    raw = zlib.decompress(segment0.read_bytes(), 16 + zlib.MAX_WBITS)
    segment0.write_bytes(gzip_compress(raw[:target.offset + 4]))
    result = verify_archive(archive, archive / "index.cdx")
    payload = result_to_dict(result)
    assert payload["coverage"][0]["complete"] is False
    assert payload["coverage"][0]["parse_offset"] == target.offset
    json_path, md_path = tmp_path / "verify.json", tmp_path / "verify.md"
    write_verify_files(result, json_path, md_path)
    markdown = md_path.read_text(encoding="utf-8")
    assert "分卷覆盖" in markdown
    assert "截断" in markdown
    assert "REVISIT_TARGET_UNRESOLVED" in markdown
