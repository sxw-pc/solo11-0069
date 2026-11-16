"""有界 BFS 队列：深度随条目传递，URL 只入队一次，入口策略决定车道顺序。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

KIND_PAGE = "page"
KIND_SITEMAP = "sitemap"
KIND_ROBOTS = "robots"


@dataclass(frozen=True)
class FrontierItem:
    """待抓取 URL 及其深度、类别与来源溯源信息。"""
    url: str
    urlkey: str
    depth: int
    kind: str = KIND_PAGE
    source_url: str | None = None
    parent_sitemap: str | None = None


class Frontier:
    """三车道 FIFO frontier，调用方负责白名单与 robots 过滤。

    车道：
    - ``_head``：种子入口，任何策略下都最先出队，保证“先处理首页”；
    - ``_priority``：robots Sitemap 声明发现的页面；
    - ``_normal``：HTML 链接发现的页面。

    策略 A（``sitemap_priority``，默认）：head → priority → normal，
    sitemap 作为优先级高于首页链接的独立批次入口。
    策略 B（``fifo_merged``）：head → normal → priority，sitemap 页面在
    HTML BFS 链接之后补位。两车道内部各自保持 FIFO。
    """

    def __init__(self, priority_first: bool = True) -> None:
        self._head: deque[FrontierItem] = deque()
        self._priority: deque[FrontierItem] = deque()
        self._normal: deque[FrontierItem] = deque()
        self._seen: set[tuple[str, int]] = set()
        self.priority_first = priority_first

    def add(
        self,
        url: str,
        url_key: str,
        depth: int,
        *,
        kind: str = KIND_PAGE,
        source_url: str | None = None,
        parent_sitemap: str | None = None,
        priority: bool = False,
        head: bool = False,
    ) -> bool:
        """加入新条目；同 URL 同深度重复时不入队。"""
        marker = (url_key, depth)
        if marker in self._seen:
            return False
        self._seen.add(marker)
        item = FrontierItem(url=url,
            urlkey=url_key,
            depth=depth,
            kind=kind,
            source_url=source_url,
            parent_sitemap=parent_sitemap,
        )
        if head:
            self._head.append(item)
        elif priority:
            self._priority.append(item)
        else:
            self._normal.append(item)
        return True

    def _lanes(self) -> tuple[deque[FrontierItem], deque[FrontierItem]]:
        if self.priority_first:
            return self._priority, self._normal
        return self._normal, self._priority

    def pop_batch(self, limit: int) -> list[FrontierItem]:
        """按入口策略取至多 ``limit`` 条，车道内部保持 FIFO。

        种子车道非空时只返回种子：首页必须先单独扩展，其第一层链接进入
        对应车道后，入口策略才决定 sitemap 与 HTML 页面的先后，否则预算
        归属会被同批并发填充掩盖。
        """
        batch: list[FrontierItem] = []
        if self._head:
            while len(batch) < limit and self._head:
                batch.append(self._head.popleft())
            return batch
        first_lane, second_lane = self._lanes()
        while len(batch) < limit and first_lane:
            batch.append(first_lane.popleft())
        while len(batch) < limit and second_lane:
            batch.append(second_lane.popleft())
        return batch

    def __bool__(self) -> bool:
        return bool(self._head or self._priority or self._normal)

    def __len__(self) -> int:
        return len(self._head) + len(self._priority) + len(self._normal)
