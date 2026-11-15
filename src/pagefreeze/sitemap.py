"""站点地图 XML 的安全解析：urlset 与 sitemapindex，禁止 DOCTYPE/外部实体。

解析器只接受 sitemaps 语义的两种根元素，按局部名匹配以兼容默认
namespace；``loc`` 文本原样返回（相对解析由抓取层用文件 URL 做 urljoin）。
任何 DOCTYPE 或 ENTITY 声明都在解析前拒绝，保证不访问外部资源。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from .errors import SitemapError

_DOCTYPE_RE = re.compile(br"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_ROOT_KINDS = ("urlset", "sitemapindex")


@dataclass(frozen=True)
class SitemapDocument:
    """一个解析成功的站点地图：根类型 + 文档顺序的原始 loc 文本。"""
    kind: str
    locs: tuple[str, ...]


def _local_name(tag: str) -> str:
    """去掉 ``{namespace}`` 前缀，返回局部元素名。"""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_sitemap_xml(body: bytes) -> SitemapDocument:
    """解析站点地图字节；失败抛出 ``SitemapError``，绝不访问外部资源。"""
    if _DOCTYPE_RE.search(body):
        raise SitemapError("SITEMAP_MALFORMED",
            "sitemap XML must not contain DOCTYPE or ENTITY declarations")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SitemapError("SITEMAP_MALFORMED", f"cannot parse sitemap XML: {exc}") from exc
    kind = _local_name(root.tag).lower()
    if kind not in _ROOT_KINDS:
        raise SitemapError("SITEMAP_MALFORMED",
            f"unsupported sitemap root element: {_local_name(root.tag)}")
    locs: list[str] = []
    for entry in root:
        if _local_name(entry.tag).lower() not in ("url", "sitemap"):
            continue
        for child in entry:
            if _local_name(child.tag).lower() == "loc":
                text = (child.text or "").strip()
                if text:
                    locs.append(text)
                break
    return SitemapDocument(kind=kind, locs=tuple(locs))
