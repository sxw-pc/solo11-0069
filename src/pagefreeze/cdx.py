"""CDX 风格文本索引：从 WARC 反解、排序去重、查询与时间窗过滤。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from .archive_io import load_archive
from .errors import CdxError, PagefreezeError
from .httpmsg import mime_type, parse_response
from .policy import normalize_url, urlkey
from .util import parse_warc_date, timestamp14, write_text
from .warc import WarcRecord

FIELDS = ("urlkey", "timestamp", "url", "status", "mime", "digest",
    "offset", "length", "filename",
)


@dataclass(frozen=True)
class CdxRow:
    """一行索引；offset/length 指向解压后卷内原始 WARC 记录。"""
    urlkey: str
    timestamp: str
    url: str
    status: int
    mime: str
    digest: str
    offset: int
    length: int
    filename: str

    def to_line(self) -> str:
        values = [self.urlkey, self.timestamp, self.url, str(self.status), self.mime,
            self.digest, str(self.offset), str(self.length), self.filename,
        ]
        if any(any(char.isspace() for char in value) for value in values):
            raise CdxError("CDX_FIELD_INVALID", "CDX fields must not contain whitespace")
        return " ".join(values)

    def to_dict(self) -> dict[str, object]:
        """固定键顺序的 JSON 友好表示。"""
        return {"urlkey": self.urlkey,
            "timestamp": self.timestamp,
            "url": self.url,
            "status": self.status,
            "mime": self.mime,
            "digest": self.digest,
            "offset": self.offset,
            "length": self.length,
            "filename": self.filename,
        }


def row_from_record(record: WarcRecord) -> CdxRow | None:
    """把 response/revisit 记录转为索引行；其他类型返回 None。"""
    if record.record_type not in ("response", "revisit"):
        return None
    target = record.header("WARC-Target-URI")
    digest = record.header("WARC-Payload-Digest")
    date_value = record.header("WARC-Date")
    if not target or not digest or not date_value:
        raise CdxError("CDX_RECORD_INVALID", "record lacks indexable WARC headers")
    try:
        message = parse_response(record.block)
        moment = parse_warc_date(date_value)
    except PagefreezeError as exc:
        raise CdxError("CDX_RECORD_INVALID", f"cannot index record: {exc}") from exc
    return CdxRow(urlkey=urlkey(target),
        timestamp=timestamp14(moment),
        url=normalize_url(target),
        status=int(message.status or 0),
        mime=mime_type(message.headers),
        digest=digest,
        offset=record.offset,
        length=record.length,
        filename=record.filename,
    )


def rows_from_records(records: Iterable[WarcRecord]) -> list[CdxRow]:
    """从已解析记录生成去重、排序后的索引行。"""
    rows = [row for record in records if (row := row_from_record(record)) is not None]
    return _sorted_unique(rows)


def rows_from_archive(archive_dir: Path) -> list[CdxRow]:
    """重新扫描归档卷生成索引；发现结构损坏时拒绝产出。"""
    loaded = load_archive(archive_dir)
    if loaded.problems:
        problem = loaded.problems[0]
        raise CdxError(problem.code, f"{problem.file}: {problem.message}")
    return rows_from_records(loaded.records)


def _sorted_unique(rows: Iterable[CdxRow]) -> list[CdxRow]:
    chosen: dict[tuple[str, str, str], CdxRow] = {}
    for row in sorted(rows, key=lambda item: (item.urlkey, item.timestamp, item.url, item.offset)):
        chosen.setdefault((row.urlkey, row.timestamp, row.url), row)
    return sorted(chosen.values(), key=lambda item: (item.urlkey, item.timestamp, item.url))


def write_index(path: Path, rows: Iterable[CdxRow]) -> list[CdxRow]:
    """稳定写入索引；返回实际写入的排序去重行。"""
    ordered = _sorted_unique(rows)
    text = "".join(row.to_line() + "\n" for row in ordered)
    try:
        write_text(path, text)
    except OSError as exc:
        raise CdxError("CDX_WRITE_ERROR", f"cannot write index: {exc}") from exc
    return ordered


def _row_from_line(line: str, number: int) -> CdxRow:
    parts = line.split(" ")
    if len(parts) != len(FIELDS) or any(part == "" for part in parts):
        raise CdxError("CDX_MALFORMED", f"invalid CDX line {number}")
    try:
        status = int(parts[3])
        offset = int(parts[6])
        length = int(parts[7])
    except ValueError as exc:
        raise CdxError("CDX_MALFORMED", f"non-numeric CDX field on line {number}") from exc
    if not len(parts[1]) == 14 or not parts[1].isdigit():
        raise CdxError("CDX_MALFORMED", f"invalid timestamp on line {number}")
    return CdxRow(
            urlkey=parts[0], timestamp=parts[1], url=parts[2], status=status,
            mime=parts[4], digest=parts[5], offset=offset, length=length,
            filename=parts[8],
        )


def iter_index(path: Path) -> Iterator[CdxRow]:
    """逐行流式读取索引；不合法行带行号抛出，调用方不必整卷驻留内存。"""
    if not path.is_file():
        raise CdxError("CDX_MISSING", "index file does not exist")
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise CdxError("CDX_READ_ERROR", "cannot read index") from exc
    with handle:
        number = 0
        while True:
            try:
                line = handle.readline()
            except (OSError, UnicodeDecodeError) as exc:
                raise CdxError("CDX_READ_ERROR", "cannot read index") from exc
            if not line:
                break
            number += 1
            if not line.strip():
                continue
            yield _row_from_line(line.rstrip("\n"), number)


def read_index(path: Path) -> list[CdxRow]:
    """读取并校验 CDX 文本；不合法行给出行号。"""
    return list(iter_index(path))


def lookup(rows: Iterable[CdxRow], url: str, ts14: str) -> CdxRow | None:
    """按 URL + 时间戳返回最接近且不晚于 ts 的快照。"""
    key = urlkey(url)
    candidates = [row for row in rows if row.urlkey == key and row.timestamp <= ts14]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item.timestamp, item.url, item.offset))[-1]


def window(
    rows: Iterable[CdxRow],
    key: str,
    start: str = "00000000000000",
    end: str = "99999999999999",
) -> list[CdxRow]:
    """返回指定 urlkey 在闭区间 [start, end] 内的快照。"""
    return sorted([
            row for row in rows
            if row.urlkey == key and start <= row.timestamp <= end],
        key=lambda item: (item.timestamp, item.url, item.offset),
    )
