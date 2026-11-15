"""从归档与索引离线回放单条响应，只监听回环地址。"""
from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from .archive_io import load_archive
from .cdx import CdxRow, lookup, read_index
from .errors import CdxError, PagefreezeError, PolicyError, ReplayError
from .httpmsg import parse_response
from .policy import normalize_url, validate_bind_host
from .digest import sha256_file
from .util import TIMESTAMP_RE, parse_clock

_LOGGER = logging.getLogger(__name__)
_HOP_BY_HOP = {"content-length", "transfer-encoding", "connection", "keep-alive"}


@dataclass(frozen=True)
class ReplaySnapshot:
    """一次回放命中的完整信息。"""
    row: CdxRow
    record_id: str
    status: int
    reason: str
    version: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


class ReplayArchive:
    """预加载归档和索引；索引损坏时保留错误供 HTTP 层返回 500。"""

    def __init__(self, archive_dir: Path, index_path: Path) -> None:
        self.archive_dir = archive_dir
        self.index_path = index_path
        self.rows: list[CdxRow] = []
        self.records: list[Any] = []
        self._by_location: dict[tuple[str, int], Any] = {}
        self._by_id: dict[str, Any] = {}
        self.load_error: ReplayError | None = None
        self._load()

    def _load(self) -> None:
        try:
            loaded = load_archive(self.archive_dir)
            if loaded.problems:
                raise ReplayError("REPLAY_ARCHIVE_ERROR", "archive contains corrupt segments")
            self.records = loaded.records
            self._by_location = {(record.filename, record.offset): record for record in loaded.records}
            self._by_id = {record.record_id: record for record in loaded.records if record.record_id}
            self.rows = read_index(self.index_path)
        except CdxError as exc:
            self.load_error = ReplayError("REPLAY_INDEX_ERROR", f"cannot read index: {exc.code}")
        except ReplayError as exc:
            self.load_error = exc
        except PagefreezeError as exc:
            self.load_error = ReplayError("REPLAY_ARCHIVE_ERROR", f"cannot load archive: {exc.code}")

    @property
    def snapshot_count(self) -> int:
        return len(self.rows)

    def resolve(self, url: str, ts14: str) -> ReplaySnapshot:
        """按 URL + 时间戳选择快照，并跟随 revisit 找到首见正文。"""
        if self.load_error is not None:
            raise self.load_error
        try:
            requested = normalize_url(url)
        except PolicyError as exc:
            raise ReplayError("REPLAY_BAD_PARAM", "url is not a valid http(s) URL", 400) from exc
        row = lookup(self.rows, requested, ts14)
        if row is None:
            raise ReplayError("REPLAY_NO_SNAPSHOT", "no snapshot at or before timestamp", 404)
        selected = self._by_location.get((row.filename, row.offset))
        if selected is None:
            raise ReplayError("REPLAY_ARCHIVE_ERROR", "index entry does not locate a record")
        final = selected
        for _ in range(8):
            if final.record_type != "revisit":
                break
            target_id = final.header("WARC-Refers-To")
            target = self._by_id.get(target_id or "")
            if target is None:
                raise ReplayError("REPLAY_ARCHIVE_ERROR", "revisit target is missing")
            final = target
        try:
            message = parse_response(selected.block)
            final_message = parse_response(final.block)
        except PagefreezeError as exc:
            raise ReplayError("REPLAY_ARCHIVE_ERROR", f"HTTP block is invalid: {exc.code}") from exc
        return ReplaySnapshot(row=row,
            record_id=selected.record_id,
            status=int(message.status or 0),
            reason=message.reason,
            version=message.version,
            headers=message.headers,
            body=final_message.body,
        )


class _ReplayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    replay_archive: ReplayArchive
    index_digest: str


