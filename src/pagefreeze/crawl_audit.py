"""抓取审计模型：每条尝试的日志、拒绝目标与一次抓取的结果聚合。"""
from __future__ import annotations

from dataclasses import dataclass, field
from .fetch import FetchResult
from .links import ResourceRef

RESOURCE_OUTCOMES = ("archived", "skipped", "error", "duplicate", "rejected")


@dataclass(frozen=True)
class FetchLog:
    """一次抓取尝试（含未发连接的 robots 跳过）的审计记录。"""
    url: str
    urlkey: str
    depth: int
    status: int | None
    mime: str
    size: int
    outcome: str
    reason: str | None = None
    resource_depth: int = 0
    source_url: str | None = None
    kind: str | None = None
    raw_reference: str | None = None


@dataclass(frozen=True)
class RejectedTarget:
    """发现但未建立连接的非回环目标。"""
    source_url: str
    target: str
    reason: str
    kind: str | None = None
    raw_reference: str | None = None


@dataclass
class CrawlResult:
    """一次抓取的全部可归档响应与审计信息。"""
    hits: list[FetchResult]
    logs: list[FetchLog]
    rejected: list[RejectedTarget]
    resources: list[ResourceRef] = field(default_factory=list)

    @property
    def archived(self) -> int:
        return sum(1 for log in self.logs if log.outcome == "archived")

    @property
    def skipped(self) -> int:
        return sum(1 for log in self.logs if log.outcome != "archived")

    @property
    def bytes(self) -> int:
        return sum(hit.size for hit in self.hits)

    def skip_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for log in self.logs:
            if log.outcome != "archived" and log.reason:
                counts[log.reason] = counts.get(log.reason, 0) + 1
        return dict(sorted(counts.items()))

    def resource_outcomes(self) -> dict[str, int]:
        """按引用边结果计数；``resource_depth=0`` 时全部为零。"""
        counts = {name: 0 for name in RESOURCE_OUTCOMES}
        for reference in self.resources:
            counts[reference.outcome] = counts.get(reference.outcome, 0) + 1
        return dict(sorted(counts.items()))

    def resource_kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for reference in self.resources:
            counts[reference.kind] = counts.get(reference.kind, 0) + 1
        return dict(sorted(counts.items()))

    def references_for(self, target: str) -> list[ResourceRef]:
        """返回指向某规范 URL 的全部引用边，用于来源回查。"""
        return [reference for reference in self.resources
                if reference.target == target]

    def cycles(self) -> list[ResourceRef]:
        """返回被判定为导入环的引用边。"""
        return [reference for reference in self.resources if reference.cycle]
