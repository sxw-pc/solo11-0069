"""流式核验的覆盖边界语义与峰值内存。

覆盖：
* 完整卷上的真实悬空引用仍报 confirmed missing；
* 截断卷未读尾部可能存在的目标只报 unresolved，不宣称缺失；
* 损坏卷已解析前缀中的确定 digest 错误不降级；
* 其他完整卷中的确定问题照常报告；
* 跨卷 revisit、CDX 重复 key、临时库异常清理与稳定顺序；
* 大正文峰值内存不随卷数累计。
"""
from __future__ import annotations

import sqlite3
import tempfile
import tracemalloc
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pagefreeze import verify_store as verify_store_module
from pagefreeze.archive_io import load_segment
from pagefreeze.cdx import CdxRow, write_index
from pagefreeze.digest import digest_bytes
from pagefreeze.httpmsg import build_response
from pagefreeze.policy import urlkey
from pagefreeze.util import gzip_compress, timestamp14, warc_date
from pagefreeze.verify import verify_archive
from pagefreeze.verify_store import VerifyStore
from pagefreeze.warc import WarcRecord, serialize_record

NAMESPACE = uuid.UUID("6f1c5a9e-2d47-5b83-8a10-3c4d5e6f7a8b")
ALGORITHM = "sha256"
MOMENT = datetime(2024, 1, 15, tzinfo=timezone.utc)
STAMP = timestamp14(MOMENT)


def _record_urn(url_key: str, kind: str) -> str:
    seed = f"{url_key}\n{STAMP}\n{kind}"
    return f"<urn:uuid:{uuid.uuid5(NAMESPACE, seed)}>"


def _make_response(url: str, body: bytes) -> WarcRecord:
    block = build_response(200, "OK", "HTTP/1.1",
        (("Content-Type", "text/html; charset=utf-8"),), body)
    payload_digest = digest_bytes(body, ALGORITHM)
    headers = [
        ("WARC-Type", "response"),
        ("WARC-Record-ID", _record_urn(urlkey(url), "response")),
        ("WARC-Date", warc_date(MOMENT)),
        ("WARC-Target-URI", url),
        ("WARC-Block-Digest", digest_bytes(block, ALGORITHM)),
        ("WARC-Payload-Digest", payload_digest),
        ("Content-Type", "application/http; msgtype=response"),
        ("Content-Length", str(len(block))),
    ]
    return WarcRecord(headers=headers, block=block)


def _make_request(url: str) -> WarcRecord:
    block = (f"GET / HTTP/1.1\r\nHost: example.test\r\n\r\n").encode("latin-1")
    headers = [
        ("WARC-Type", "request"),
        ("WARC-Record-ID", _record_urn(urlkey(url), "request")),
        ("WARC-Date", warc_date(MOMENT)),
        ("WARC-Target-URI", url),
        ("WARC-Block-Digest", digest_bytes(block, ALGORITHM)),
        ("WARC-Payload-Digest", digest_bytes(b"", ALGORITHM)),
        ("Content-Type", "application/http; msgtype=request"),
        ("Content-Length", str(len(block))),
    ]
    return WarcRecord(headers=headers, block=block)


def _make_revisit(url: str, target_id: str, payload_digest: str,
                  profile: str = "identical-payload-digest") -> WarcRecord:
    block = build_response(200, "OK", "HTTP/1.1",
        (("Content-Type", "text/html; charset=utf-8"),), b"")
    headers = [
        ("WARC-Type", "revisit"),
        ("WARC-Record-ID", _record_urn(urlkey(url), "revisit")),
        ("WARC-Date", warc_date(MOMENT)),
        ("WARC-Target-URI", url),
        ("WARC-Block-Digest", digest_bytes(block, ALGORITHM)),
        ("WARC-Payload-Digest", payload_digest),
        ("WARC-Refers-To", target_id),
        ("WARC-Profile", profile),
        ("Content-Type", "application/http; msgtype=response"),
        ("Content-Length", str(len(block))),
    ]
    return WarcRecord(headers=headers, block=block)


def _make_warcinfo() -> WarcRecord:
    block = b"software: pagefreeze-test\n"
    headers = [
        ("WARC-Type", "warcinfo"),
        ("WARC-Record-ID", _record_urn("warcinfo", "warcinfo")),
        ("WARC-Date", warc_date(MOMENT)),
        ("WARC-Block-Digest", digest_bytes(block, ALGORITHM)),
        ("WARC-Payload-Digest", digest_bytes(block, ALGORITHM)),
        ("Content-Type", "application/warc-fields"),
        ("Content-Length", str(len(block))),
    ]
    return WarcRecord(headers=headers, block=block)


