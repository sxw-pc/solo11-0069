"""链接与静态资源发现；只解析文本，不执行脚本、不改写 HTML/CSS。

导航链接（``<a>``/``<area>`` 的 href）与资源引用分开处理：前者进入页面
BFS，后者记录为可审计的 ``ResourceRef``，由 crawl 层决定是否抓取。CSS
中的相对引用始终相对**该 CSS 自身的 URL** 解析，而不是最初 HTML 页面。
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit
from .css import parse_css_tokens
from .errors import PolicyError
from .policy import normalize_url

_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "about:")
_APPROVED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class RawReference:
    """尚未解析为绝对 URL 的引用 token；``reason`` 表示格式不可用。"""
    raw: str
    kind: str
    tag: str | None = None
    attribute: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ResourceRef:
    """一条静态资源引用边：谁引用、原始 token、解析目标、类型与资源深度。"""
    source_url: str
    raw: str
    target: str | None
    kind: str
    depth: int
    outcome: str = "discovered"
    reason: str | None = None
    duplicate: bool = False
    tag: str | None = None
    attribute: str | None = None
    cycle: bool = False

    @property
    def reference(self) -> str:
        """原始引用文本的别名，便于审计调用方读取。"""
        return self.raw

    @property
    def referenced_url(self) -> str | None:
        """解析后绝对 URL 的别名；不可解析时为 None。"""
        return self.target


class _LinkParser(HTMLParser):
    """只提取导航链接；脚本、样式资源不在此解析。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in ("a", "area"):
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                candidate = value.strip()
                if candidate and not candidate.lower().startswith(_SKIP_SCHEMES):
                    self.links.append(candidate)


def extract_links(html: bytes) -> list[str]:
    """按出现顺序返回去重后的导航 href；编码错误不会中断抓取。"""
    parser = _LinkParser()
    try:
        parser.feed(html.decode("utf-8", errors="replace"))
    except ValueError:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for link in parser.links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


class _ResourceParser(HTMLParser):
    """提取 rel=stylesheet、img/source 的 src 与 srcset；忽略 script。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[RawReference] = []

    def _add(self, raw: str | None, kind: str, tag: str, attribute: str) -> None:
        if raw is None:
            return
        candidate = raw.strip()
        if candidate:
            self.items.append(RawReference(candidate, kind, tag, attribute))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "script":
            return
        values = {key.lower(): value for key, value in attrs}
        if name == "link":
            rel = (values.get("rel") or "").lower().split()
            if "stylesheet" in rel:
                self._add(values.get("href"), "stylesheet", name, "href")
            return
        if name in ("img", "source"):
            self._add(values.get("src"), "image", name, "src")
            srcset = values.get("srcset")
            if srcset:
                for candidate in parse_srcset(srcset):
                    self.items.append(
                        RawReference(candidate, "srcset", name, "srcset"))
            return


def parse_srcset(value: str) -> list[str]:
    """按 HTML srcset 候选列表解析；逗号只在不位于 URL/括号内时分隔候选。"""
    candidates: list[str] = []
    text = value or ""
    index = 0
    length = len(text)
    while index < length:
        while index < length and (text[index].isspace() or text[index] == ","):
            index += 1
        if index >= length:
            break
        start = index
        while index < length and not text[index].isspace():
            index += 1
        url = text[start:index]
        while url.endswith(","):
            url = url[:-1]
        if url:
            candidates.append(url)
        depth = 0
        quote: str | None = None
        while index < length:
            char = text[index]
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            elif char == "," and depth == 0:
                index += 1
                break
            index += 1
    return candidates


def _resolve(raw: RawReference, source_url: str, depth: int) -> ResourceRef:
    value = (raw.raw or "").strip()
    reason = raw.reason
    if not reason:
        if not value:
            reason = "empty_reference"
        elif value.startswith("#"):
            reason = "fragment_only"
        elif value.lower().startswith(("var(", "env(", "attr(")):
            # url(var(--x)) 之类是待运行时求值的 CSS 函数，不是静态地址。
            reason = "css_function"
        else:
            scheme = urlsplit(value).scheme.lower()
            if scheme == "data":
                reason = "data_uri"
            elif scheme and scheme not in _APPROVED_SCHEMES:
                reason = "unsupported_scheme"
    target: str | None = None
    if reason is None:
        try:
            target = normalize_url(value, base=source_url)
        except PolicyError as exc:
            reason = ("unsupported_scheme" if exc.code == "URL_SCHEME"
                      else "invalid_url")
    return ResourceRef(source_url=source_url,
        raw=value,
        target=target,
        kind=raw.kind,
        depth=depth,
        outcome="discovered" if target else "skipped",
        reason=reason,
        tag=raw.tag,
        attribute=raw.attribute,
    )


def discover_html_resources(
    html: bytes, source_url: str, depth: int = 1,
) -> list[ResourceRef]:
    """解析一份 HTML 文档中的静态资源引用，并以文档 URL 为基准解析。"""
    parser = _ResourceParser()
    try:
        parser.feed(html.decode("utf-8", errors="replace"))
    except ValueError:
        return []
    return [_resolve(item, source_url, depth) for item in parser.items]


def parse_css_resources(css: bytes) -> list[RawReference]:
    """把 CSS token 适配为 HTML 侧的原始引用表示，供统一解析。"""
    return [RawReference(item.raw, item.kind, "css", item.kind.removeprefix("css-"),
                         item.reason)
        for item in parse_css_tokens(css)]


def discover_css_resources(
    css: bytes, source_url: str, depth: int = 1,
) -> list[ResourceRef]:
    """解析一份 CSS；相对 URL 以该 CSS 的 URL 为基准。"""
    return [_resolve(item, source_url, depth) for item in parse_css_resources(css)]


def discover_resources(
    content: bytes, content_type: str, source_url: str, depth: int = 1,
) -> list[ResourceRef]:
    """按内容类型选择 HTML 或 CSS 解析器；其他类型不产生资源引用。"""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime == "text/html":
        return discover_html_resources(content, source_url, depth)
    if mime == "text/css":
        return discover_css_resources(content, source_url, depth)
    return []


def resolve_reference(
    raw: str,
    kind: str,
    source_url: str,
    depth: int,
    tag: str | None = None,
    attribute: str | None = None,
) -> ResourceRef:
    """把一条已知出处的引用解析为目标 URL；供重定向等非解析器场景复用。"""
    return _resolve(RawReference(raw, kind, tag, attribute), source_url, depth)


# 兼容更直白的调用名：资源提取与导航链接提取保持分离。
extract_html_resources = discover_html_resources
extract_css_resources = discover_css_resources
extract_resources = discover_resources
