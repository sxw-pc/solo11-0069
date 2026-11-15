"""有界 BFS 队列：深度随条目传递，URL 只入队一次，支持分层优先级。

层（tier）是显式的入口优先级：层号小的批次先派发，同层内保持 FIFO。
抓取策略用它表达“种子 → 站点地图声明页 → HTML 发现链接”等顺序；
调用方负责白名单与 robots 过滤。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class FrontierItem:
    """待抓取 URL 及其深度、类别、来源与所在层。"""
    url: str
    urlkey: str
    depth: int
    kind: str = "page"
    source: str | None = None
    tier: int = 0


class Frontier:
    """分层 FIFO frontier；同层先进先出，层号小的先派发。"""

    def __init__(self) -> None:
        self._queues: dict[int, deque[FrontierItem]] = {}
        self._seen: set[tuple[str, int, str]] = set()

    def add(self, item: FrontierItem) -> bool:
        """加入新条目；同 URL 同深度同类别的重复条目不入队。"""
        marker = (item.urlkey, item.depth, item.kind)
        if marker in self._seen:
            return False
        self._seen.add(marker)
        self._queues.setdefault(item.tier, deque()).append(item)
        return True

    def pop_batch(self, limit: int) -> list[FrontierItem]:
        """按层号升序、层内 FIFO 取至多 ``limit`` 条，用于一轮并发抓取。"""
        batch: list[FrontierItem] = []
        for tier in sorted(self._queues):
            queue = self._queues[tier]
            while queue and len(batch) < limit:
                batch.append(queue.popleft())
            if not queue:
                del self._queues[tier]
            if len(batch) >= limit:
                break
        return batch

    def __bool__(self) -> bool:
        return any(self._queues.values())

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._queues.values())
