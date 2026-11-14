"""归档卷读取：容忍尾部损坏，同时把损坏位置交给核验器。"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from pathlib import Path
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
    """单个分卷的解压结果。"""
    file: str
    gzip_size: int
    raw_size: int
    records: list[WarcRecord] = field(default_factory=list)
    problems: list[ArchiveProblem] = field(default_factory=list)


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


def _gunzip_tolerant(data: bytes) -> tuple[bytes, list[tuple[str, str]]]:
    """逐块解压以保留损坏前已解出的字节。"""
    if not data:
        return b"", [("GZIP_EMPTY", "empty gzip segment")]
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
    if not problems and not decompressor.eof:
        problems.append(("GZIP_TRUNCATED", "gzip stream ended before trailer"))
    if not problems and decompressor.unused_data:
        problems.append(("GZIP_TRAILING_DATA", "gzip stream has trailing bytes"))
    return raw, problems


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
    raw, gzip_problems = _gunzip_tolerant(compressed)
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
    return SegmentLoad(file=path.name,
        gzip_size=len(compressed),
        raw_size=len(raw),
        records=records,
        problems=problems,
    )


def load_archive(archive_dir: Path) -> ArchiveLoad:
    """加载归档目录下所有分卷，按卷名顺序聚合。"""
    result = ArchiveLoad()
    for path in list_segments(archive_dir):
        segment = load_segment(path)
        result.segments.append(segment)
        result.records.extend(segment.records)
        result.problems.extend(segment.problems)
    return result
