"""WARC 子集记录构造器：warcinfo / request / response / revisit。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable
from urllib.parse import urlsplit
from . import SOFTWARE
from .fetch import FetchResult
from .httpmsg import build_response
from .digest import digest_bytes
from .util import timestamp14, warc_date
from .warc import REVISIT_PROFILE, WarcRecord


def record_urn(namespace: uuid.UUID, url_key: str, ts14: str, kind: str) -> str:
    """由命名空间 + urlkey + 时间戳 + 记录种类生成确定性 UUIDv5。"""
    seed = f"{url_key}\n{ts14}\n{kind}"
    return f"<urn:uuid:{uuid.uuid5(namespace, seed)}>"


def _make_record(
    warc_type: str,
    urn: str,
    moment: datetime,
    target_uri: str | None,
    block: bytes,
    content_type: str,
    payload_digest: str,
    algorithm: str,
    extras: Iterable[tuple[str, str]] = (),
) -> WarcRecord:
    headers: list[tuple[str, str]] = [("WARC-Type", warc_type),
        ("WARC-Record-ID", urn),
        ("WARC-Date", warc_date(moment)),
    ]
    if target_uri is not None:
        headers.append(("WARC-Target-URI", target_uri))
    headers.extend([
            ("WARC-Block-Digest", digest_bytes(block, algorithm)),
            ("WARC-Payload-Digest", payload_digest),
        ])
    headers.extend(extras)
    headers.extend([
            ("Content-Type", content_type),
            ("Content-Length", str(len(block))),
        ])
    return WarcRecord(headers=headers, block=block)


def make_warcinfo(
    moment: datetime,
    config_digest: str,
    namespace: uuid.UUID,
    algorithm: str,
) -> WarcRecord:
    """卷首记录：软件、配置摘要与抓取窗口。"""
    ts14 = timestamp14(moment)
    block = (f"software: {SOFTWARE}\n"
        "format: WARC/1.0-subset\n"
        f"config-digest: {config_digest}\n"
        f"crawl-window: {warc_date(moment)}/{warc_date(moment)}\n"
        f"record-namespace: {namespace}\n"
    ).encode("utf-8")
    urn = record_urn(namespace, "warcinfo", ts14, "warcinfo")
    return _make_record("warcinfo", urn, moment, None, block, "application/warc-fields",
        digest_bytes(block, algorithm), algorithm,
    )


def make_request_record(
    url: str,
    url_key: str,
    moment: datetime,
    user_agent: str,
    namespace: uuid.UUID,
    algorithm: str,
) -> WarcRecord:
    """记录精简请求头；不保存 Cookie、Authorization 或任何凭据。"""
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    block = (f"GET {target} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        f"User-Agent: {user_agent}\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")
    urn = record_urn(namespace, url_key, timestamp14(moment), "request")
    return _make_record("request", urn, moment, url, block, "application/http; msgtype=request",
        digest_bytes(b"", algorithm), algorithm,
    )


def make_response_record(
    result: FetchResult,
    url_key: str,
    moment: datetime,
    namespace: uuid.UUID,
    algorithm: str,
) -> WarcRecord:
    """保存原始 HTTP 响应头和响应体字节。"""
    block = build_response(result.status, result.reason, result.version, result.headers, result.body)
    urn = record_urn(namespace, url_key, timestamp14(moment), "response")
    return _make_record("response", urn, moment, result.url, block,
        "application/http; msgtype=response",
        digest_bytes(result.body, algorithm), algorithm,
    )


def make_revisit_record(
    result: FetchResult,
    url_key: str,
    moment: datetime,
    refers_to: str,
    payload_digest: str,
    namespace: uuid.UUID,
    algorithm: str,
) -> WarcRecord:
    """只存本次响应头，正文由 WARC-Refers-To 指向首见记录。"""
    header_block = build_response(result.status, result.reason, result.version, result.headers, b"")
    urn = record_urn(namespace, url_key, timestamp14(moment), "revisit")
    return _make_record("revisit", urn, moment, result.url, header_block,
        "application/http; msgtype=response", payload_digest, algorithm,
        extras=(("WARC-Refers-To", refers_to), ("WARC-Profile", REVISIT_PROFILE)),
    )
