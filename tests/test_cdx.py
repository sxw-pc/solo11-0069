"""CDX 排序去重、查询、时间窗与真实偏移定位测试。"""
from __future__ import annotations

import zlib
from pathlib import Path
import pytest
from pagefreeze.cdx import CdxRow, lookup, read_index, rows_from_archive, window, write_index
from pagefreeze.errors import CdxError
from pagefreeze.pipeline import PipelineResult
from pagefreeze.warc import parse_records


def _row(url: str, stamp: str, offset: int = 0) -> CdxRow:
    from pagefreeze.policy import urlkey
    return CdxRow(urlkey=urlkey(url), timestamp=stamp, url=url, status=200,
        mime="text/html", digest="sha256:" + "0" * 64,
        offset=offset, length=100, filename="seg-00000.warc.gz",
    )


def test_sort_dedupe_and_rewrite(tmp_path: Path) -> None:
    later = _row("http://127.0.0.1:18080/b", "20240102000000")
    earlier = _row("http://127.0.0.1:18080/b", "20240101000000")
    duplicate = _row("http://127.0.0.1:18080/b", "20240101000000", offset=99)
    path = tmp_path / "index.cdx"
    written = write_index(path, [later, earlier, duplicate])
    assert [row.timestamp for row in written] == ["20240101000000", "20240102000000"]
    assert written[0].offset == 0
    assert read_index(path) == written


def test_lookup_and_window() -> None:
    rows = [_row("http://127.0.0.1:18080/a", "20240101000000"),
        _row("http://127.0.0.1:18080/a", "20240103000000"),
        _row("http://127.0.0.1:18080/a", "20240105000000"),
    ]
    selected = lookup(rows, "HTTP://127.0.0.1:18080/a#x", "20240104000000")
    assert selected is not None
    assert selected.timestamp == "20240103000000"
    assert lookup(rows, "http://127.0.0.1:18080/a", "20231231000000") is None
    assert len(window(rows, rows[0].urlkey, "20240102000000", "20240105000000")) == 2


def test_malformed_index_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.cdx"
    path.write_text("not enough fields\n", encoding="utf-8")
    with pytest.raises(CdxError) as caught:
        read_index(path)
    assert caught.value.code == "CDX_MALFORMED"


def test_archive_offsets_jump_to_exact_record(demo_result: PipelineResult, tmp_path: Path
) -> None:
    rows = rows_from_archive(demo_result.paths.archive_dir)
    assert len(rows) == 16
    row = next(item for item in rows if item.url.endswith("/zh/"))
    compressed = (demo_result.paths.archive_dir / row.filename).read_bytes()
    raw = zlib.decompress(compressed, 16 + zlib.MAX_WBITS)
    record_bytes = raw[row.offset:row.offset + row.length]
    records = parse_records(record_bytes, row.filename)
    assert len(records) == 1
    assert records[0].header("WARC-Target-URI") == row.url
    assert records[0].header("WARC-Payload-Digest") == row.digest
