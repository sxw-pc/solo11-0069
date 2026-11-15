"""静态资源发现用的合成 fixture：HTML 直连、CSS 递归、循环与拒绝分支。

这些路由挂在 ``/res/`` 下，不被 demo 首页引用，因此不会改变 demo 的既有
计数；测试可以单独把种子指向 ``/res/`` 来覆盖资源发现策略。
"""
from __future__ import annotations

import base64

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII=",
)
_HUGE_PNG = _PNG + b"\x00" * 32000
_NOTES = b"synthetic text resource used to exercise the MIME allowlist\n"

_INDEX = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<title>资源发现样例</title>"
    "<link rel=\"stylesheet\" href=\"style.css\">"
    "<link rel=\"stylesheet\" href=\"theme/alt.css\">"
    "<link rel=\"stylesheet\" href=\"http://203.0.113.7/ext.css\">"
    "<script src=\"app.js\"></script>"
    "</head><body><h1>资源发现样例</h1>"
    "<img src=\"img/logo.png\" srcset=\"img/logo.png 1x, img/logo-2x.png 2x\" "
    "alt=\"logo\">"
    "<picture><source srcset=\"img/hero.png 1x, img/hero-2x.png 2x\">"
    "<img src=\"img/hero-fallback.png\" alt=\"hero\"></picture>"
    "<img src=\"img/shared.png\" alt=\"shared\">"
    "<img src=\"img/notes.txt\" alt=\"notes\">"
    "<img src=\"img/huge.png\" alt=\"huge\">"
    "<img src=\"img/inline.png\" "
    "srcset=\"data:image/png;base64,AAAA 1x\" alt=\"inline\">"
    "<img src=\"/private/blocked.png\" alt=\"blocked\">"
    "<p><a href=\"/res/page2.html\">第二个页面</a>"
    "<a href=\"http://203.0.113.7/outside\">站外链接</a></p>"
    "</body></html>"
).encode("utf-8")

_PAGE2 = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<title>资源发现样例二</title><link rel=\"stylesheet\" href=\"style.css\">"
    "</head><body><h1>共享资源</h1>"
    "<img src=\"img/shared.png\" alt=\"shared\">"
    "<a href=\"/res/\">返回</a></body></html>"
).encode("utf-8")

_STYLE = (
    "@import url(\"nested/import.css\");\n"
    "@import \"theme/alt.css\";\n"
    "/* url(img/commented-out.png) 不是引用 */\n"
    ".fake::before{content:\"url(img/fake-token.png)\"}\n"
    "body{background-image:url(\"img/bg.png\")}\n"
    ".shared{background:url(\"img/shared.png\")}\n"
    ".inline{background:url(data:image/png;base64,AAAA)}\n"
    ".escaped{background:url(\"img/logo.png\")}\n"
).encode("utf-8")

_ALT = (
    "@import \"cycle-a.css\";\n"
    ".accent{background:url(\"../img/accent.png\")}\n"
).encode("utf-8")

_IMPORT = (
    "@import \"../deep.css\";\n"
    ".nested{background:url(\"../img/deep.png\")}\n"
).encode("utf-8")

_DEEP = ".deep{background:url(\"../img/deeper.png\")}\n".encode("utf-8")
_CYCLE_A = "@import \"cycle-b.css\";\n.a{background:url(\"../img/a.png\")}\n".encode("utf-8")
_CYCLE_B = "@import \"cycle-a.css\";\n.b{background:url(\"../img/b.png\")}\n".encode("utf-8")

_IMAGES = frozenset({"logo.png", "logo-2x.png", "hero.png", "hero-2x.png",
    "hero-fallback.png", "bg.png", "shared.png", "accent.png", "a.png", "b.png",
    "deep.png", "deeper.png", "inline.png", "blocked.png",
})

_DOCUMENTS = {
    "/res/": ("text/html; charset=utf-8", _INDEX),
    "/res/index.html": ("text/html; charset=utf-8", _INDEX),
    "/res/page2.html": ("text/html; charset=utf-8", _PAGE2),
    "/res/style.css": ("text/css; charset=utf-8", _STYLE),
    "/res/theme/alt.css": ("text/css; charset=utf-8", _ALT),
    "/res/nested/import.css": ("text/css; charset=utf-8", _IMPORT),
    "/res/deep.css": ("text/css; charset=utf-8", _DEEP),
    "/res/theme/cycle-a.css": ("text/css; charset=utf-8", _CYCLE_A),
    "/res/theme/cycle-b.css": ("text/css; charset=utf-8", _CYCLE_B),
    "/res/app.js": ("application/javascript", b"alert('never fetched');\n"),
    "/res/img/notes.txt": ("text/plain; charset=utf-8", _NOTES),
}


def respond_resource(path: str) -> tuple[str, bytes, str] | None:
    """返回 ``(content_type, body, route)``；不属于 /res/ 时返回 None。"""
    document = _DOCUMENTS.get(path)
    if document is not None:
        return document[0], document[1], "res-" + path.rsplit("/", 1)[-1]
    if path.startswith("/res/img/") and path.endswith(".png"):
        name = path.rsplit("/", 1)[-1]
        if name == "huge.png":
            return "image/png", _HUGE_PNG, "res-huge-png"
        if name in _IMAGES:
            return "image/png", _PNG, "res-png"
    return None
