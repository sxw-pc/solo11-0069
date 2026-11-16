"""FetchResult 原始字节语义与有界 gzip 实体解码。

fetch 层不得解压或改写线上 body；只有链接发现层在验证实体完整后才解码。
"""
from __future__ import annotations

import struct
import zlib

import pytest

from conftest import ROOT
from pagefreeze.config import load_settings
from pagefreeze.fetch import FetchResult, fetch_once
from pagefreeze.gzipbody import (
    MAX_DECODED_BYTES, GzipBodyError, decode_gzip_members,
)
from pagefreeze.httpmsg import header_value, mime_type
from pagefreeze.ratelimit import RateLimiter
from pagefreeze.util import gzip_compress

PAGE = ("<a href=\"/guide\">指南</a>"
    "<a href=\"中文/页面.html\">中文</a>").encode("utf-8")
HEADERS = (("Content-Type", "text/html; charset=utf-8"),
    ("Content-Encoding", "gzip"), ("Content-Length", "0"))


def _bad_trailer(data: bytes, *, isize: bool = False) -> bytes:
    raw = bytearray(gzip_compress(data))
    start = len(raw) - 4 if isize else len(raw) - 8
    raw[start:start + 4] = struct.pack("<I",
        struct.unpack("<I", raw[start:start + 4])[0] ^ 0x1)
    return bytes(raw)


def _result(body: bytes, headers=HEADERS) -> FetchResult:
    return FetchResult(url="http://127.0.0.1:18080/gzip",
        status=200,
        reason="OK",
        version="HTTP/1.1",
        headers=tuple(headers),
        body=body,
        mime=mime_type(headers),
    )


def test_fetch_result_keeps_raw_bytes_and_reports_gzip() -> None:
    compressed = gzip_compress(PAGE)
    result = _result(compressed)
    assert result.body == compressed
    assert result.size == len(compressed)
    assert result.is_gzip
    assert header_value(result.headers, "Content-Encoding") == "gzip"
    assert result.mime == "text/html"


def test_is_gzip_is_case_insensitive_and_header_driven() -> None:
    assert _result(b"x", (("Content-Encoding", "GZip"),)).is_gzip
    assert _result(b"\x1f\x8b", ()).is_gzip is False


def test_fetch_once_preserves_wire_body(gzip_site: str) -> None:
    config = load_settings(ROOT / "config").crawl
    limiter = RateLimiter(0)
    result = fetch_once(gzip_site + "/gz/complete.html", config, limiter)
    assert result.is_gzip
    assert result.mime == "text/html"
    assert result.body.startswith(b"\x1f\x8b")
    # 线上压缩字节原样保留；解压只在链接发现前显式发生。
    assert decode_gzip_members(result.body).startswith(b"<!doctype html>")


def test_single_and_concatenated_members_are_fully_decoded() -> None:
    first = gzip_compress(b"<a href=\"/guide\">x</a>")
    second = gzip_compress(b"<a href=\"/appendix\">y</a>")
    assert decode_gzip_members(first) == b"<a href=\"/guide\">x</a>"
    assert decode_gzip_members(first + second) == (
        b"<a href=\"/guide\">x</a><a href=\"/appendix\">y</a>")


def test_utf8_bytes_survive_multi_member_decoding() -> None:
    split = PAGE.index("中文".encode("utf-8"))
    data = gzip_compress(PAGE[:split]) + gzip_compress(PAGE[split:])
    assert decode_gzip_members(data) == PAGE


def test_crc_and_isize_mismatch_are_distinct_codes() -> None:
    prefix = gzip_compress(b"<a href=\"/guide\">x</a>")
    for suffix, code in ((_bad_trailer(b"tail"), "GZIP_CRC_MISMATCH"),
                         (_bad_trailer(b"tail", isize=True), "GZIP_ISIZE_MISMATCH")):
        with pytest.raises(GzipBodyError) as caught:
            decode_gzip_members(prefix + suffix)
        assert caught.value.code == code


def test_truncated_member_never_returns_a_prefix() -> None:
    data = gzip_compress(b"<a href=\"/guide\">x</a>") + gzip_compress(b"tail")
    with pytest.raises(GzipBodyError) as caught:
        decode_gzip_members(data[:-3])
    assert caught.value.code == "GZIP_TRUNCATED"
    with pytest.raises(GzipBodyError):
        decode_gzip_members(b"")


def test_garbage_and_trailing_bytes_are_rejected() -> None:
    good = gzip_compress(b"payload")
    for data in (b"not gzip", good[:-5], good + good + b"\x00\x00"):
        with pytest.raises(GzipBodyError):
            decode_gzip_members(data)


def test_decoded_limit_is_independent_of_compressed_size() -> None:
    body = b"x" * 4096
    compressed = gzip_compress(body)
    assert len(compressed) < len(body)
    assert decode_gzip_members(compressed, len(body)) == body
    with pytest.raises(GzipBodyError) as caught:
        decode_gzip_members(compressed, len(body) - 1)
    assert caught.value.code == "GZIP_TOO_LARGE"


def test_limit_applies_across_members_not_per_member() -> None:
    half = gzip_compress(b"y" * 600)
    with pytest.raises(GzipBodyError) as caught:
        decode_gzip_members(half + half, 1000)
    assert caught.value.code == "GZIP_TOO_LARGE"


def test_default_limit_is_a_bounded_constant() -> None:
    assert MAX_DECODED_BYTES == 1024 * 1024
    with pytest.raises(GzipBodyError) as caught:
        decode_gzip_members(gzip_compress(b"z" * (MAX_DECODED_BYTES + 1)))
    assert caught.value.code == "GZIP_TOO_LARGE"


def test_zlib_default_stream_still_decodes() -> None:
    """常见 gzip 生产者带文件名或 mtime 的头也必须可解。"""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    data = compressor.compress(b"<a href=\"/guide\">x</a>") + compressor.flush()
    assert decode_gzip_members(data) == b"<a href=\"/guide\">x</a>"
