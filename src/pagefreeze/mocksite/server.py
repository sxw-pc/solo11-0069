"""回环 mock HTTP 服务；所有时间相关响应头都由注入时钟生成。"""
from __future__ import annotations

import logging
import socket
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ..errors import PolicyError
from ..policy import validate_bind_host
from ..util import http_date
from .content import respond

_LOGGER = logging.getLogger(__name__)


class _MockHTTPServer(ThreadingHTTPServer):
    """带固定时钟的 ThreadingHTTPServer，测试与 demo 共用。"""
    daemon_threads = True
    allow_reuse_address = True
    mock_now: datetime


class _MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pagefreeze-mock/0.1"
    sys_version = ""

    def do_GET(self) -> None:
        """处理 GET；慢路由在发送前按固定时长延迟。"""
        response = respond(self.path)
        if response.delay_seconds:
            time.sleep(response.delay_seconds)
        try:
            self.send_response_only(response.status, response.reason)
            self.send_header("Date", http_date(self.server.mock_now))  # type: ignore[attr-defined]
            self.send_header("Server", self.server_version)
            for name, value in response.headers:
                self.send_header(name, value)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)
                self.wfile.flush()
        except (ConnectionError, TimeoutError):
            _LOGGER.debug("mock client disconnected early: %s", self.path)
        finally:
            self.close_connection = True

    def do_HEAD(self) -> None:
        """HEAD 只返回头，便于手动检查服务是否在线。"""
        response = respond(self.path)
        try:
            self.send_response_only(response.status, response.reason)
            self.send_header("Date", http_date(self.server.mock_now))  # type: ignore[attr-defined]
            self.send_header("Server", self.server_version)
            for name, value in response.headers:
                self.send_header(name, value)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
        except (ConnectionError, TimeoutError):
            _LOGGER.debug("mock HEAD client disconnected: %s", self.path)
        finally:
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        """把访问日志降为 debug，避免污染 demo 的固定格式 stdout。"""
        _LOGGER.debug("mock %s - %s", self.address_string(), format % args)


def _server_class(host: str) -> type[_MockHTTPServer]:
    if ":" not in host:
        return _MockHTTPServer
    class _MockHTTPServerV6(_MockHTTPServer):
        address_family = socket.AF_INET6
    return _MockHTTPServerV6


def create_mock_server(host: str, port: int, now: datetime) -> ThreadingHTTPServer:
    """创建并绑定 mock 服务；绑定地址必须先通过回环校验。"""
    safe_host = validate_bind_host(host)
    if not 0 <= port <= 65535:
        raise PolicyError("PORT_RANGE", "port must be between 0 and 65535")
    server_cls = _server_class(safe_host)
    try:
        server = server_cls((safe_host, port), _MockHandler)
    except OSError as exc:
        raise PolicyError("SERVE_BIND_FAILED", f"cannot bind {safe_host}:{port}") from exc
    server.mock_now = now  # type: ignore[attr-defined]
    return server
