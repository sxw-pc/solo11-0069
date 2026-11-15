"""有界 BFS 队列：深度随条目传递，URL 只入队一次。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class FrontierItem:
    """待抓取 URL 及其页面深度；资源条目额外携带来源与资源深度。"""
    url: str
    urlkey: str
    depth: int
    resource_depth: int = 0
    source_url: str | None = None
    kind: str | None = None
    raw_reference: str | None = None
    lineage: tuple[str, ...] = ()

    @property
    def is_resource(self) -> bool:
        return self.resource_depth > 0


class Frontier:
    """FIFO frontier，调用方负责白名单与 robots 过滤。"""

    def __init__(self) -> None:
        self._queue: deque[FrontierItem] = deque()
        self._seen: set[tuple[str, int, int]] = set()

    def add(self, url: str, url_key: str, depth: int, *,
        resource_depth: int = 0,
        source_url: str | None = None,
        kind: str | None = None,
        raw_reference: str | None = None,
        lineage: tuple[str, ...] = (),
    ) -> bool:
        """加入新条目；同 URL、同页面深度、同资源深度重复时不入队。"""
        marker = (url_key, depth, resource_depth)
        if marker in self._seen:
            return False
        self._seen.add(marker)
        self._queue.append(FrontierItem(url=url,
            urlkey=url_key,
            depth=depth,
            resource_depth=resource_depth,
            source_url=source_url,
            kind=kind,
            raw_reference=raw_reference,
            lineage=lineage,
        ))
        return True

    def pop_batch(self, limit: int) -> list[FrontierItem]:
        """按 FIFO 顺序取至多 ``limit`` 条，用于一轮并发抓取。"""
        batch: list[FrontierItem] = []
        while self._queue and len(batch) < limit:
            batch.append(self._queue.popleft())
        return batch

    def drain(self) -> list[FrontierItem]:
        """取出全部剩余条目；用于记录达到页数上限而未抓取的条目。"""
        remaining = list(self._queue)
        self._queue.clear()
        return remaining

    def __bool__(self) -> bool:
        return bool(self._queue)

    def __len__(self) -> int:
        return len(self._queue)

