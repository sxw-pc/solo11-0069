"""有界 gzip 成员解码：只有全部成员校验通过才返回完整文档。

HTTP ``Content-Encoding: gzip`` 允许一个响应体由多个 gzip 成员拼接。
本模块逐个成员解压，并在内存中累计输出；任一成员截断、CRC/ISIZE
校验失败、尾随字节不是合法成员或解压结果超过硬上限时，整体失败。
调用方据此决定“零个链接”还是“完整链接集合”，不存在部分提交。
"""
from __future__ import annotations

import zlib

# 解压后字节的独立硬上限，与压缩响应体的 max_body_bytes 无关。
# 该值只约束内存缓冲，不改变已归档的原始响应；超限时整页放弃链接发现。
MAX_DECODED_BYTES = 1024 * 1024

_WBITS = 16 + zlib.MAX_WBITS


class GzipBodyError(ValueError):
    """gzip 实体无法完整验证；``code`` 是稳定的内部诊断码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _classify_error(exc: zlib.error) -> str:
    text = str(exc).lower()
    if "incorrect data check" in text:
        return "GZIP_CRC_MISMATCH"
    if "incorrect length check" in text:
        return "GZIP_ISIZE_MISMATCH"
    if "incomplete" in text or "truncated" in text or "buffer error" in text:
        return "GZIP_TRUNCATED"
    return "GZIP_INVALID"


def decode_gzip_members(data: bytes, limit: int | None = None) -> bytes:
    """完整解压所有拼接成员；任何不完整或超限都抛出 ``GzipBodyError``。

    ``limit`` 为 ``None`` 时使用模块当前常量，便于测试注入更小的上限。
    输出在内存中缓冲，但累计长度始终受 ``limit`` 约束；失败时不返回前缀。
    """
    cap = MAX_DECODED_BYTES if limit is None else limit
    if cap < 0:
        raise GzipBodyError("GZIP_LIMIT_INVALID", "decoded byte limit must be non-negative")
    if not data:
        raise GzipBodyError("GZIP_TRUNCATED", "gzip body has no member")
    output = bytearray()
    remaining = data
    members = 0
    while remaining:
        inflater = zlib.decompressobj(_WBITS)
        chunk = remaining
        while True:
            room = cap - len(output) + 1
            try:
                piece = inflater.decompress(chunk, room)
            except zlib.error as exc:
                raise GzipBodyError(_classify_error(exc), str(exc)) from exc
            output.extend(piece)
            if len(output) > cap:
                raise GzipBodyError("GZIP_TOO_LARGE",
                    f"decoded body exceeds {cap} bytes",
                )
            if inflater.eof:
                members += 1
                remaining = inflater.unused_data
                break
            chunk = inflater.unconsumed_tail
            if not chunk:
                raise GzipBodyError("GZIP_TRUNCATED", "gzip member is incomplete")
    if members == 0:
        raise GzipBodyError("GZIP_TRUNCATED", "gzip body has no complete member")
    return bytes(output)
