"""归档卷读取：容忍尾部损坏，同时把损坏位置交给核验器。

核验路径使用 :func:`iter_segments` 逐卷产出 :class:`SegmentLoad`，调用方处理完
一卷即可释放该卷全部正文，峰值内存不随分卷数量累计。每卷同时给出 gzip EOF 与
WARC 解析边界，供核验器区分“确认缺失”与“未判定”。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from .warc import WarcRecord, scan_records


@dataclass(frozen=True)
class ArchiveProblem:
    """卷级问题：文件名、稳定原因码与尽力推断的记录序号。"""
    file: str
    code: str
    record_index: int
    message: str


@dataclass
class SegmentLoad:
    """单个分卷的解压结果与覆盖边界。

    ``gzip_eof`` 表示 gzip 流是否完整读到 trailer；``parse_offset`` 是 WARC 解析
    到达的解压字节边界；``complete`` 仅在二者都覆盖整卷时为真。
    """
    file: str
    gzip_size: int
    raw_size: int
    records: list[WarcRecord] = field(default_factory=list)
    problems: list[ArchiveProblem] = field(default_factory=list)
    gzip_eof: bool = False
    parse_offset: int = 0
    complete: bool = False


@dataclass
class ArchiveLoad:
    """全部已解析卷与聚合问题。"""
    segments: list[SegmentLoad] = field(default_factory=list)
    records: list[WarcRecord] = field(default_factory=list)
    problems: list[ArchiveProblem] = field(default_factory=list)


def list_segments(archive_dir: Path) -> list[Path]:
    """按文件名排序列出 ``seg-*.warc.gz``。"""
    if not archive_dir.is_dir():
        return []
    return sorted(archive_dir.glob("seg-*.warc.gz"), key=lambda item: item.name)


def _gunzip_tolerant(data: bytes) -> tuple[bytes, list[tuple[str, str]], bool]:
    """逐块解压以保留损坏前已解出的字节；返回内容、问题与 gzip EOF 标记。"""
    if not data:
        return b"", [("GZIP_EMPTY", "empty gzip segment")], False
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    chunks: list[bytes] = []
    problems: list[tuple[str, str]] = []
    for start in range(0, len(data), 4096):
        try:
            chunks.append(decompressor.decompress(data[start:start + 4096]))
        except zlib.error as exc:
            problems.append(("GZIP_INVALID", f"gzip stream is invalid: {exc}"))
            break
    if not problems:
        try:
            chunks.append(decompressor.flush())
        except zlib.error as exc:
            problems.append(("GZIP_INVALID", f"gzip trailer is invalid: {exc}"))
    raw = b"".join(chunks)
    eof = decompressor.eof
    if not problems and not eof:
        problems.append(("GZIP_TRUNCATED", "gzip stream ended before trailer"))
    if not problems and decompressor.unused_data:
        problems.append(("GZIP_TRAILING_DATA", "gzip stream has trailing bytes"))
    return raw, problems, eof


def load_segment(path: Path) -> SegmentLoad:
    """读取一个分卷；文件不可读或内容损坏都转为问题列表。"""
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        return SegmentLoad(file=path.name,
            gzip_size=0,
            raw_size=0,
            problems=[ArchiveProblem(path.name, "FILE_READ_ERROR", 0, f"cannot read segment: {exc}")],
        )
    raw, gzip_problems, gzip_eof = _gunzip_tolerant(compressed)
    records, parse_problem = scan_records(raw, path.name)
    problems = [ArchiveProblem(path.name, code, len(records), message)
        for code, message in gzip_problems]
    if parse_problem is not None:
        problems.append(ArchiveProblem(
                path.name,
                parse_problem.code,
                parse_problem.record_index,
                parse_problem.message,
            ))
    parse_offset = parse_problem.offset if parse_problem is not None else len(raw)
    complete = gzip_eof and parse_problem is None and parse_offset == len(raw)
    return SegmentLoad(file=path.name,
        gzip_size=len(compressed),
        raw_size=len(raw),
        records=records,
        problems=problems,
        gzip_eof=gzip_eof,
        parse_offset=parse_offset,
        complete=complete,
    )


def iter_segments(archive_dir: Path) -> Iterator[SegmentLoad]:
    """按卷名顺序逐卷产出；调用方持有一卷的时间就是其正文的存活期。"""
    for path in list_segments(archive_dir):
        yield load_segment(path)


def load_archive(archive_dir: Path) -> ArchiveLoad:
    """加载归档目录下所有分卷，按卷名顺序聚合。"""
    result = ArchiveLoad()
    for segment in iter_segments(archive_dir):
        result.segments.append(segment)
        result.records.extend(segment.records)
        result.problems.extend(segment.problems)
    return result
