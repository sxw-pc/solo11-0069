"""归档卷读取：逐卷流式处理，容忍尾部损坏并记录覆盖边界。

核验器不再把所有卷的完整记录同时留在内存中：:func:`iter_segments` 每次只
保留当前卷，调用方把跨卷所需的轻量信息落盘后即可释放。每个 :class:`SegmentLoad`
同时携带 gzip EOF 与 WARC 解析边界，供核验器区分“确认不存在”与“可能落在未读
尾部”。:func:`load_archive` 保留全量内存形态，供索引重扫与回放等既有调用方使用。
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

    ``gzip_complete`` 表示 gzip 流是否到达正常 EOF；``parsed_offset`` 是解压后
    字节流中已成功解析 WARC 记录覆盖到的位置（等于下一条记录应开始的偏移）。
    两者同时为真且无结构问题时，``fully_covered`` 才成立，查表 miss 才能被当作
    确认缺失。
    """
    file: str
    gzip_size: int
    raw_size: int
    records: list[WarcRecord] = field(default_factory=list)
    problems: list[ArchiveProblem] = field(default_factory=list)
    gzip_complete: bool = False
    parsed_offset: int = 0
    readable: bool = True

    @property
    def fully_covered(self) -> bool:
        """该卷是否被完整读取并解析到 EOF。"""
        return self.readable and self.gzip_complete and self.parsed_offset == self.raw_size


@dataclass
class ArchiveLoad:
    """全部已解析卷与聚合问题（全量内存形态，供非核验调用方使用）。"""
    segments: list[SegmentLoad] = field(default_factory=list)
    records: list[WarcRecord] = field(default_factory=list)
    problems: list[ArchiveProblem] = field(default_factory=list)


def list_segments(archive_dir: Path) -> list[Path]:
    """按文件名排序列出 ``seg-*.warc.gz``。"""
    if not archive_dir.is_dir():
        return []
    return sorted(archive_dir.glob("seg-*.warc.gz"), key=lambda item: item.name)


def _gunzip_tolerant(data: bytes) -> tuple[bytes, list[tuple[str, str]], bool]:
    """逐块解压以保留损坏前已解出的字节；同时报告 gzip 是否完整 EOF。"""
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
    # gzip 成员正常到达 EOF（eof=True）即视为完整：尾随字节只产生
    # GZIP_TRAILING_DATA 结构问题，不影响 WARC 已全部解压这一事实，因此不应把
    # 查表 miss 降级为未判定。
    complete = not problems and decompressor.eof
    if not problems and not decompressor.eof:
        problems.append(("GZIP_TRUNCATED", "gzip stream ended before trailer"))
    if not problems and decompressor.unused_data:
        problems.append(("GZIP_TRAILING_DATA", "gzip stream has trailing bytes"))
    return b"".join(chunks), problems, complete


def load_segment(path: Path) -> SegmentLoad:
    """读取一个分卷；文件不可读或内容损坏都转为问题列表，并记录覆盖边界。"""
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        return SegmentLoad(file=path.name,
            gzip_size=0,
            raw_size=0,
            problems=[ArchiveProblem(path.name, "FILE_READ_ERROR", 0, f"cannot read segment: {exc}")],
            readable=False,
        )
    raw, gzip_problems, gzip_complete = _gunzip_tolerant(compressed)
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
        parsed_offset = parse_problem.offset
    else:
        parsed_offset = len(raw)
    return SegmentLoad(file=path.name,
        gzip_size=len(compressed),
        raw_size=len(raw),
        records=records,
        problems=problems,
        gzip_complete=gzip_complete,
        parsed_offset=parsed_offset,
        readable=True,
    )


def iter_segments(archive_dir: Path) -> Iterator[SegmentLoad]:
    """按卷名顺序逐卷产出；调用方处理完当前卷即可释放其记录与正文。"""
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
