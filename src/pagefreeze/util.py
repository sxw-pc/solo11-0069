"""通用工具：确定性时钟、摘要、稳定 JSON 与固定头 gzip。"""
from __future__ import annotations

import json
import os
import re
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .errors import PagefreezeError

DEFAULT_DEMO_CLOCK = "20240115000000"
TIMESTAMP_RE = re.compile(r"^\d{14}$")
_WARC_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def parse_clock(value: str) -> datetime:
    """解析 14 位 UTC 时间戳或 ISO-8601 时间为带时区 datetime。"""
    text = value.strip()
    if TIMESTAMP_RE.match(text):
        try:
            parsed = datetime.strptime(text, "%Y%m%d%H%M%S")
        except ValueError as exc:
            raise PagefreezeError("CLOCK_INVALID", f"invalid clock: {value}") from exc
        return parsed.replace(tzinfo=timezone.utc)
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise PagefreezeError("CLOCK_INVALID", f"invalid clock: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def resolve_clock(value: str | None, demo_default: str | None = None) -> datetime:
    """CLI 优先级：显式参数 > PAGEFREEZE_CLOCK > demo 默认 > 当前 UTC。"""
    raw = value or os.environ.get("PAGEFREEZE_CLOCK")
    if not raw and demo_default:
        raw = demo_default
    if not raw:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return parse_clock(raw)


def timestamp14(moment: datetime) -> str:
    """输出 CDX 使用的 14 位 UTC 时间戳。"""
    return moment.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def warc_date(moment: datetime) -> str:
    """输出 WARC-Date 的 ISO 秒级 UTC 格式。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_warc_date(value: str) -> datetime:
    """解析 WARC-Date；不合法时抛出稳定错误码。"""
    match = _WARC_DATE_RE.match(value)
    if not match:
        raise PagefreezeError("WARC_DATE_INVALID", f"invalid WARC-Date: {value}")
    parts = [int(item) for item in match.groups()]
    return datetime(*parts, tzinfo=timezone.utc)


def http_date(moment: datetime) -> str:
    """生成不依赖系统本地时区的 HTTP Date 头。"""
    utc = moment.astimezone(timezone.utc)
    return (f"{_WEEKDAYS[utc.weekday()]}, {utc.day:02d} {_MONTHS[utc.month - 1]} "
        f"{utc.year:04d} {utc.hour:02d}:{utc.minute:02d}:{utc.second:02d} GMT")


def stable_json(data: Any) -> str:
    """固定键顺序的 JSON 序列化；键顺序由调用方构造字典时决定。"""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def gzip_compress(data: bytes) -> bytes:
    """生成头字段固定的 gzip 字节流，避免 mtime / OS 字节破坏确定性。"""
    compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
    payload = compressor.compress(data) + compressor.flush()
    header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return header + payload + trailer


def write_text(path: Path, text: str) -> None:
    """以 UTF-8 和 LF 原子写文本（同目录临时文件后替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def size_bucket(size: int) -> str:
    """把响应体大小归入固定的三个报告区间。"""
    if size < 1024:
        return "lt_1k"
    if size < 10240:
        return "1k_10k"
    return "ge_10k"