def _write_segment(archive_dir: Path, index: int, records: list[WarcRecord]) -> Path:
    raw_parts: list[bytes] = []
    offset = 0
    filename = f"seg-{index:05d}.warc.gz"
    for record_index, record in enumerate(records):
        record.filename = filename
        record.offset = offset
        record.record_index = record_index
        raw = serialize_record(record.headers, record.block)
        record.length = len(raw)
        raw_parts.append(raw)
        offset += len(raw)
    path = archive_dir / filename
    archive_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip_compress(b"".join(raw_parts)))
    return path


def _cdx_row(record: WarcRecord) -> CdxRow:
    return CdxRow(urlkey=urlkey(record.header("WARC-Target-URI") or ""),
        timestamp=STAMP,
        url=record.header("WARC-Target-URI") or "",
        status=200,
        mime="text/html",
        digest=record.header("WARC-Payload-Digest") or "",
        offset=record.offset,
        length=record.length,
        filename=record.filename,
    )


def _write_index_for(archive_dir: Path, records: list[WarcRecord],
                     extra: list[CdxRow] | None = None) -> Path:
    rows = [_cdx_row(record) for record in records
            if record.record_type in ("response", "revisit")]
    if extra:
        rows.extend(extra)
    path = archive_dir / "index.cdx"
    write_index(path, rows)
    return path


def _raw_of(path: Path) -> bytes:
    import zlib
    return zlib.decompress(path.read_bytes(), 16 + zlib.MAX_WBITS)


def _cut_gzip_bytes(path: Path, keep_fraction: float = 0.8) -> None:
    """直接截断 gzip 字节流：gzip EOF 与 WARC 解析都停在记录中部。"""
    compressed = path.read_bytes()
    path.write_bytes(compressed[: int(len(compressed) * keep_fraction)])


def _cut_raw_mid_record(path: Path, cut_at: int) -> None:
    """在 WARC 字节流中部切开后重新压缩：gzip 完整 EOF，但 WARC 只解析出前缀。"""
    path.write_bytes(gzip_compress(_raw_of(path)[:cut_at]))


def _build_two_segment_archive(archive_dir: Path) -> tuple[list[WarcRecord], list[WarcRecord]]:
    """seg-0 含 warcinfo/request/response(T)；seg-1 含 request/revisit(R->T)。"""
    target = _make_response("http://example.test/target", b"target-body" * 16)
    revisit = _make_revisit("http://example.test/revisit", target.record_id,
        target.header("WARC-Payload-Digest") or "")
    first = [_make_warcinfo(), _make_request("http://example.test/target"), target]
    second = [_make_request("http://example.test/revisit"), revisit]
    _write_segment(archive_dir, 0, first)
    _write_segment(archive_dir, 1, second)
    return first, second


def _codes(result: object) -> set[str]:
    return {issue.code for issue in result.issues}  # type: ignore[attr-defined]


