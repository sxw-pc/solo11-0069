"""有界 BFS 队列：深度随条目传递，URL 只入队一次。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class FrontierItem:
    """待抓取 URL 及其深度。"""
    url: str
    urlkey: str
    depth: int


class Frontier:
    """FIFO frontier，调用方负责白名单与 robots 过滤。"""

    def __init__(self) -> None:
        self._queue: deque[FrontierItem] = deque()
        self._seen: set[tuple[str, int]] = set()

    def add(self, url: str, url_key: str, depth: int) -> bool:
        """加入新条目；同 URL 同深度重复时不入队。"""
        marker = (url_key, depth)
        if marker in self._seen:
            return False
        self._seen.add(marker)
        self._queue.append(FrontierItem(url=url, urlkey=url_key, depth=depth))
        return True

    def pop_batch(self, limit: int) -> list[FrontierItem]:
        """按 FIFO 顺序取至多 ``limit`` 条，用于一轮并发抓取。"""
        batch: list[FrontierItem] = []
        while self._queue and len(batch) < limit:
            batch.append(self._queue.popleft())
        return batch

    def __bool__(self) -> bool:
        return bool(self._queue)

    def __len__(self) -> int:
        return len(self._queue)
