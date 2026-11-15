"""CSS 引用 token 解析：区分注释/字符串假 token，处理引号、括号、转义与 data:。

只做文本扫描，不解释 CSS 语义、不执行任何内容。``url()`` 与 ``@import`` 的
结果交给 ``links`` 层按内容类型与深度决定是否进入抓取。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class CssToken:
    """一个 CSS 引用 token；``reason`` 非空表示 token 格式不可用。"""
    raw: str
    kind: str
    reason: str | None = None


def _mask_comments(css: str) -> str:
    """用空格替换注释，同时保留字符串内的注释字样。"""
    chars = list(css)
    index = 0
    length = len(css)
    quote: str | None = None
    while index < length:
        char = css[index]
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
            index += 1
            continue
        if char == "/" and index + 1 < length and css[index + 1] == "*":
            end = css.find("*/", index + 2)
            end = length if end < 0 else end + 2
            for position in range(index, end):
                if chars[position] not in "\r\n":
                    chars[position] = " "
            index = end
            continue
        index += 1
    return "".join(chars)


def _unescape_css(value: str) -> str:
    """解码 CSS 转义；保留无法识别的内容而不是丢弃整条引用。"""
    result: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue
        index += 1
        if index >= length:
            break
        if value[index] in "\r\n\f":
            if value[index] == "\r" and index + 1 < length and value[index + 1] == "\n":
                index += 1
            index += 1
            continue
        if value[index] in _HEX:
            start = index
            while index < length and index - start < 6 and value[index] in _HEX:
                index += 1
            codepoint = int(value[start:index], 16)
            if codepoint == 0 or 0xD800 <= codepoint <= 0xDFFF or codepoint > 0x10FFFF:
                result.append("�")
            else:
                result.append(chr(codepoint))
            if index < length and value[index].isspace():
                index += 1
            continue
        result.append(value[index])
        index += 1
    return "".join(result)


def _read_css_string(text: str, index: int) -> tuple[str | None, int]:
    """读取带引号字符串，返回未转义值与结束位置。

    未闭合字符串按 CSS 的 bad-string 规则在换行处终止，使后续声明仍可扫描。
    """
    quote = text[index]
    index += 1
    buffer: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 < len(text):
                buffer.append(char)
                buffer.append(text[index + 1])
                index += 2
                continue
            index += 1
            continue
        if char == quote:
            return _unescape_css("".join(buffer)), index + 1
        if char in "\r\n\f":
            return None, index
        buffer.append(char)
        index += 1
    return None, index


def _read_url_token(text: str, index: int) -> tuple[str | None, int]:
    """从 ``url(`` 之后读取 URL，处理引号、括号与转义；未闭合返回 None。

    失败时仍扫描到声明结束（``}`` 或 ``;``），避免单个坏 token 吞掉后续引用。
    """
    def recovery(start: int) -> int:
        cursor = start
        while cursor < len(text) and text[cursor] not in "};":
            cursor += 1
        return cursor

    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and text[index] in ("'", '"'):
        quote_start = index
        raw, end = _read_css_string(text, index)
        if raw is None:
            # 未闭合字符串可能一直吞到 EOF，从开引号处按声明边界恢复。
            return None, recovery(quote_start)
        while end < len(text) and text[end].isspace():
            end += 1
        if end < len(text) and text[end] == ")":
            return raw, end + 1
        return None, recovery(end)
    buffer: list[str] = []
    depth = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            if index + 1 < len(text):
                buffer.append(char)
                buffer.append(text[index + 1])
                index += 2
                continue
            index += 1
            continue
        if char == "(":
            depth += 1
            buffer.append(char)
            index += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                return _unescape_css("".join(buffer).strip()), index + 1
            buffer.append(char)
            index += 1
            continue
        buffer.append(char)
        index += 1
    return None, index


def _ident_char(char: str) -> bool:
    return char.isalnum() or char in ("_", "-") or unicodedata.category(char)[0] in "LMN"


def _at_import(text: str, index: int) -> tuple[str | None, int] | None:
    """识别 ``@import`` 并读取其 URL；不是 import 时返回 None。"""
    end = index + 1
    while end < len(text) and _ident_char(text[end]):
        end += 1
    if text[index + 1:end].lower() != "import":
        return None
    cursor = end
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor < len(text) and text[cursor] in ("'", '"'):
        return _read_css_string(text, cursor)
    if text[cursor:cursor + 4].lower() == "url(":
        return _read_url_token(text, cursor + 4)
    return None, cursor


def parse_css_tokens(css: bytes) -> list[CssToken]:
    """解析 CSS 的 ``url()`` 与 ``@import``，跳过注释和普通字符串。"""
    text = _mask_comments(css.decode("utf-8", errors="replace"))
    items: list[CssToken] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("'", '"'):
            # 字符串里的 url(...) 字样不是引用；跳过整个字符串。
            _, index = _read_css_string(text, index)
            continue
        if char == "@":
            imported = _at_import(text, index)
            if imported is not None:
                raw, end = imported
                if raw is None:
                    items.append(CssToken("", "css-import", "malformed_reference"))
                else:
                    items.append(CssToken(raw, "css-import"))
                index = max(end, index + 1)
                continue
        if (char in ("u", "U") and text[index:index + 4].lower() == "url("
                and (index == 0 or not _ident_char(text[index - 1]))):
            raw, end = _read_url_token(text, index + 4)
            if raw is None:
                items.append(CssToken("", "css-url", "malformed_reference"))
            elif raw.lower().startswith(("var(", "env(", "attr(")):
                # 这些都是 CSS 值函数，不是资源 URL；留痕但不解析为地址。
                items.append(CssToken(raw, "css-url", "css_function"))
            else:
                items.append(CssToken(raw, "css-url"))
            index = max(end, index + 1)
            continue
        index += 1
    return items