def test_segment_load_records_gzip_eof_and_warc_boundary(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, _ = _build_two_segment_archive(archive_dir)
    intact = load_segment(archive_dir / "seg-00000.warc.gz")
    assert intact.fully_covered is True
    assert intact.gzip_complete is True
    assert intact.parsed_offset == intact.raw_size
    target = first[-1]

    # 情况一：直接截断 gzip 字节流，gzip EOF 与 WARC 解析都停在记录中部。
    gzip_path = archive_dir / "seg-00000.warc.gz"
    _cut_gzip_bytes(gzip_path)
    cut_gzip = load_segment(gzip_path)
    assert cut_gzip.fully_covered is False
    assert cut_gzip.gzip_complete is False
    assert cut_gzip.parsed_offset < cut_gzip.raw_size
    assert {problem.code for problem in cut_gzip.problems} <= {
        "GZIP_TRUNCATED", "GZIP_INVALID", "WARC_TRUNCATED"}

    # 情况二：在 T 的记录中部切开后重新压缩，gzip 完整 EOF，但 WARC 只解析出前缀。
    _write_segment(archive_dir, 0, first)
    raw_path = archive_dir / "seg-00000.warc.gz"
    mid_record_cut = target.offset + max(1, target.length // 2)
    _cut_raw_mid_record(raw_path, mid_record_cut)
    cut_warc = load_segment(raw_path)
    assert cut_warc.fully_covered is False
    assert cut_warc.gzip_complete is True
    assert cut_warc.parsed_offset == target.offset
    assert [record.record_id for record in cut_warc.records] == [
        record.record_id for record in first[:-1]]
    assert {problem.code for problem in cut_warc.problems} <= {"WARC_TRUNCATED"}


def test_dangling_references_in_complete_archive_are_confirmed_missing(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    ghost = CdxRow(urlkey="com,example)/ghost", timestamp=STAMP,
        url="http://example.test/ghost", status=200, mime="text/html",
        digest="sha256:" + "0" * 64, offset=999999, length=42,
        filename="seg-00001.warc.gz")
    index_path = _write_index_for(archive_dir, first + second, extra=[ghost])
    result = verify_archive(archive_dir, index_path)
    assert result.status == "fail"
    missing = next(issue for issue in result.issues
                   if issue.code == "INDEX_MISSING_RECORD")
    assert missing.file == "seg-00001.warc.gz"
    assert "INDEX_REFERENCE_UNRESOLVED" not in _codes(result)
    assert result.stats["unresolved"] == 0
    assert result.stats["truncated_segments"] == 0


def test_revisit_to_unknown_id_in_complete_archive_is_confirmed_missing(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    unknown_revisit = _make_revisit("http://example.test/other",
        "<urn:uuid:does-not-exist>", "sha256:" + "1" * 64)
    # 替换 seg-1 的 revisit，使所有卷仍完整结束。
    second[1] = unknown_revisit
    _write_segment(archive_dir, 1, second)
    index_path = _write_index_for(archive_dir, first + second)
    result = verify_archive(archive_dir, index_path)
    issue = next(item for item in result.issues if item.code == "REVISIT_TARGET_MISSING")
    assert issue.file == "seg-00001.warc.gz"
    assert "REVISIT_TARGET_UNRESOLVED" not in _codes(result)


def test_h1_truncated_target_tail_marks_references_unresolved(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    index_path = _write_index_for(archive_dir, first + second)
    _cut_gzip_bytes(archive_dir / "seg-00000.warc.gz", keep_fraction=0.85)
    result = verify_archive(archive_dir, index_path)
    assert result.status == "fail"
    codes = _codes(result)
    # 指向未读尾部的 CDX 行与跨卷 revisit 都只能是未判定，不能宣称缺失。
    assert "INDEX_REFERENCE_UNRESOLVED" in codes
    assert "REVISIT_TARGET_UNRESOLVED" in codes
    assert "INDEX_MISSING_RECORD" not in codes
    assert "REVISIT_TARGET_MISSING" not in codes
    unresolved_index = next(issue for issue in result.issues
                            if issue.code == "INDEX_REFERENCE_UNRESOLVED")
    assert unresolved_index.file == "seg-00000.warc.gz"
    unresolved_revisit = next(issue for issue in result.issues
                              if issue.code == "REVISIT_TARGET_UNRESOLVED")
    assert unresolved_revisit.file == "seg-00001.warc.gz"
    assert result.stats["unresolved"] == 2
    assert result.stats["truncated_segments"] == 1


def test_prefix_digest_error_is_not_downgraded_by_later_truncation(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    target = first[-1]
    tampered_block = bytes([target.block[0] ^ 0x01]) + target.block[1:]
    target.block = tampered_block  # 头部摘要保持原值 -> 确定的 BLOCK_DIGEST_MISMATCH
    _write_segment(archive_dir, 0, first)
    # 再制造一个截断卷，迫使同次核验进入覆盖边界分支。
    extra = [_make_request("http://example.test/extra"),
        _make_response("http://example.test/extra", b"extra-body" * 8)]
    third_path = _write_segment(archive_dir, 2, extra)
    _cut_gzip_bytes(third_path, keep_fraction=0.8)
    index_path = _write_index_for(archive_dir, first + second + extra)
    result = verify_archive(archive_dir, index_path)
    digest_issue = next(issue for issue in result.issues
                        if issue.code == "BLOCK_DIGEST_MISMATCH")
    assert digest_issue.file == "seg-00000.warc.gz"
    assert digest_issue.record_index == target.record_index
    # 前缀中的确定错误与截断卷的未判定引用同时保留。
    assert "INDEX_REFERENCE_UNRESOLVED" in _codes(result)


def test_confirmed_error_in_other_complete_segment_survives_truncation(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    # seg-0 截断，但 seg-1 完整；在完整的 seg-1 上制造悬空 CDX 引用。
    _cut_gzip_bytes(archive_dir / "seg-00000.warc.gz", keep_fraction=0.85)
    ghost = CdxRow(urlkey="com,example)/ghost", timestamp=STAMP,
        url="http://example.test/ghost", status=200, mime="text/html",
        digest="sha256:" + "0" * 64, offset=4242, length=7,
        filename="seg-00001.warc.gz")
    index_path = _write_index_for(archive_dir, first + second, extra=[ghost])
    result = verify_archive(archive_dir, index_path)
    codes = _codes(result)
    assert "INDEX_MISSING_RECORD" in codes  # 完整卷上的真错误不得被掩盖
    assert "INDEX_REFERENCE_UNRESOLVED" in codes  # 截断尾部仍只是未判定
    confirmed = next(issue for issue in result.issues
                     if issue.code == "INDEX_MISSING_RECORD")
    assert confirmed.file == "seg-00001.warc.gz"


def test_offset_inside_parsed_prefix_is_confirmed_missing(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    _cut_gzip_bytes(archive_dir / "seg-00000.warc.gz", keep_fraction=0.85)
    cut = load_segment(archive_dir / "seg-00000.warc.gz")
    assert cut.fully_covered is False
    assert cut.parsed_offset > 5  # offset 5 确定落在已成功解析的前缀内
    # 偏移落在已成功解析的前缀字节范围内却无记录 -> 确定的偏移错误。
    ghost = CdxRow(urlkey="com,example)/gap", timestamp=STAMP,
        url="http://example.test/gap", status=200, mime="text/html",
        digest="sha256:" + "2" * 64, offset=5, length=9,
        filename="seg-00000.warc.gz")
    index_path = _write_index_for(archive_dir, first + second, extra=[ghost])
    result = verify_archive(archive_dir, index_path)
    # 前缀范围内的悬空引用是确定错误；指向未读尾部的 T 才是未判定。
    gap_issue = next(issue for issue in result.issues
                     if issue.code == "INDEX_MISSING_RECORD" and issue.file == "seg-00000.warc.gz")
    assert gap_issue.record_index == len(cut.records)
    unresolved_files = {issue.file for issue in result.issues
                        if issue.code == "INDEX_REFERENCE_UNRESOLVED"}
    assert unresolved_files == {"seg-00000.warc.gz"}


def test_cross_segment_revisit_resolves_and_checks_digest(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    index_path = _write_index_for(archive_dir, first + second)
    ok = verify_archive(archive_dir, index_path)
    assert ok.status == "pass"
    # 摘要不一致仍按确定错误报告。
    second[1] = _make_revisit("http://example.test/revisit", first[-1].record_id,
        "sha256:" + "9" * 64)
    _write_segment(archive_dir, 1, second)
    index_path = _write_index_for(archive_dir, first + second)
    result = verify_archive(archive_dir, index_path)
    bad = next(issue for issue in result.issues if issue.code == "REVISIT_DIGEST_MISMATCH")
    assert bad.file == "seg-00001.warc.gz"


def test_duplicate_cdx_key_is_reported(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    rows = [_cdx_row(record) for record in first + second
            if record.record_type in ("response", "revisit")]
    rows.append(rows[0])
    # write_index 会去重，这里直接写原始文本来制造真正的重复 key。
    index_path = archive_dir / "index.cdx"
    index_path.write_text("".join(row.to_line() + "\n" for row in rows), encoding="utf-8")
    result = verify_archive(archive_dir, index_path)
    duplicate = next(issue for issue in result.issues
                     if issue.code == "INDEX_DUPLICATE_ENTRY")
    assert duplicate.file == "index.cdx"


def test_truncated_archive_never_passes(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    _write_index_for(archive_dir, first + second)
    _cut_gzip_bytes(archive_dir / "seg-00000.warc.gz", keep_fraction=0.85)
    result = verify_archive(archive_dir, archive_dir / "index.cdx")
    assert result.status == "fail"
    assert result.stats["truncated_segments"] == 1
    assert all(check.status == "fail" for check in result.checks
               if check.name in ("truncation", "index_consistency", "revisit_links"))


def test_temp_store_is_cleaned_up_on_success_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    index_path = _write_index_for(archive_dir, first + second)
    created: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr(verify_store_module.tempfile, "mkstemp", tracking_mkstemp)
    verify_archive(archive_dir, index_path)
    assert created, "verify run must create a temporary SQLite store"
    assert all(not path.exists() for path in created)

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: object, parameters: object = ()) -> object:  # type: ignore[override]
            if isinstance(sql, str) and sql.lstrip().startswith("CREATE TABLE"):
                raise sqlite3.OperationalError("injected schema failure")
            return super().execute(sql, parameters)

    real_connect = sqlite3.connect

    def failing_connect(path: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = FailingConnection
        return real_connect(path, *args, **kwargs)

    failing: list[Path] = []

    def failing_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        failing.append(Path(name))
        return fd, name

    monkeypatch.setattr(verify_store_module.tempfile, "mkstemp", failing_mkstemp)
    monkeypatch.setattr(verify_store_module.sqlite3, "connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        VerifyStore()
    assert failing, "failure path must still create a temporary file first"
    assert all(not path.exists() for path in failing)


def test_issue_order_is_stable_across_runs(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    ghost = CdxRow(urlkey="com,example)/ghost", timestamp=STAMP,
        url="http://example.test/ghost", status=200, mime="text/html",
        digest="sha256:" + "0" * 64, offset=999999, length=42,
        filename="seg-00001.warc.gz")
    index_path = _write_index_for(archive_dir, first + second, extra=[ghost])
    left = verify_archive(archive_dir, index_path)
    right = verify_archive(archive_dir, index_path)
    signature = [(issue.file, issue.record_index, issue.code) for issue in left.issues]
    assert signature == [(issue.file, issue.record_index, issue.code) for issue in right.issues]


def test_trailing_gzip_bytes_keep_confirmed_missing_semantics(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    # 给完整 gzip 成员追加一个空成员：gzip 正常 EOF，只报 GZIP_TRAILING_DATA。
    path = archive_dir / "seg-00001.warc.gz"
    path.write_bytes(path.read_bytes() + gzip_compress(b""))
    ghost = CdxRow(urlkey="com,example)/ghost", timestamp=STAMP,
        url="http://example.test/ghost", status=200, mime="text/html",
        digest="sha256:" + "0" * 64, offset=4242, length=7,
        filename="seg-00001.warc.gz")
    index_path = _write_index_for(archive_dir, first + second, extra=[ghost])
    result = verify_archive(archive_dir, index_path)
    codes = _codes(result)
    assert "GZIP_TRAILING_DATA" in codes
    # WARC 已全部解压并解析，悬空引用仍是确认缺失，不能被降级为未判定。
    assert "INDEX_MISSING_RECORD" in codes
    assert "INDEX_REFERENCE_UNRESOLVED" not in codes
    assert result.stats["truncated_segments"] == 0


def test_unreadable_segment_makes_its_references_unresolved(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    first, second = _build_two_segment_archive(archive_dir)
    index_path = _write_index_for(archive_dir, first + second)
    unreadable = archive_dir / "seg-00001.warc.gz"
    unreadable.chmod(0o000)
    try:
        result = verify_archive(archive_dir, index_path)
    finally:
        unreadable.chmod(0o644)
    codes = _codes(result)
    assert "FILE_READ_ERROR" in codes
    assert "INDEX_REFERENCE_UNRESOLVED" in codes
    assert "INDEX_MISSING_RECORD" not in codes
    assert result.status == "fail"


def test_peak_memory_does_not_accumulate_across_large_segments(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    body = b"x" * (512 * 1024)
    segment_count = 6
    warcinfo = _make_warcinfo()
    all_records: list[WarcRecord] = [warcinfo]
    for index in range(segment_count):
        url = f"http://example.test/page-{index}"
        response = _make_response(url, body)
        records = [warcinfo] if index == 0 else []
        records.extend([_make_request(url), response])
        _write_segment(archive_dir, index, records)
        all_records.append(response)
    index_path = _write_index_for(archive_dir, all_records)
    total_payload = segment_count * len(body)
    assert total_payload == 3 * 1024 * 1024  # 多卷大正文，旧实现峰值与其同量级
    tracemalloc.start()
    try:
        result = verify_archive(archive_dir, index_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.status == "pass"
    # 旧实现的峰值与全部卷正文同量级；流式实现应稳定在约单卷水平。
    assert peak < 3 * 1024 * 1024, f"peak memory {peak} grew with segment count"
