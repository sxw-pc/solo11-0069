"""离线回放服务：命中断言、四类错误分支与索引损坏。"""
from __future__ import annotations

import http.client
import json
import shutil
import threading
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from pagefreeze.cdx import CdxRow, read_index, write_index
from pagefreeze.digest import digest_bytes
from pagefreeze.fetch import FetchResult
from pagefreeze.pipeline import PipelineResult
from pagefreeze.replay import ReplayArchive, ReplayServer
from pagefreeze.util import parse_clock
from pagefreeze.warc_build import make_revisit_record, make_response_record, make_warcinfo

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


# ---------------------------------------------------------------------------
# 索引行与 WARC 记录的逐行绑定校验（坏行局部失败，健康行照常回放）
# ---------------------------------------------------------------------------

_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567856781234")
_MOMENT = parse_clock(FIXED_TS)
_LATER = parse_clock("20240220000000")


def _hit(url: str, body: bytes, status: int = 200,
         mime: str = "text/html; charset=utf-8") -> FetchResult:
    reason = "OK" if status == 200 else "Not Found"
    return FetchResult(url=url, status=status, reason=reason, version="HTTP/1.1",
        headers=(("Content-Type", mime),), body=body, mime=mime.split(";", 1)[0])


def _write_raw_archive(archive: Path, records: list) -> tuple[list, list[CdxRow]]:
    """把手工构造的 WARC 记录写成单卷并登记真实 offset/length。"""
    from pagefreeze.cdx import row_from_record
    from pagefreeze.util import gzip_compress
    from pagefreeze.warc import serialize_record
    archive.mkdir(parents=True, exist_ok=True)
    filename = "seg-00000.warc.gz"
    raw_parts: list[bytes] = []
    offset = 0
    for index, record in enumerate(records):
        record.filename = filename
        record.offset = offset
        record.record_index = index
        raw = serialize_record(record.headers, record.block)
        record.length = len(raw)
        raw_parts.append(raw)
        offset += len(raw)
    (archive / filename).write_bytes(gzip_compress(b"".join(raw_parts)))
    rows = [row for record in records if (row := row_from_record(record)) is not None]
    return records, rows


def _response_archive(archive: Path, hits: list[FetchResult],
                      moments: list[datetime] | None = None) -> list[CdxRow]:
    """warcinfo + 每个 FetchResult 一条 response 记录的最小归档。"""
    moments = moments or [_MOMENT] * len(hits)
    records = [make_warcinfo(_MOMENT, "config-digest", _NAMESPACE, "sha256")]
    for hit, moment in zip(hits, moments):
        from pagefreeze.policy import urlkey
        records.append(make_response_record(
            hit, urlkey(hit.url), moment, _NAMESPACE, "sha256"))
    _, rows = _write_raw_archive(archive, records)
    write_index(archive / "index.cdx", rows)
    return rows


def _mutate_row(index: Path, predicate, **changes) -> CdxRow:
    """只改写满足条件的一行，其余行保持原样后重写索引。"""
    rows = read_index(index)
    target = next(row for row in rows if predicate(row))
    mutated = replace(target, **changes)
    write_index(index, [mutated if row == target else row for row in rows])
    return mutated


def _assert_blocked(status: int, headers: dict[str, str], body: bytes,
                    forbidden: bytes) -> None:
    """坏请求必须是稳定错误，且不携带任何被伪装记录的实体字节。"""
    assert status == 500
    assert json.loads(body)["error"]["code"] == "REPLAY_INDEX_RECORD_MISMATCH"
    assert forbidden not in body
    assert "x-archive-digest" not in headers
    assert "x-archive-ref" not in headers
    assert headers["cache-control"] == "no-store"


