"""WARC 写入：gzip 原始 body、响应头与摘要原样保留，链接跳过不影响归档。"""
from __future__ import annotations

import zlib
from datetime import datetime, timezone
from pathlib import Path

from conftest import ROOT
from pagefreeze.config import load_settings
from pagefreeze.digest import digest_bytes
from pagefreeze.fetch import FetchResult
from pagefreeze.httpmsg import header_value, parse_response
from pagefreeze.store import build_records, write_archive
from pagefreeze.util import gzip_compress
from pagefreeze.warc import parse_records

NOW = datetime(2024, 1, 15, tzinfo=timezone.utc)
PAGE = b'<a href="/guide">guide</a>'
COMPRESSED = gzip_compress(PAGE)
HEADERS = (("Content-Type", "text/html; charset=utf-8"),
    ("Content-Encoding", "gzip"),
    ("Content-Length", str(len(COMPRESSED))),
)


def _result() -> FetchResult:
    return FetchResult(url="http://127.0.0.1:18080/gzip",
        status=200,
        reason="OK",
        version="HTTP/1.1",
        headers=HEADERS,
        body=COMPRESSED,
        mime="text/html",
    )


def test_response_block_keeps_compressed_bytes_and_encoding() -> None:
    config = load_settings(ROOT / "config").archive
    records, _, _ = build_records([_result()], NOW, config, "sha256:" + "0" * 64, "ua")
    response = next(record for record in records if record.record_type == "response")
    message = parse_response(response.block)
    assert header_value(message.headers, "Content-Encoding") == "gzip"
    assert header_value(message.headers, "Content-Type").startswith("text/html")
    assert message.body == COMPRESSED
    assert zlib.decompress(message.body, 16 + zlib.MAX_WBITS) == PAGE


def test_payload_digest_is_over_compressed_body() -> None:
    config = load_settings(ROOT / "config").archive
    records, _, _ = build_records([_result()], NOW, config, "sha256:" + "0" * 64, "ua")
    response = next(record for record in records if record.record_type == "response")
    assert response.header("WARC-Payload-Digest") == digest_bytes(COMPRESSED)


def test_segment_roundtrip_replays_original_gzip(tmp_path: Path) -> None:
    config = load_settings(ROOT / "config").archive
    result = write_archive(tmp_path, config, [_result()], NOW, "sha256:" + "0" * 64, "ua")
    raw = zlib.decompress((tmp_path / "seg-00000.warc.gz").read_bytes(),
        16 + zlib.MAX_WBITS)
    response = next(record for record in parse_records(raw)
        if record.record_type == "response")
    message = parse_response(response.block)
    assert message.body == COMPRESSED
    assert result.responses == 1 and result.revisits == 0
