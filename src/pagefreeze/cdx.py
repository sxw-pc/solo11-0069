"""CDX 风格文本索引：从 WARC 反解、排序去重、查询与时间窗过滤。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from .archive_io import load_archive
from .errors import CdxError, PagefreezeError
from .httpmsg import mime_type, parse_response
from .policy import normalize_url, urlkey
from .util import parse_warc_date, timestamp14, write_text
from .warc import WarcRecord

# 索引行与所定位 WARC 记录之间的逐字段绑定问题码，
# 供全量核验（verify）与在线回放（replay）复用同一套判定。
INDEX_BINDING_CODES = frozenset((
    "INDEX_LENGTH_MISMATCH",
    "INDEX_URL_MISMATCH",
    "INDEX_URLKEY_MISMATCH",
    "INDEX_TIMESTAMP_MISMATCH",
    "INDEX_STATUS_MISMATCH",
    "INDEX_MIME_MISMATCH",
    "INDEX_DIGEST_MISMATCH",
    "INDEX_RECORD_TYPE_MISMATCH",
))

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


def read_index(path: Path) -> list[CdxRow]:
    """读取并校验 CDX 文本；不合法行给出行号。"""
    if not path.is_file():
        raise CdxError("CDX_MISSING", "index file does not exist")
    rows: list[CdxRow] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CdxError("CDX_READ_ERROR", "cannot read index") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
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
        rows.append(CdxRow(
                urlkey=parts[0], timestamp=parts[1], url=parts[2], status=status,
                mime=parts[4], digest=parts[5], offset=offset, length=length,
                filename=parts[8],
            ))
    return rows


def bind_index_row(row: CdxRow, record: WarcRecord) -> list[tuple[str, str]]:
    """逐字段核对 CDX 行与其按 (filename, offset) 定位到的 WARC 记录。

    纯语法之外的语义绑定：URL/urlkey、timestamp、status、MIME、payload
    digest、length，以及 response/revisit 记录类型。返回 ``(code, message)``
    列表；空列表表示该行与记录完全绑定。HTTP 块本身无法解析时返回原始错误码，
    调用方据此拒绝把该记录当作行所声称的快照。
    """
    issues: list[tuple[str, str]] = []
    if row.length != record.length:
        issues.append(("INDEX_LENGTH_MISMATCH",
            "index length differs from record length"))
    target = record.header("WARC-Target-URI")
    date_value = record.header("WARC-Date")
    try:
        message = parse_response(record.block)
        normalized = normalize_url(target or "")
        parsed_date = parse_warc_date(date_value or "")
    except PagefreezeError as exc:
        return [(exc.code, exc.message)]
    if row.url != normalized:
        issues.append(("INDEX_URL_MISMATCH", "index URL differs from record"))
    if row.urlkey != urlkey(normalized):
        issues.append(("INDEX_URLKEY_MISMATCH", "index urlkey differs from record"))
    if row.timestamp != timestamp14(parsed_date):
        issues.append(("INDEX_TIMESTAMP_MISMATCH",
            "index timestamp differs from record"))
    if row.status != int(message.status or 0):
        issues.append(("INDEX_STATUS_MISMATCH", "index status differs from HTTP block"))
    if row.mime != mime_type(message.headers):
        issues.append(("INDEX_MIME_MISMATCH", "index MIME differs from HTTP block"))
    if row.digest != record.header("WARC-Payload-Digest"):
        issues.append(("INDEX_DIGEST_MISMATCH",
            "index digest differs from record"))
    if record.record_type not in ("response", "revisit"):
        issues.append(("INDEX_RECORD_TYPE_MISMATCH",
            "index points at a non-response record"))
    return issues

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
