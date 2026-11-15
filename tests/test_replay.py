"""离线回放服务：命中断言、四类错误分支与索引损坏。"""
from __future__ import annotations

import http.client
import json
import shutil
import threading
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from pagefreeze.cdx import read_index
from pagefreeze.pipeline import PipelineResult
from pagefreeze.replay import ReplayServer

FIXED_TS = "20240115000000"
BASE = "http://127.0.0.1:18080"


@contextmanager
def running_server(archive: Path, index: Path) -> Iterator[ReplayServer]:
    server = ReplayServer(archive, index, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers, body
    finally:
        connection.close()


def _replay_path(url: str, stamp: str) -> str:
    return "/replay?" + urllib.parse.urlencode({"url": url, "ts": stamp})


def test_hit_has_original_headers_and_provenance(demo_result: PipelineResult) -> None:
    rows = read_index(demo_result.paths.index_path)
    row = next(item for item in rows if item.url == BASE + "/zh/")
    with running_server(demo_result.paths.archive_dir, demo_result.paths.index_path) as server:
        status, headers, body = _get(server.port, _replay_path(row.url, FIXED_TS))
    assert status == 200
    assert "中文编码样例".encode("utf-8") in body
    assert headers["content-type"].startswith("text/html")
    assert headers["x-archive-ref"] == row.digest or headers["x-archive-ref"].startswith("<urn:uuid:")
    assert headers["x-archive-timestamp"] == row.timestamp
    assert headers["x-archive-digest"] == row.digest
    assert headers["x-archive-file"] == row.filename


def test_missing_early_and_invalid_requests(demo_result: PipelineResult) -> None:
    with running_server(demo_result.paths.archive_dir, demo_result.paths.index_path) as server:
        early = _get(server.port, _replay_path(BASE + "/zh/", "19990101000000"))
        unknown = _get(server.port, _replay_path(BASE + "/not-collected", FIXED_TS))
        invalid = _get(server.port, _replay_path(BASE + "/zh/", "bad"))
        index_page = _get(server.port, "/")
    assert early[0] == 404
    assert json.loads(early[2])["error"]["code"] == "REPLAY_NO_SNAPSHOT"
    assert unknown[0] == 404
    assert json.loads(unknown[2])["error"]["code"] == "REPLAY_NO_SNAPSHOT"
    assert invalid[0] == 400
    assert json.loads(invalid[2])["error"]["code"] == "REPLAY_BAD_PARAM"
    assert index_page[0] == 200
    assert b"<script" not in index_page[2]
    assert "快照数量：16".encode("utf-8") in index_page[2]


def test_revisit_body_is_followed(demo_result: PipelineResult) -> None:
    with running_server(demo_result.paths.archive_dir, demo_result.paths.index_path) as server:
        status_a, _, body_a = _get(server.port, _replay_path(BASE + "/dup/a", FIXED_TS))
        status_b, headers_b, body_b = _get(server.port, _replay_path(BASE + "/dup/b", FIXED_TS))
    assert status_a == status_b == 200
    assert body_a == body_b
    assert headers_b["x-archive-ref"].startswith("<urn:uuid:")


def test_corrupt_index_returns_500(demo_result: PipelineResult, tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    shutil.copytree(demo_result.paths.archive_dir, archive)
    (archive / "index.cdx").write_text("broken index\n", encoding="utf-8")
    with running_server(archive, archive / "index.cdx") as server:
        status, _, body = _get(server.port, _replay_path(BASE + "/zh/", FIXED_TS))
    assert status == 500
    assert json.loads(body)["error"]["code"] == "REPLAY_INDEX_ERROR"
