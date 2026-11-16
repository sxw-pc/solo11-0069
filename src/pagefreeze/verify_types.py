"""核验结果的数据类型：问题、分卷覆盖、检查项与总结果。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VerifyIssue:
    """定位到文件与记录序号的问题。"""
    file: str
    record_index: int | None
    code: str
    message: str


@dataclass(frozen=True)
class SegmentCoverage:
    """单卷可读范围：gzip 是否到 EOF、WARC 解析到哪个解压字节。"""
    file: str
    gzip_eof: bool
    parse_offset: int
    raw_size: int
    complete: bool
    record_count: int


@dataclass(frozen=True)
class VerifyCheck:
    """一项高层检查的汇总状态。"""
    name: str
    status: str
    detail: str
    issue_count: int


@dataclass
class VerifyResult:
    """核验结论与可归档的统计。"""
    status: str
    stats: dict[str, int]
    checks: list[VerifyCheck]
    issues: list[VerifyIssue]
    coverage: list[SegmentCoverage] = field(default_factory=list)
