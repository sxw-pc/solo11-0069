"""从 HTML 中提取链接；不执行脚本、不做 DOM 重写。"""
from __future__ import annotations

from html.parser import HTMLParser

_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "about:")


class _LinkParser(HTMLParser):

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
    """按出现顺序返回去重后的 href；编码错误不会中断抓取。"""
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
