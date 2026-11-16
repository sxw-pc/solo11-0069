"""gzip 链接发现测试用的确定性合成路由。

这些路由挂在 ``/gz/`` 下，不被 demo 首页引用，因此不会改变 demo 的既有
计数。测试把种子指向某个 ``/gz/...`` 页面，覆盖：合法拼接成员、中文相对
链接、尾成员 CRC/ISIZE 损坏、截断、解压超限与非 HTML gzip。
"""
from __future__ import annotations

import struct
from functools import lru_cache
from urllib.parse import unquote
from ..util import gzip_compress

_HEAD = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<title>gzip 链接发现样例</title></head><body>"
).encode("utf-8")
_TAIL = b"</body></html>"

# 成员一：页面开头与 /guide、中文相对链接。
_MEMBER_ONE = _HEAD + (
    "<h1>gzip 链接发现样例</h1><nav>"
    "<a href=\"/guide\">业务指南</a>"
    "<a href=\"中文/页面.html\">中文相对页面</a>"
).encode("utf-8")
# 成员二：页面结尾与 /appendix；完整实体应两者都发现。
_MEMBER_TWO = (
    "<a href=\"/appendix\">附录</a>"
    "<a href=\"/guide#again\">重复指南（去重）</a>"
).encode("utf-8") + _TAIL

_COMPLETE = gzip_compress(_MEMBER_ONE) + gzip_compress(_MEMBER_TWO)


def _gzip_with_bad_trailer(data: bytes, *, isize: bool = False) -> bytes:
    """生成 deflate 正确但 CRC 或 ISIZE 尾标记错误的单成员 gzip。"""
    raw = bytearray(gzip_compress(data))
    start = len(raw) - 4 if isize else len(raw) - 8
    value = struct.unpack("<I", raw[start:start + 4])[0]
    raw[start:start + 4] = struct.pack("<I", value ^ 0x1)
    return bytes(raw)


_BAD_CRC = gzip_compress(_MEMBER_ONE) + _gzip_with_bad_trailer(_MEMBER_TWO)
_BAD_ISIZE = gzip_compress(_MEMBER_ONE) + _gzip_with_bad_trailer(_MEMBER_TWO, isize=True)
_TRUNCATED = _COMPLETE[:-6]


@lru_cache(maxsize=1)
def _oversize() -> bytes:
    """解压后超过 MAX_DECODED_BYTES 的合法 gzip；按需生成一次。"""
    return gzip_compress(_HEAD + b"<p>" + b"x" * (1024 * 1024 + 64) + _TAIL)


def oversize_body() -> bytes:
    """暴露超限实体供测试直接断言“压缩体小于 body 上限”。"""
    return _oversize()

_PLAIN = _HEAD + (
    "<h1>普通 HTML</h1><nav>"
    "<a href=\"/guide\">业务指南</a>"
    "<a href=\"中文/页面.html\">中文相对页面</a>"
    "<a href=\"/appendix\">附录</a>"
    "</nav>"
).encode("utf-8") + _TAIL

_CSS = b".gzip{background:url(\"href=/not-a-link\")}\n"


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>{title}</title><body>{body}</body></html>"
    ).encode("utf-8")


_TARGETS = {
    "/guide": _page("业务指南", "<h1>业务指南</h1><a href=\"/gz/complete.html\">返回样例</a>"),
    "/appendix": _page("附录", "<h1>附录</h1><a href=\"/gz/complete.html\">返回样例</a>"),
    "/gz/中文/页面.html": _page("中文相对页面", "<h1>中文相对页面</h1>"),
}

_INDEX = _page("gzip 样例目录", (
    "<h1>gzip 样例目录</h1><nav>"
    "<a href=\"/gz/complete.html\">合法拼接成员</a>"
    "<a href=\"/gz/bad-crc.html\">尾成员 CRC 错误</a>"
    "<a href=\"/gz/bad-isize.html\">尾成员 ISIZE 错误</a>"
    "<a href=\"/gz/truncated.html\">截断实体</a>"
    "<a href=\"/gz/oversize.html\">解压超限</a>"
    "<a href=\"/gz/plain.html\">普通 HTML</a>"
    "<a href=\"/gz/style.css\">非 HTML gzip</a>"
    "</nav>"
))

# 预算对照用：一个自身不再发现新链接的正常页面。
_AFTER = _page("后续正常页面", "<h1>后续正常页面</h1>")


def _html(body: bytes) -> tuple[str, bytes, str]:
    return "text/html; charset=utf-8", body, "gz"


def respond_gzip(path: str) -> tuple[str, bytes, str, tuple[tuple[str, str], ...]] | None:
    """返回 ``(content_type, body, route, headers)``；不属于 /gz/ 时返回 None。"""
    decoded = unquote(path)
    if decoded == "/gz/" or decoded == "/gz/index.html":
        content_type, body, route = _html(_INDEX)
        return content_type, body, route, ()
    if decoded in _TARGETS:
        content_type, body, _ = _html(_TARGETS[decoded])
        return content_type, body, "gz-target", ()
    if path == "/gz/complete.html":
        content_type, body, route = _html(_COMPLETE)
        return content_type, body, route, (("Content-Encoding", "gzip"),)
    if path == "/gz/bad-crc.html":
        content_type, body, route = _html(_BAD_CRC)
        return content_type, body, "gz-bad-crc", (("Content-Encoding", "gzip"),)
    if path == "/gz/bad-isize.html":
        content_type, body, route = _html(_BAD_ISIZE)
        return content_type, body, "gz-bad-isize", (("Content-Encoding", "gzip"),)
    if path == "/gz/truncated.html":
        content_type, body, route = _html(_TRUNCATED)
        return content_type, body, "gz-truncated", (("Content-Encoding", "gzip"),)
    if path == "/gz/oversize.html":
        content_type, _, route = _html(b"")
        return content_type, _oversize(), "gz-oversize", (("Content-Encoding", "gzip"),)
    if path == "/gz/plain.html":
        content_type, body, route = _html(_PLAIN)
        return content_type, body, route, ()
    if path == "/gz/after.html":
        content_type, body, route = _html(_AFTER)
        return content_type, body, route, ()
    if path == "/gz/style.css":
        return "text/css; charset=utf-8", _CSS, "gz-css", (("Content-Encoding", "gzip"),)
    return None
