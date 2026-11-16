"""抓取审计数据类型与分类辅助，供 crawl 与 sitemap 发现层共享。"""
from __future__ import annotations

from dataclasses import dataclass
from .frontier import FrontierItem, KIND_PAGE

KIND_PAGE_VALUE = KIND_PAGE


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
    kind: str = KIND_PAGE_VALUE
    reason: str | None = None
    source_url: str | None = None
    parent_sitemap: str | None = None


@dataclass(frozen=True)
class RejectedTarget:
    """发现但未建立连接的目标（跨 origin Sitemap 或非回环链接）。"""
    source_url: str
    target: str
    reason: str
    source_kind: str = KIND_PAGE_VALUE


def log_for(
    item: FrontierItem, status: int | None, mime: str, size: int,
    outcome: str, reason: str | None = None,
) -> FetchLog:
    """由 frontier 条目构造一条审计日志，继承类别与溯源字段。"""
    return FetchLog(url=item.url,
        urlkey=item.urlkey,
        depth=item.depth,
        status=status,
        mime=mime,
        size=size,
        outcome=outcome,
        kind=item.kind,
        reason=reason,
        source_url=item.source_url,
        parent_sitemap=item.parent_sitemap,
    )


def is_xml_mime(mime: str) -> bool:
    """识别 Sitemap 可接受的 XML 媒体类型（含 +xml 子类）。"""
    base = mime.strip().lower()
    return base in ("application/xml", "text/xml") or base.endswith("+xml")
