"""WARC 序列化/反序列化、摘要与截断的单元测试。"""
from __future__ import annotations

import pytest
from pagefreeze.errors import WarcParseError
from pagefreeze.httpmsg import build_response, parse_response
from pagefreeze.digest import digest_bytes
from pagefreeze.warc import parse_records, scan_records, serialize_record


def _response_bytes(body: bytes) -> bytes:
    block = build_response(200,
        "OK",
        "HTTP/1.1",
        [("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("X-Synthetic", "yes"),
        ],
        body,
    )
    headers = [("WARC-Type", "response"),
        ("WARC-Record-ID", "<urn:uuid:11111111-1111-5111-8111-111111111111>"),
        ("WARC-Date", "2024-01-15T00:00:00Z"),
        ("WARC-Target-URI", "http://127.0.0.1:18080/zh/"),
        ("WARC-Block-Digest", digest_bytes(block)),
        ("WARC-Payload-Digest", digest_bytes(body)),
        ("Content-Type", "application/http; msgtype=response"),
        ("Content-Length", str(len(block))),
    ]
    return serialize_record(headers, block)


def test_roundtrip_preserves_utf8_and_binary_payload() -> None:
    body = "中文载荷".encode("utf-8") + b"\x00\x01\xff"
    raw = _response_bytes(body)
    records = parse_records(raw, "seg-test.warc.gz")
    assert len(records) == 1
    record = records[0]
    assert record.record_type == "response"
    assert record.header("WARC-Target-URI") == "http://127.0.0.1:18080/zh/"
    assert record.length == len(raw)
    assert record.block.endswith(body)
    assert parse_response(record.block).body == body


def test_scan_reports_truncated_tail() -> None:
    raw = _response_bytes(b"payload")
    records, problem = scan_records(raw[:-2])
    assert records == []
    assert problem is not None
    assert problem.code == "WARC_TRUNCATED"
    with pytest.raises(WarcParseError) as caught:
        parse_records(raw[:-2])
    assert caught.value.code == "WARC_TRUNCATED"
    assert caught.value.record_index == 0


def test_known_digest_format() -> None:
    assert digest_bytes(b"abc") == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