def test_clean_synthetic_archive_replays(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    body_a = b"<html>page alpha</html>"
    rows = _response_archive(archive,
        [_hit("http://127.0.0.1:18080/a", body_a),
         _hit("http://127.0.0.1:18080/b", b"<html>page bravo</html>")])
    row = next(item for item in rows if item.url.endswith("/a"))
    with running_server(archive, archive / "index.cdx") as server:
        status, headers, body = _get(server.port, _replay_path(row.url, FIXED_TS))
    assert status == 200
    assert body == body_a
    assert headers["x-archive-digest"] == row.digest
    assert headers["x-archive-timestamp"] == row.timestamp
    assert headers["x-archive-file"] == row.filename


def test_row_repointed_at_another_record_is_blocked(tmp_path: Path) -> None:
    """H1：A 行改指 B 的 offset，仍保留 A 的 url/digest/filename。"""
    archive = tmp_path / "archive"
    body_a = b"<html>page alpha secret</html>"
    body_b = b"<html>page bravo public</html>"
    rows = _response_archive(archive,
        [_hit("http://127.0.0.1:18080/a", body_a),
         _hit("http://127.0.0.1:18080/b", body_b)])
    row_b = next(item for item in rows if item.url.endswith("/b"))
    _mutate_row(archive / "index.cdx",
        lambda row: row.url.endswith("/a"), offset=row_b.offset)
    with running_server(archive, archive / "index.cdx") as server:
        bad = _get(server.port, _replay_path("http://127.0.0.1:18080/a", FIXED_TS))
        good = _get(server.port, _replay_path("http://127.0.0.1:18080/b", FIXED_TS))
    _assert_blocked(*bad, forbidden=body_b)
    assert body_a not in bad[2]
    status_g, headers_g, body_g = good
    assert status_g == 200
    assert body_g == body_b
    row_b_loaded = next(item for item in read_index(archive / "index.cdx")
        if item.url.endswith("/b"))
    assert headers_g["x-archive-digest"] == row_b_loaded.digest


def test_same_url_other_timestamp_offset_is_blocked(tmp_path: Path) -> None:
    """同 URL 两个时间快照：行指向另一时刻记录时，Target-URI 相同也必须拒绝。"""
    archive = tmp_path / "archive"
    early_body = b"<html>version one</html>"
    later_body = b"<html>version two</html>"
    url = "http://127.0.0.1:18080/versioned"
    rows = _response_archive(archive,
        [_hit(url, early_body), _hit(url, later_body)],
        moments=[_MOMENT, _LATER])
    later_row = next(row for row in rows if row.timestamp == "20240220000000")
    _mutate_row(archive / "index.cdx",
        lambda row: row.timestamp == FIXED_TS, offset=later_row.offset)
    with running_server(archive, archive / "index.cdx") as server:
        bad = _get(server.port, _replay_path(url, FIXED_TS))
        good = _get(server.port, _replay_path(url, "20240220000000"))
    _assert_blocked(*bad, forbidden=later_body)
    assert early_body not in bad[2]
    assert good[0] == 200
    assert good[2] == later_body
    assert good[1]["x-archive-timestamp"] == "20240220000000"


def test_each_provenance_field_mismatch_is_blocked(tmp_path: Path) -> None:
    """digest/status/mime/length/timestamp 任一不一致都拒绝，且不影响兄弟行。"""
    archive = tmp_path / "archive"
    body_a = b"<html>field mismatch page</html>"
    body_b = b"<html>healthy sibling page</html>"
    url_a = "http://127.0.0.1:18080/a"
    url_b = "http://127.0.0.1:18080/b"
    binding_cases = [
        {"digest": "sha256:" + "f" * 64},
        {"status": 404},
        {"mime": "text/plain"},
        {"length": 999},
        {"timestamp": "20240101000000"},
    ]
    for changes in binding_cases:
        _response_archive(archive,
            [_hit(url_a, body_a), _hit(url_b, body_b)])
        _mutate_row(archive / "index.cdx", lambda row: row.url == url_a, **changes)
        with running_server(archive, archive / "index.cdx") as server:
            bad = _get(server.port, _replay_path(url_a, FIXED_TS))
            good = _get(server.port, _replay_path(url_b, FIXED_TS))
        _assert_blocked(*bad, forbidden=body_a)
        assert good[0] == 200
        assert good[2] == body_b
        assert good[1]["x-archive-digest"] == next(
            item for item in read_index(archive / "index.cdx") if item.url == url_b
        ).digest


def test_url_and_urlkey_rewrite_never_serves_foreign_body(tmp_path: Path) -> None:
    """行的选择键（url/urlkey）被改写时，要么查无快照要么绑定失败，均无实体字节。"""
    archive = tmp_path / "archive"
    body_a = b"<html>key mismatch page</html>"
    body_b = b"<html>healthy sibling page</html>"
    url_a = "http://127.0.0.1:18080/a"
    url_b = "http://127.0.0.1:18080/b"
    key_cases = [
        {"url": "http://127.0.0.1:18080/impostor"},
        {"urlkey": "http://impostor,127.0.0.1)/a"},
    ]
    for changes in key_cases:
        _response_archive(archive,
            [_hit(url_a, body_a), _hit(url_b, body_b)])
        _mutate_row(archive / "index.cdx", lambda row: row.url == url_a, **changes)
        with running_server(archive, archive / "index.cdx") as server:
            bad = _get(server.port, _replay_path(url_a, FIXED_TS))
            good = _get(server.port, _replay_path(url_b, FIXED_TS))
        assert bad[0] in (404, 500)
        payload = json.loads(bad[2])["error"]["code"]
        assert payload in ("REPLAY_NO_SNAPSHOT", "REPLAY_INDEX_RECORD_MISMATCH")
        assert body_a not in bad[2] and body_b not in bad[2]
        assert "x-archive-digest" not in bad[1]
        assert good[0] == 200
        assert good[2] == body_b


def test_revisit_missing_target_is_blocked_but_siblings_replay(tmp_path: Path) -> None:
    """revisit 的 WARC-Refers-To 指向不存在记录：拒绝该 URL，健康 URL 仍 200。"""
    from pagefreeze.httpmsg import build_response
    from pagefreeze.policy import urlkey
    archive = tmp_path / "archive"
    body = b"<html>revisited payload</html>"
    healthy = b"<html>independent healthy page</html>"
    payload_digest = digest_bytes(body)
    revisit_url = "http://127.0.0.1:18080/revisit"
    healthy_url = "http://127.0.0.1:18080/healthy"
    revisit_block = build_response(200, "OK", "HTTP/1.1",
        (("Content-Type", "text/html; charset=utf-8"),), b"")
    revisit = make_revisit_record(_hit(revisit_url, b""), urlkey(revisit_url),
        _MOMENT, "<urn:uuid:does-not-exist>", payload_digest, _NAMESPACE, "sha256")
    healthy_record = make_response_record(_hit(healthy_url, healthy),
        urlkey(healthy_url), _MOMENT, _NAMESPACE, "sha256")
    records = [make_warcinfo(_MOMENT, "config-digest", _NAMESPACE, "sha256"),
        revisit, healthy_record]
    _, rows = _write_raw_archive(archive, records)
    write_index(archive / "index.cdx", rows)
    with running_server(archive, archive / "index.cdx") as server:
        bad = _get(server.port, _replay_path(revisit_url, FIXED_TS))
        good = _get(server.port, _replay_path(healthy_url, FIXED_TS))
    _assert_blocked(*bad, forbidden=body)
    assert good[0] == 200
    assert good[2] == healthy


def test_revisit_digest_mismatch_is_blocked(tmp_path: Path) -> None:
    """revisit 行与记录绑定一致，但其 payload digest 与正文目标不一致时拒绝。"""
    from pagefreeze.httpmsg import build_response
    from pagefreeze.policy import urlkey
    archive = tmp_path / "archive"
    target_body = b"<html>original payload</html>"
    target_url = "http://127.0.0.1:18080/origin"
    revisit_url = "http://127.0.0.1:18080/revisit"
    target = make_response_record(_hit(target_url, target_body),
        urlkey(target_url), _MOMENT, _NAMESPACE, "sha256")
    target_urn = target.record_id
    lying_digest = "sha256:" + "9" * 64
    revisit_block = build_response(200, "OK", "HTTP/1.1",
        (("Content-Type", "text/html; charset=utf-8"),), b"")
    revisit = make_revisit_record(_hit(revisit_url, b""), urlkey(revisit_url),
        _MOMENT, target_urn, lying_digest, _NAMESPACE, "sha256")
    records = [make_warcinfo(_MOMENT, "config-digest", _NAMESPACE, "sha256"),
        target, revisit]
    _, rows = _write_raw_archive(archive, records)
    write_index(archive / "index.cdx", rows)
    with running_server(archive, archive / "index.cdx") as server:
        bad = _get(server.port, _replay_path(revisit_url, FIXED_TS))
        good = _get(server.port, _replay_path(target_url, FIXED_TS))
    _assert_blocked(*bad, forbidden=target_body)
    assert good[0] == 200
    assert good[2] == target_body


def test_binding_failure_is_per_request_and_does_not_poison_archive(
    demo_result: PipelineResult, tmp_path: Path,
) -> None:
    """同一损坏索引：坏 A 稳定 500，健康 B 与其它 URL 持续可回放。"""
    archive = tmp_path / "archive"
    shutil.copytree(demo_result.paths.archive_dir, archive)
    index = archive / "index.cdx"
    rows = read_index(index)
    row_a = next(item for item in rows if item.url == BASE + "/zh/")
    row_b = next(item for item in rows if item.url == BASE + "/robots.txt")
    _mutate_row(index, lambda row: row == row_a, offset=row_b.offset)
    with running_server(archive, index) as server:
        first_bad = _get(server.port, _replay_path(row_a.url, FIXED_TS))
        good = _get(server.port, _replay_path(row_b.url, FIXED_TS))
        second_bad = _get(server.port, _replay_path(row_a.url, FIXED_TS))
        index_page = _get(server.port, "/")
    for bad in (first_bad, second_bad):
        assert bad[0] == 500
        assert json.loads(bad[2])["error"]["code"] == "REPLAY_INDEX_RECORD_MISMATCH"
        assert "x-archive-digest" not in bad[1]
        assert "x-archive-ref" not in bad[1]
    assert good[0] == 200
    assert good[1]["x-archive-digest"] == row_b.digest
    assert index_page[0] == 200


def test_resolve_raises_before_any_snapshot_bytes(tmp_path: Path) -> None:
    """直接验证 resolve 在绑定失败时抛错，不构造 ReplaySnapshot。"""
    archive = tmp_path / "archive"
    body_a = b"<html>never send me</html>"
    body_b = b"<html>sibling</html>"
    rows = _response_archive(archive,
        [_hit("http://127.0.0.1:18080/a", body_a),
         _hit("http://127.0.0.1:18080/b", body_b)])
    row_b = next(item for item in rows if item.url.endswith("/b"))
    _mutate_row(archive / "index.cdx",
        lambda row: row.url.endswith("/a"), offset=row_b.offset)
    loaded = ReplayArchive(archive, archive / "index.cdx")
    assert loaded.load_error is None
    try:
        loaded.resolve("http://127.0.0.1:18080/a", FIXED_TS)
    except Exception as exc:  # noqa: BLE001 - 断言稳定错误码即可
        assert getattr(exc, "code", "") == "REPLAY_INDEX_RECORD_MISMATCH"
        assert getattr(exc, "http_status", 0) == 500
    else:  # pragma: no cover - 必须抛出，失败时显式报错
        raise AssertionError("resolve must reject an unbound index row")
    snapshot = loaded.resolve("http://127.0.0.1:18080/b", FIXED_TS)
    assert snapshot.body == body_b
