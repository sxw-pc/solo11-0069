"""确定性合成的 mock 页面与响应定义，不含任何真实站点或凭据。"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
from ..util import gzip_compress
from .resources import respond_resource


@dataclass(frozen=True)
class MockResponse:
    """mock 站点的一个完整响应，时间类头由 server 统一注入。"""
    status: int
    reason: str
    content_type: str
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()
    delay_seconds: float = 0.0
_CSS = ("body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;"
    "padding:0 1rem;color:#20242a;background:#f7f8fa}"
    "a{color:#175fa3}nav{display:flex;gap:1rem;flex-wrap:wrap}"
    ".card{border:1px solid #ccd2da;border-radius:.5rem;padding:1rem;margin:1rem 0}"
    ".muted{color:#59636f}").encode("utf-8")
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII=")
_ROBOTS = "User-agent: *\nCrawl-delay: 0\nDisallow: /private/\n"
_DUPLICATE = ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
    "<title>重复载荷示例</title><body><h1>相同响应体</h1>"
    "<p>两个不同 URL 返回完全相同的字节，用于 revisit 去重验证。</p>"
    "</body></html>"
).encode("utf-8")


def _page(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body>{body}</body></html>"
    ).encode("utf-8")
_ROOT = _page("封页台离线样例首页",
    "<h1>封页台样例站点</h1>"
    "<p>全部内容均为本地合成，仅用于离线归档评测。</p>"
    "<link rel=\"stylesheet\" href=\"/assets/site.css\">"
    "<img src=\"/assets/dot.png\" alt=\"一像素样例\">"
    "<nav>"
    "<a href=\"/list?page=1#part-a\">分页列表</a>"
    "<a href=\"/redirect/301\">两级重定向</a>"
    "<a href=\"/dup/a\">重复载荷 A</a>"
    "<a href=\"/dup/b\">重复载荷 B</a>"
    "<a href=\"/gzip\">gzip 页面</a>"
    "<a href=\"/status/404\">404 示例</a>"
    "<a href=\"/status/500\">500 示例</a>"
    "<a href=\"/assets/site.css\">样式表</a>"
    "<a href=\"/assets/dot.png\">图片</a>"
    "<a href=\"/huge\">超大响应</a>"
    "<a href=\"/slow?slow=1\">慢响应</a>"
    "<a href=\"/zh/\">中文页面</a>"
    "<a href=\"/private/secret\">robots 禁止路径</a>"
    "<a href=\"http://203.0.113.7/outside\">站外链接</a>"
    "</nav>",
)


def _list_page(page_number: int) -> bytes:
    previous = (f"<a href=\"?page={page_number - 1}#rows\">上一页</a>"
        if page_number > 1
        else "<span class=\"muted\">上一页不可用</span>")
    following = (f"<a href=\"?page={page_number + 1}#rows\">下一页</a>"
        if page_number < 3
        else "<span class=\"muted\">下一页不可用</span>")
    body = (f"<h1>分页列表 第 {page_number} 页</h1>"
        f"<div class=\"card\" id=\"rows\">第 {page_number} 页的合成条目</div>"
        f"<nav>{previous}{following}<a href=\"/\">返回首页</a></nav>")
    return _page(f"分页列表 {page_number}", body)


def _redirect_page(target: str) -> bytes:
    return _page("重定向", f"<p>即将前往 <code>{target}</code></p>")


def _not_found(path: str) -> bytes:
    return _page("404 合成页面",
        f"<h1>未找到</h1><p>路径 <code>{path}</code> 不存在。</p>"
        "<p><a href=\"/\">返回首页</a></p>",
    )


def _server_error() -> bytes:
    return _page("500 合成页面",
        "<h1>服务端错误</h1><p>这是用于评测的固定 500 响应。</p>",
    )


def _huge_page() -> bytes:
    payload = "X" * 32000
    return _page("超大响应",
        f"<h1>超大响应</h1><pre>{payload}</pre>",
    )


def _chinese_page() -> bytes:
    return _page("中文编码样例",
        "<h1>中文编码样例</h1><p>这段文字用于验证 UTF-8 与 Content-Type 的回放保留。</p>"
        "<p>归档应原样保存字节，而不是重新排版。</p>",
    )


def _gzip_response() -> MockResponse:
    source = _page("gzip 压缩页面",
        "<h1>gzip 载荷</h1><p>响应体由服务端压缩，归档保存压缩后的原始字节。</p>",
    )
    compressed = gzip_compress(source)
    return MockResponse(200,
        "OK",
        "text/html; charset=utf-8",
        compressed,
        (("Content-Encoding", "gzip"), ("X-Mock-Route", "gzip")),
    )


def _ok(content_type: str, body: bytes, route: str) -> MockResponse:
    return MockResponse(200, "OK", content_type, body, (("X-Mock-Route", route),))


def _redirect(status: int, target: str, route: str) -> MockResponse:
    reason = "Moved Permanently" if status == 301 else "Found"
    return MockResponse(status,
        reason,
        "text/html; charset=utf-8",
        _redirect_page(target),
        (("Location", target), ("X-Mock-Route", route)),
    )


def respond(raw_target: str) -> MockResponse:
    """按 path/query 生成响应；所有分支均为纯函数。"""
    parsed = urlsplit(raw_target)
    path = parsed.path or "/"
    query = parse_qs(parsed.query, keep_blank_values=True)
    if path.startswith("/res/"):
        resource = respond_resource(path)
        if resource is not None:
            content_type, body, route = resource
            return _ok(content_type, body, route)
    if path == "/":
        return _ok("text/html; charset=utf-8", _ROOT, "root")
    if path == "/list":
        try:
            page_number = int(query.get("page", ["1"])[0])
        except ValueError:
            page_number = 1
        if 1 <= page_number <= 3:
            return _ok("text/html; charset=utf-8",
                _list_page(page_number),
                f"list-{page_number}",
            )
        return MockResponse(404,
            "Not Found",
            "text/html; charset=utf-8",
            _not_found(path),
            (("X-Mock-Route", "list-invalid"),),
        )
    if path == "/redirect/301":
        return _redirect(301, "/redirect/302", "redirect-301")
    if path == "/redirect/302":
        return _redirect(302, "/landing", "redirect-302")
    if path == "/landing":
        return _ok("text/html; charset=utf-8",
            _page("重定向终点", "<h1>重定向终点</h1><p><a href=\"/\">返回首页</a></p>"),
            "landing",
        )
    if path in ("/dup/a", "/dup/b"):
        return _ok("text/html; charset=utf-8", _DUPLICATE, path.rsplit("/", 1)[-1])
    if path == "/gzip":
        return _gzip_response()
    if path == "/status/404":
        return MockResponse(404,
            "Not Found",
            "text/html; charset=utf-8",
            _not_found(path),
            (("X-Mock-Route", "status-404"),),
        )
    if path == "/status/500":
        return MockResponse(500,
            "Internal Server Error",
            "text/html; charset=utf-8",
            _server_error(),
            (("X-Mock-Route", "status-500"),),
        )
    if path == "/assets/site.css":
        return _ok("text/css; charset=utf-8", _CSS, "css")
    if path == "/assets/dot.png":
        return _ok("image/png", _PNG, "png")
    if path == "/robots.txt":
        return _ok("text/plain; charset=utf-8", _ROBOTS.encode("utf-8"), "robots")
    if path == "/huge":
        return _ok("text/html; charset=utf-8", _huge_page(), "huge")
    if path == "/slow":
        delay = 0.0 if query.get("slow", ["1"])[0] == "0" else 1.0
        return MockResponse(200,
            "OK",
            "text/html; charset=utf-8",
            _page("慢响应", "<h1>慢响应</h1><p>客户端应在此响应前超时。</p>"),
            (("X-Mock-Route", "slow"),),
            delay_seconds=delay,
        )
    if path == "/private/secret":
        return _ok("text/html; charset=utf-8",
            _page("robots 禁止页面", "<h1>该页面应由 robots 规则跳过</h1>"),
            "private",
        )
    if path == "/zh/":
        return _ok("text/html; charset=utf-8", _chinese_page(), "zh")
    return MockResponse(404,
        "Not Found",
        "text/html; charset=utf-8",
        _not_found(path),
        (("X-Mock-Route", "missing"),),
    )
