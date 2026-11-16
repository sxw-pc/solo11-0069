"""链接提取与 gzip 链接事务的单元测试。"""
from __future__ import annotations

import struct

import pytest

from pagefreeze.crawl import (
    LINK_DISCOVERY_SKIP, FetchLog, _handle_result, _stage_links,
)
from pagefreeze.fetch import FetchResult
from pagefreeze.frontier import FrontierItem
from pagefreeze.httpmsg import mime_type
from pagefreeze.links import extract_links
from pagefreeze.policy import urlkey
from pagefreeze.util import gzip_compress

BASE = "http://127.0.0.1:18080/dir/page.html"
HEADERS = (("Content-Type", "text/html; charset=utf-8"),
    ("Content-Encoding", "gzip"),)


def _bad(data: bytes) -> bytes:
    raw = bytearray(gzip_compress(data))
    raw[-8:-4] = struct.pack("<I",
        struct.unpack("<I", raw[-8:-4])[0] ^ 0x1)
    return bytes(raw)


def _result(body: bytes, *, status: int = 200, headers=HEADERS, url: str = BASE
) -> FetchResult:
    return FetchResult(url=url,
        status=status,
        reason="OK",
        version="HTTP/1.1",
        headers=tuple(headers),
        body=body,
        mime=mime_type(headers),
    )


def _item(url: str = BASE) -> FrontierItem:
    return FrontierItem(url=url, urlkey=urlkey(url), depth=1)


class _Recorder:
    """记录 enqueue 的参数，证明提交是有序、仅一次的。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int, str]] = []

    def __call__(self, raw_url: str, base: str | None, depth: int, source: str) -> None:
        self.calls.append((raw_url, base, depth, source))

    @property
    def targets(self) -> list[str]:
        from urllib.parse import urljoin
        return [urljoin(base or source, raw) for raw, base, _, source in self.calls]


class _Config:
    allowed_mime = ("text/html", "text/css")


def test_extract_links_preserves_order_and_dedupes() -> None:
    html = ("<a href=\"/a\">A</a><area href='b.html'>"
        "<a href=\"/a#again\">A2</a><a href=\"mailto:x@y\">mail</a>").encode()
    assert extract_links(html) == ["/a", "b.html", "/a#again"]


def test_complete_gzip_commits_all_links_in_order_once() -> None:
    document = (b'<a href="/guide">g</a>'
        + "<a href=\"中文/页面.html\">zh</a>".encode("utf-8")
        + b'<a href="/appendix">a</a><a href="/guide#dup">again</a>')
    cut = document.index("中文".encode("utf-8"))
    body = gzip_compress(document[:cut]) + gzip_compress(document[cut:])
    recorder = _Recorder()
    skips = []
    _handle_result(_result(body), _item(), _Config, recorder, [], [], skips)
    assert recorder.targets == [
        "http://127.0.0.1:18080/guide",
        "http://127.0.0.1:18080/dir/中文/页面.html",
        "http://127.0.0.1:18080/appendix",
        "http://127.0.0.1:18080/guide#dup",
    ]
    assert skips == []


def test_prefix_links_are_not_committed_when_tail_crc_fails() -> None:
    prefix = gzip_compress(b'<a href="/guide">g</a>')
    body = prefix + _bad(b'<a href="/appendix">a</a>')
    recorder = _Recorder()
    skips = []
    hits: list[FetchResult] = []
    logs: list[FetchLog] = []
    _handle_result(_result(body), _item(), _Config, recorder, hits, logs, skips)
    assert recorder.calls == []
    assert len(hits) == 1 and hits[0].body == body
    assert [log.outcome for log in logs] == ["archived"]
    assert logs[0].reason == LINK_DISCOVERY_SKIP
    assert len(skips) == 1
    assert skips[0].reason == LINK_DISCOVERY_SKIP
    assert skips[0].detail == "GZIP_CRC_MISMATCH"
    assert skips[0].url == BASE


def test_stage_links_does_not_mutate_state_before_validation() -> None:
    recorder = _Recorder()
    body = gzip_compress(b'<a href="/guide">g</a>') + b"\x00\x00"
    links, code = _stage_links(_result(body))
    assert links is None
    assert code == "GZIP_INVALID"
    assert recorder.calls == []


def test_truncated_and_oversize_contribute_no_links() -> None:
    for body, code in (
        (gzip_compress(b'<a href="/guide">g</a>')[:-2], "GZIP_TRUNCATED"),
        (gzip_compress(b'<a href="/guide">g</a>') + gzip_compress(b"tail")[:-1],
         "GZIP_TRUNCATED"),
    ):
        recorder = _Recorder()
        links, found = _stage_links(_result(body))
        assert links is None and found == code
        assert recorder.calls == []


def test_non_html_gzip_is_left_untouched() -> None:
    body = gzip_compress(b'<a href="/guide">g</a>')
    headers = (("Content-Type", "text/css"), ("Content-Encoding", "gzip"))
    recorder = _Recorder()
    hits: list[FetchResult] = []
    logs: list[FetchLog] = []
    _handle_result(_result(body, headers=headers), _item(), _Config, recorder,
        hits, logs, [])
    assert recorder.calls == []
    assert logs[0].outcome == "archived"
    assert logs[0].reason is None
    assert hits[0].body == body


def test_plain_html_still_discovers_links() -> None:
    body = b'<a href="/guide">g</a><a href="/appendix">a</a>'
    headers = (("Content-Type", "text/html; charset=utf-8"),)
    recorder = _Recorder()
    _handle_result(_result(body, headers=headers), _item(), _Config, recorder,
        [], [], [])
    assert recorder.targets == ["http://127.0.0.1:18080/guide",
        "http://127.0.0.1:18080/appendix"]


def test_redirect_html_is_not_expanded() -> None:
    body = gzip_compress(b'<a href="/guide">g</a>')
    headers = (("Content-Type", "text/html; charset=utf-8"),
        ("Content-Encoding", "gzip"), ("Location", "/next"))
    recorder = _Recorder()
    _handle_result(_result(body, status=302, headers=headers), _item(), _Config,
        recorder, [], [], [])
    assert recorder.targets == ["http://127.0.0.1:18080/next"]
