"""robots 声明的 Sitemap XML 解析（urlset / sitemapindex）。

安全约束：
- 只用标准库 SAX/pulldom 增量解析，显式关闭外部一般实体与外部参数实体，
  并通过词法处理器在见到任何 DOCTYPE 声明时立即报错——因此不读取 DTD、
  不解析外部实体、不访问任何外部资源，也不接受内部实体展开；
- 不做 XInclude、XSLT、schema 校验或任何会触发网络/文件访问的处理；
- 单个文档解析失败只抛出 :class:`SitemapParseError`，由抓取层记录后继续
  处理其他 Sitemap 声明与普通 HTML frontier。
"""
from __future__ import annotations

import xml.sax
from dataclasses import dataclass
from xml.dom.pulldom import CHARACTERS, END_ELEMENT, START_ELEMENT, parseString
from xml.sax import SAXException
from .errors import PagefreezeError
from .policy import normalize_url

_MAX_LOC_CHARS = 4096


class SitemapParseError(PagefreezeError):
    """Sitemap XML 无法解析、根元素未知或缺少必需字段。"""

    def __init__(self, message: str) -> None:
        super().__init__("SITEMAP_PARSE_ERROR", message)


class _DtdRejector:
    """SAX 词法处理器：任何 DOCTYPE 都视为非法，阻断实体声明入口。"""

    def comment(self, text: str) -> None:  # pragma: no cover - 空实现
        """允许注释，但无需处理。"""

    def startDTD(self, name: str, public_id: str | None, system_id: str | None) -> None:
        raise xml.sax.SAXNotSupportedException("DOCTYPE declarations are not allowed")

    def endDTD(self) -> None:  # pragma: no cover - startDTD 已先抛错
        """DTD 结束回调；正常路径不可达。"""

    def startCDATA(self) -> None:
        """CDATA 段开始，无需处理。"""

    def endCDATA(self) -> None:
        """CDATA 段结束，无需处理。"""

    def startEntity(self, name: str) -> None:
        """实体引用开始，无需处理（外部实体已由 feature 关闭）。"""

    def endEntity(self, name: str) -> None:
        """实体引用结束，无需处理。"""


@dataclass(frozen=True)
class SitemapEntry:
    """一个 ``<loc>`` 解析结果；loc 已相对解析为绝对 URL 文本。"""
    loc: str
    is_index: bool
    raw_loc: str


@dataclass(frozen=True)
class SitemapParseResult:
    """单张 Sitemap 的解析结果：页面 loc 与子表 loc 分开返回。"""
    pages: tuple[SitemapEntry, ...]
    child_sitemaps: tuple[SitemapEntry, ...]


def _local_name(name: str) -> str:
    """剥除默认/前缀命名空间，返回元素本地名。"""
    if "}" in name:
        return name.rsplit("}", 1)[1]
    if ":" in name:
        return name.rsplit(":", 1)[1]
    return name


def _make_parser() -> xml.sax.XMLReader:
    """构造禁用全部外部资源解析、拒绝 DOCTYPE 的 SAX 解析器。"""
    parser = xml.sax.make_parser()
    parser.setFeature(xml.sax.handler.feature_external_ges, False)
    parser.setFeature(xml.sax.handler.feature_external_pes, False)
    parser.setFeature(xml.sax.handler.feature_namespaces, True)
    parser.setProperty(xml.sax.handler.property_lexical_handler, _DtdRejector())
    return parser


def parse_sitemap(body: bytes, base_url: str) -> SitemapParseResult:
    """解析一张 Sitemap 字节流；``base_url`` 用于相对 loc 的基准解析。

    支持根元素 ``urlset`` 与 ``sitemapindex``（默认命名空间可带可不带）。
    畸形 XML、DOCTYPE、未知根元素或无法规范化的 loc 都会抛
    :class:`SitemapParseError`，调用方据此只记录错误而不中断其他声明。
    """
    if not isinstance(body, (bytes, bytearray)):
        raise SitemapParseError("sitemap body must be bytes")
    try:
        document = bytes(body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SitemapParseError(f"sitemap is not valid UTF-8: {exc}") from exc
    if not document.lstrip().startswith("<"):
        raise SitemapParseError("sitemap document does not start with an XML element")
    parser = _make_parser()
    root: str | None = None
    pages: list[SitemapEntry] = []
    children: list[SitemapEntry] = []
    current_loc: list[str] | None = None
    depth = 0
    loc_depth = -1
    try:
        for event, node in parseString(document, parser=parser):
            if event == START_ELEMENT:
                tag = _local_name(node.nodeName)
                if depth == 0:
                    if tag not in ("urlset", "sitemapindex"):
                        raise SitemapParseError(f"unknown sitemap root element: {tag}")
                    root = tag
                depth += 1
                if tag == "loc":
                    current_loc = []
                    loc_depth = depth
            elif event == END_ELEMENT:
                tag = _local_name(node.nodeName)
                if tag == "loc" and current_loc is not None:
                    raw = "".join(current_loc).strip()
                    _collect_loc(root, raw, base_url, pages, children)
                    current_loc = None
                    loc_depth = -1
                depth -= 1
            elif event == CHARACTERS and current_loc is not None and depth == loc_depth:
                if sum(len(part) for part in current_loc) < _MAX_LOC_CHARS:
                    current_loc.append(node.data)
    except SitemapParseError:
        raise
    except SAXException as exc:
        raise SitemapParseError(f"malformed sitemap XML: {exc.getMessage()}") from exc
    except Exception as exc:  # 解析器内部异常统一转为稳定错误码
        raise SitemapParseError(f"cannot parse sitemap: {exc}") from exc
    if root is None:
        raise SitemapParseError("sitemap document has no root element")
    return SitemapParseResult(pages=tuple(pages), child_sitemaps=tuple(children))


def _collect_loc(
    root: str | None,
    raw: str,
    base_url: str,
    pages: list[SitemapEntry],
    children: list[SitemapEntry],
) -> None:
    if not raw:
        raise SitemapParseError("empty <loc> element")
    try:
        absolute = normalize_url(raw, base_url)
    except PagefreezeError as exc:
        raise SitemapParseError(f"invalid sitemap loc {raw!r}: {exc.message}") from exc
    entry = SitemapEntry(loc=absolute, is_index=root == "sitemapindex", raw_loc=raw)
    if root == "sitemapindex":
        children.append(entry)
    else:
        pages.append(entry)
