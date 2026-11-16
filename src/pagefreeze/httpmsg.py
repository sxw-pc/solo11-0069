"""application/http 块（请求行 / 状态行 + 头 + 体）的最小读写工具。"""
from __future__ import annotations

from dataclasses import dataclass
from .errors import PagefreezeError

CRLF = b"\r\n"
HEADER_END = b"\r\n\r\n"


@dataclass(frozen=True)
class HttpMessage:
    """解析后的 HTTP 消息；body 保留原始字节。"""
    start_line: str
    version: str
    status: int | None
    reason: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


def header_value(headers: list[tuple[str, str]] | tuple[tuple[str, str], ...], name: str) -> str | None:
    """大小写不敏感地取首个同名头。"""
    wanted = name.lower()
    for key, value in headers:
        if key.lower() == wanted:
            return value
    return None


def mime_type(headers: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> str:
    """提取小写基础 MIME 类型，例如 ``text/html``。"""
    raw = header_value(headers, "Content-Type") or ""
    return raw.split(";", 1)[0].strip().lower()


def parse_response(block: bytes) -> HttpMessage:
    """解析缓存的 HTTP 响应块；不跟随跳转、不解压正文。"""
    separator = block.find(HEADER_END)
    if separator < 0:
        raise PagefreezeError("HTTP_MESSAGE_MALFORMED", "HTTP block has no header terminator")
    head = block[:separator]
    body = block[separator + len(HEADER_END):]
    lines = head.split(CRLF)
    if not lines:
        raise PagefreezeError("HTTP_MESSAGE_MALFORMED", "empty HTTP block")
    try:
        start_line = lines[0].decode("latin-1")
    except UnicodeDecodeError as exc:
        raise PagefreezeError("HTTP_MESSAGE_MALFORMED", "invalid HTTP start line") from exc
    pieces = start_line.split(" ", 2)
    if len(pieces) < 2 or not pieces[0].startswith("HTTP/"):
        raise PagefreezeError("HTTP_MESSAGE_MALFORMED", "invalid HTTP status line")
    try:
        status = int(pieces[1])
    except ValueError as exc:
        raise PagefreezeError("HTTP_MESSAGE_MALFORMED", "invalid HTTP status code") from exc
    reason = pieces[2] if len(pieces) > 2 else ""
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if b":" not in line:
            raise PagefreezeError("HTTP_MESSAGE_MALFORMED", "invalid HTTP header line")
        key, value = line.split(b":", 1)
        headers.append((key.decode("latin-1"), value.decode("latin-1").strip()))
    return HttpMessage(start_line=start_line,
        version=pieces[0],
        status=status,
        reason=reason,
        headers=tuple(headers),
        body=body,
    )


def build_response(
    status: int,
    reason: str,
    version: str,
    headers: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    body: bytes,
) -> bytes:
    """按固定顺序重建 HTTP 响应块，供 WARC response/revisit 使用。"""
    lines = [f"{version} {status} {reason}".rstrip().encode("latin-1")]
    for key, value in headers:
        if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
            raise PagefreezeError("HTTP_HEADER_INVALID", "newline in HTTP header")
        lines.append(f"{key}: {value}".encode("latin-1"))
    return CRLF.join(lines) + HEADER_END + body