class _ReplayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pagefreeze-replay/0.1"

    def do_GET(self) -> None:
        """``/`` 是本地索引页，``/replay`` 是单条响应回放。"""
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._serve_index()
        elif parsed.path == "/replay":
            self._serve_replay(parsed.query)
        else:
            self._json_error(404, "REPLAY_NOT_FOUND", "unknown route")

    def _serve_replay(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        urls = params.get("url", [])
        stamps = params.get("ts", [])
        if len(urls) != 1 or len(stamps) != 1 or not urls[0]:
            self._json_error(400, "REPLAY_BAD_PARAM", "url and ts are required once")
            return
        if not TIMESTAMP_RE.match(stamps[0]):
            self._json_error(400, "REPLAY_BAD_PARAM", "ts must be 14 digits")
            return
        try:
            parse_clock(stamps[0])
        except PagefreezeError:
            self._json_error(400, "REPLAY_BAD_PARAM", "ts is not a valid date")
            return
        archive = self.server.replay_archive  # type: ignore[attr-defined]
        try:
            snapshot = archive.resolve(urls[0], stamps[0])
        except ReplayError as exc:
            self._json_error(exc.http_status, exc.code, exc.message)
            return
        self._send_snapshot(snapshot)

    def _serve_index(self) -> None:
        archive = self.server.replay_archive  # type: ignore[attr-defined]
        if archive.load_error is not None:
            self._json_error(500, archive.load_error.code, archive.load_error.message)
            return
        rows = sorted(archive.rows, key=lambda row: (row.urlkey, row.timestamp))
        items = []
        for row in rows:
            query = urlencode({"url": row.url, "ts": row.timestamp})
            items.append("<tr>"
                f"<td><code>{escape(row.urlkey)}</code></td>"
                f"<td>{escape(row.timestamp)}</td>"
                f"<td>{row.status}</td><td>{row.length}</td>"
                f"<td><a href=\"/replay?{escape(query)}\">回放</a></td>"
                "</tr>")
        html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            "<title>封页台回放索引</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:64rem;margin:2rem auto;"
            "padding:0 1rem;background:#f7f8fa;color:#20242a}"
            "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd2da;"
            "padding:.45rem .6rem;text-align:left}code{word-break:break-all}"
            "</style></head><body><h1>封页台回放索引</h1>"
            f"<p>快照数量：{len(rows)}。本页不加载外部资源。</p>"
            "<table><thead><tr><th>urlkey</th><th>时间戳</th><th>状态</th>"
            "<th>记录长度</th><th>操作</th></tr></thead><tbody>"
            + "".join(items)
            + "</tbody></table></body></html>"
        ).encode("utf-8")
        self._send_bytes(200, "OK", [("Content-Type", "text/html; charset=utf-8")], html)
        self.close_connection = True

    def _send_snapshot(self, snapshot: ReplaySnapshot) -> None:
        headers = [(name, value) for name, value in snapshot.headers
            if name.lower() not in _HOP_BY_HOP]
        headers.extend([
                ("Content-Length", str(len(snapshot.body))),
                ("X-Archive-Ref", snapshot.record_id),
                ("X-Archive-Timestamp", snapshot.row.timestamp),
                ("X-Archive-Digest", snapshot.row.digest),
                ("X-Archive-File", snapshot.row.filename),
                ("X-Archive-Urlkey", snapshot.row.urlkey),
                ("Connection", "close"),
            ])
        try:
            self.send_response_only(snapshot.status, snapshot.reason or None)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if snapshot.body:
                self.wfile.write(snapshot.body)
                self.wfile.flush()
        except (ConnectionError, TimeoutError):
            _LOGGER.debug("replay client disconnected")
        finally:
            self.close_connection = True

    def _json_error(self, status: int, code: str, message: str) -> None:
        payload = json.dumps({"error": {"code": code, "message": message}},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8"),
                   ("Cache-Control", "no-store")]
        self._send_bytes(status, "OK" if status < 400 else "Error", headers, payload)
        self.close_connection = True

    def _send_bytes(
        self, status: int, reason: str,
                    headers: list[tuple[str, str]], body: bytes) -> None:
        try:
            headers = headers + [("Content-Length", str(len(body))), ("Connection", "close")]
            self.send_response_only(status, reason)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionError, TimeoutError):
            _LOGGER.debug("replay client disconnected")

    def log_message(self, format: str, *args: object) -> None:
        _LOGGER.debug("replay %s - %s", self.address_string(), format % args)


def _server_class(host: str) -> type[_ReplayServer]:
    if ":" not in host:
        return _ReplayServer
    class _ReplayServerV6(_ReplayServer):
        address_family = socket.AF_INET6
    return _ReplayServerV6


class ReplayServer:
    """可启动/停止的回放服务包装，便于 CLI 与测试共用。"""

    def __init__(
        self,
        archive_dir: Path,
        index_path: Path,
        host: str = "127.0.0.1",
        port: int = 18081,
    ) -> None:
        safe_host = validate_bind_host(host)
        if not 0 <= port <= 65535:
            raise PolicyError("PORT_RANGE", "port must be between 0 and 65535")
        self.archive = ReplayArchive(archive_dir, index_path)
        try:
            self.httpd = _server_class(safe_host)((safe_host, port), _ReplayHandler)
        except OSError as exc:
            raise PolicyError("SERVE_BIND_FAILED", f"cannot bind {safe_host}:{port}") from exc
        self.httpd.replay_archive = self.archive
        self.httpd.index_digest = sha256_file(index_path) if index_path.is_file() else ""

    @property
    def host(self) -> str:
        return str(self.httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def snapshot_count(self) -> int:
        return self.archive.snapshot_count

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self.httpd.shutdown()

    def close(self) -> None:
        self.httpd.server_close()
