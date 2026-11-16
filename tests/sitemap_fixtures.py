"""Sitemap 采集测试专用夹具：两个回环 origin 的确定性 HTTP 服务。

主 origin（动态端口 A）承载 robots、urlset、sitemapindex、循环子表、损坏
XML 与一组仅由 Sitemap 声明的页面；第二 origin（动态端口 B）验证跨 origin
声明在连接前即被拒绝。全部服务只绑定 127.0.0.1，测试可断言零外网连接。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _urlset_xml(base: str, include_disallowed: bool = True) -> bytes:
    locs = [
        "/sm/one",
        "sm/two",
        f"{base}/sm/three",
        "/sm/one",  # 重复 loc：visited 去重，只请求一次
    ]
    if include_disallowed:
        locs.append("/private/blocked")
    parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"]
    parts.extend(f"<url><loc>{loc}</loc></url>" for loc in locs)
    parts.append("</urlset>")
    return "\n".join(parts).encode("utf-8")


def _index_xml() -> bytes:
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
        "<sitemap><loc>/sitemaps/child.xml</loc></sitemap>"
        "<sitemap><loc>/sitemaps/loop.xml</loc></sitemap>"
        "</sitemapindex>").encode("utf-8")


def _child_xml() -> bytes:
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
        "<url><loc>/sm/four</loc></url>"
        "</urlset>").encode("utf-8")


def _loop_xml() -> bytes:
    # 指回 index，制造 URL 循环。
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
        "<sitemap><loc>/sitemaps/index.xml</loc></sitemap>"
        "</sitemapindex>").encode("utf-8")


def _broken_xml() -> bytes:
    return b"<?xml version=\"1.0\"?><urlset><url><loc>/sm/never</loc></url>"


def _page(title: str, extra_nav: str = "") -> bytes:
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body><h1>{title}</h1>"
        "<nav><a href=\"/nav/a\">导航 A</a>"
        "<a href=\"/nav/b\">导航 B</a>"
        "<a href=\"/nav/c\">导航 C</a></nav>"
        f"{extra_nav}</body></html>").encode("utf-8")


def _nav_page(name: str) -> bytes:
    return _page(f"导航页 {name}")


def _sm_page(name: str) -> bytes:
    return _page(f"地图页 {name}")


def _robots_text(port_a: int, port_b: int) -> str:
    return (
        "User-agent: *\n"
        "Disallow: /private/\n"
        f"Sitemap: http://127.0.0.1:{port_a}/sitemaps/root.xml\n"
        f"Sitemap: http://127.0.0.1:{port_a}/sitemaps/index.xml\n"
        f"Sitemap: http://127.0.0.1:{port_b}/foreign.xml\n"
    )


@dataclass
class SitemapSite:
    """运行中的双 origin 服务及其观测状态。"""
    base_a: str
    base_b: str
    requests_a: list[str]
    requests_b: list[str]
    robots_url: str


class _RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _record(self) -> None:
        self.server.requests.append(self.path)  # type: ignore[attr-defined]

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - http.server 接口名
        self._record()
        path = self.path.split("?", 1)[0]
        port_a = self.server.port_a  # type: ignore[attr-defined]
        port_b = self.server.port_b  # type: ignore[attr-defined]
        if path == "/robots.txt":
            self._send(200, "text/plain; charset=utf-8",
                _robots_text(port_a, port_b).encode("utf-8"))
            return
        routes = self.server.routes  # type: ignore[attr-defined]
        if path in routes:
            content_type, body = routes[path]
            self._send(200, content_type, body)
            return
        if path.startswith("/sm/") or "/sm/" in path:
            self._send(200, "text/html; charset=utf-8", _sm_page(path.rsplit("/", 1)[-1]))
            return
        if path.startswith("/nav/"):
            self._send(200, "text/html; charset=utf-8", _nav_page(path.rsplit("/", 1)[-1]))
            return
        if path == "/private/blocked":
            self._send(200, "text/html; charset=utf-8", _page("禁止页"))
            return
        self._send(404, "text/html; charset=utf-8", _page("404"))

    def log_message(self, format: str, *args: object) -> None:
        pass


def _start(base_a: str, base_b: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.port_a = 0  # type: ignore[attr-defined]
    server.port_b = 0  # type: ignore[attr-defined]
    server.routes = {  # type: ignore[attr-defined]
        "/": ("text/html; charset=utf-8", _page("首页")),
        "/sitemaps/root.xml": ("application/xml; charset=utf-8", _urlset_xml(base_a)),
        "/sitemaps/index.xml": ("application/xml; charset=utf-8", _index_xml()),
        "/sitemaps/child.xml": ("application/xml; charset=utf-8", _child_xml()),
        "/sitemaps/loop.xml": ("application/xml; charset=utf-8", _loop_xml()),
        "/sitemaps/broken.xml": ("application/xml; charset=utf-8", _broken_xml()),
        "/sitemaps/not-xml.xml": ("text/html; charset=utf-8",
            _page("伪装成地图的 HTML")),
        "/foreign.xml": ("application/xml; charset=utf-8", _urlset_xml(base_b)),
    }
    return server


@pytest.fixture()
def sitemap_site() -> SitemapSite:
    """启动两个回环 origin；第二 origin 若被请求则测试可据此断言连接泄漏。"""
    server_a = _start("", "")
    port_a = server_a.server_address[1]
    server_b = _start("", "")
    port_b = server_b.server_address[1]
    base_a = f"http://127.0.0.1:{port_a}"
    base_b = f"http://127.0.0.1:{port_b}"
    for server in (server_a, server_b):
        server.port_a = port_a  # type: ignore[attr-defined]
        server.port_b = port_b  # type: ignore[attr-defined]
    server_a.routes["/sitemaps/root.xml"] = (  # type: ignore[attr-defined]
        "application/xml; charset=utf-8", _urlset_xml(base_a))
    server_b.routes["/foreign.xml"] = (  # type: ignore[attr-defined]
        "application/xml; charset=utf-8", _urlset_xml(base_b))
    threads = [
        threading.Thread(target=server_a.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True),
        threading.Thread(target=server_b.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        yield SitemapSite(base_a=base_a, base_b=base_b,
            requests_a=server_a.requests,  # type: ignore[attr-defined]
            requests_b=server_b.requests,  # type: ignore[attr-defined]
            robots_url=f"{base_a}/robots.txt")
    finally:
        server_a.shutdown()
        server_b.shutdown()
        server_a.server_close()
        server_b.server_close()
        for thread in threads:
            thread.join(timeout=2)
