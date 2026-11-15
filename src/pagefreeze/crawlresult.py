"""抓取结果的数据类型：审计日志、回环外拒绝与按 origin 的限速台账。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from .frontier import FrontierItem

if TYPE_CHECKING:
    from .fetch import FetchResult


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


@dataclass(frozen=True)
class RejectedTarget:
    """发现但未建立连接的非回环目标。"""
    source_url: str
    target: str
    reason: str


@dataclass
class OriginStats:
    """单个 origin 的限速合规与 robots 跳过台账。"""
    origin: str
    request_count: int = 0
    request_starts: list[float] = field(default_factory=list)
    robots_skipped: int = 0
    effective_interval_ms: int = 0
    robots_delay_ms: int | None = None
    robots_warnings: tuple[str, ...] = ()

    def record_start(self, when: float) -> None:
        """记录一次请求起始时间；调用方传入的是相对抓取开始的秒偏移。"""
        self.request_count += 1
        self.request_starts.append(when)

    def to_dict(self) -> dict[str, object]:
        """确定性摘要：不含墙钟起始时间，保证固定时钟下报告逐字节一致。"""
        return {"origin": self.origin,
            "request_count": self.request_count,
            "robots_skipped": self.robots_skipped,
            "effective_interval_ms": self.effective_interval_ms,
            "robots_delay_ms": self.robots_delay_ms,
            "robots_warnings": list(self.robots_warnings),
        }


@dataclass
class CrawlResult:
    """一次抓取的全部可归档响应与审计信息。"""
    hits: list[FetchResult]
    logs: list[FetchLog]
    rejected: list[RejectedTarget]
    origin_stats: dict[str, OriginStats] = field(default_factory=dict)

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

    def origin_summary(self) -> list[dict[str, object]]:
        """按 origin 名稳定排序的汇总，供配置摘要与报告使用。"""
        return [self.origin_stats[name].to_dict()
            for name in sorted(self.origin_stats)]


def log_for(
    item: FrontierItem, status: int | None, mime: str, size: int,
    outcome: str, reason: str | None = None,
) -> FetchLog:
    """由 frontier 条目构造一条审计日志。"""
    return FetchLog(url=item.url,
        urlkey=item.urlkey,
        depth=item.depth,
        status=status,
        mime=mime,
        size=size,
        outcome=outcome,
        reason=reason,
    )
