"""WARC/1.0 子集的记录容器、序列化与按字节位置解析。

记录构造器（warcinfo/request/response/revisit）在 ``warc_build.py``，
本模块只负责格式层，核验与索引都依赖这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from .errors import WarcError, WarcParseError
from .httpmsg import CRLF, HEADER_END

WARC_VERSION = b"WARC/1.0"
REVISIT_PROFILE = "identical-payload-digest"
_MAX_HEADER_BYTES = 65536


@dataclass
class WarcRecord:
    """一条已解析或待写入的记录；offset/length 描述卷内原始字节范围。"""
    headers: list[tuple[str, str]]
    block: bytes
    offset: int = 0
    length: int = 0
    filename: str = ""
    record_index: int = 0

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def header_map(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers}

    @property
    def record_id(self) -> str:
        return self.header("WARC-Record-ID") or ""

    @property
    def record_type(self) -> str:
        return (self.header("WARC-Type") or "").lower()


@dataclass(frozen=True)
class ParseProblem:
    """解析中止的位置与稳定原因码。"""
    code: str
    message: str
    record_index: int
    offset: int


def serialize_record(headers: Iterable[tuple[str, str]], block: bytes) -> bytes:
    """按给定头顺序序列化记录，并校验 Content-Length 与实体一致。"""
    header_list = list(headers)
    length_values = [value for key, value in header_list if key.lower() == "content-length"]
    if not length_values:
        raise WarcError("WARC_CONTENT_LENGTH", "record needs Content-Length")
    try:
        declared = int(length_values[0])
    except ValueError as exc:
        raise WarcError("WARC_CONTENT_LENGTH", "Content-Length must be an integer") from exc
    if declared != len(block):
        raise WarcError("WARC_CONTENT_LENGTH", "Content-Length does not match block")
    lines = [WARC_VERSION]
    for key, value in header_list:
        if any(char in key + value for char in ("\r", "\n")):
            raise WarcError("WARC_HEADER_INVALID", "newline in WARC header")
        try:
            lines.append(f"{key}: {value}".encode("latin-1"))
        except UnicodeEncodeError as exc:
            raise WarcError("WARC_HEADER_INVALID", "WARC header must be latin-1") from exc
    return CRLF.join(lines) + HEADER_END + block + CRLF + CRLF


def scan_records(data: bytes, filename: str = "") -> tuple[list[WarcRecord], ParseProblem | None]:
    """尽力解析整卷；遇到尾部半条记录时返回已解析记录和问题描述。"""
    records: list[WarcRecord] = []
    offset = 0
    while offset < len(data):
        index = len(records)
        if not data.startswith(WARC_VERSION, offset):
            return records, ParseProblem(
                "WARC_MALFORMED", "record does not start with WARC/1.0", index, offset)
        header_end = data.find(HEADER_END, offset)
        if header_end < 0 or header_end - offset > _MAX_HEADER_BYTES:
            return records, ParseProblem("WARC_TRUNCATED", "record header is incomplete", index, offset)
        head = data[offset:header_end]
        lines = head.split(CRLF)
        headers: list[tuple[str, str]] = []
        for raw_line in lines[1:]:
            if b":" not in raw_line:
                return records, ParseProblem("WARC_MALFORMED", "invalid WARC header line", index, offset)
            key, value = raw_line.split(b":", 1)
            try:
                headers.append((key.decode("latin-1"), value.decode("latin-1").strip()))
            except UnicodeDecodeError:
                return records, ParseProblem("WARC_MALFORMED", "non-latin header bytes", index, offset)
        content_lengths = [value for key, value in headers if key.lower() == "content-length"]
        if not content_lengths:
            return records, ParseProblem("WARC_MALFORMED", "missing Content-Length", index, offset)
        try:
            block_length = int(content_lengths[0])
        except ValueError:
            return records, ParseProblem("WARC_MALFORMED", "invalid Content-Length", index, offset)
        if block_length < 0:
            return records, ParseProblem("WARC_MALFORMED", "negative Content-Length", index, offset)
        body_start = header_end + len(HEADER_END)
        record_end = body_start + block_length + len(CRLF + CRLF)
        if record_end > len(data):
            return records, ParseProblem("WARC_TRUNCATED", "record body is incomplete", index, offset)
        if data[body_start + block_length:record_end] != CRLF + CRLF:
            return records, ParseProblem("WARC_MALFORMED", "record trailer is missing", index, offset)
        records.append(WarcRecord(
                headers=headers,
                block=data[body_start:body_start + block_length],
                offset=offset,
                length=record_end - offset,
                filename=filename,
                record_index=index,
            ))
        offset = record_end
    return records, None


def parse_records(data: bytes, filename: str = "") -> list[WarcRecord]:
    """严格解析；任何结构问题都抛出带记录序号的异常。"""
    records, problem = scan_records(data, filename)
    if problem is not None:
        raise WarcParseError(problem.code,
            problem.message,
            record_index=problem.record_index,
            offset=problem.offset,
        )
    return records
