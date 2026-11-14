"""摘要计算：block/payload 摘要、文件流式摘要与多文件联合指纹。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable
from .errors import PagefreezeError


def sha256_bytes(data: bytes) -> str:
    """返回不带算法前缀的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def digest_bytes(data: bytes, algorithm: str = "sha256") -> str:
    """返回 ``sha256:<hex>`` 形式的摘要；其他算法给稳定错误码。"""
    if algorithm != "sha256":
        raise PagefreezeError("DIGEST_UNSUPPORTED", f"unsupported digest: {algorithm}")
    return f"sha256:{sha256_bytes(data)}"


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免把大归档一次读入内存。"""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def files_fingerprint(paths: Iterable[Path]) -> str:
    """对按文件名排序的文件内容做联合摘要，便于打印归档指纹。"""
    hasher = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        hasher.update(path.name.encode("utf-8"))
        hasher.update(bytes.fromhex(sha256_file(path)))
    return hasher.hexdigest()
