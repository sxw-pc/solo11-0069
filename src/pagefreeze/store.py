"""归档写入：确定性记录排序、revisit 去重、gzip 分卷与偏移登记。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from .config import ArchiveConfig
from .errors import PagefreezeError
from .fetch import FetchResult
from .policy import urlkey
from .digest import digest_bytes
from .util import gzip_compress
from .warc import WarcRecord, serialize_record
from .warc_build import make_request_record, make_response_record, make_revisit_record, make_warcinfo


@dataclass(frozen=True)
class SegmentInfo:
    """写入后的分卷统计；raw_size 是解压后 WARC 字节数。"""
    filename: str
    record_count: int
    raw_size: int
    gzip_size: int


@dataclass
class WriteResult:
    """归档写入结果，供索引、台账与报告复用。"""
    records: list[WarcRecord] = field(default_factory=list)
    segments: list[SegmentInfo] = field(default_factory=list)
    responses: int = 0
    revisits: int = 0
    requests: int = 0
    warcinfo: int = 0

    @property
    def raw_bytes(self) -> int:
        return sum(segment.raw_size for segment in self.segments)

    @property
    def gzip_bytes(self) -> int:
        return sum(segment.gzip_size for segment in self.segments)


def _sorted_hits(hits: list[FetchResult]) -> list[FetchResult]:
    unique = {hit.url: hit for hit in hits}
    return sorted(unique.values(), key=lambda hit: (urlkey(hit.url), hit.url))


def build_records(
    hits: list[FetchResult],
    now: datetime,
    config: ArchiveConfig,
    config_digest: str,
    user_agent: str,
) -> tuple[list[WarcRecord], int, int]:
    """按 (urlkey, url) 排序构造全部记录，并返回 response/revisit 数量。"""
    algorithm = config.digest_algorithm
    namespace = config.uuid_namespace
    records: list[WarcRecord] = [make_warcinfo(now, config_digest, namespace, algorithm)]
    seen_payloads: dict[str, str] = {}
    responses = 0
    revisits = 0
    for hit in _sorted_hits(hits):
        key = urlkey(hit.url)
        records.append(make_request_record(
                hit.url, key, now, user_agent, namespace, algorithm,
            ))
        payload_digest = digest_bytes(hit.body, algorithm)
        first_record = seen_payloads.get(payload_digest)
        if first_record is None:
            response = make_response_record(hit, key, now, namespace, algorithm)
            records.append(response)
            seen_payloads[payload_digest] = response.record_id
            responses += 1
        else:
            records.append(make_revisit_record(
                    hit, key, now, first_record, payload_digest,
                    namespace, algorithm,
                ))
            revisits += 1
    return records, responses, revisits


def _write_segment(path: Path, raw: bytes, record_count: int) -> SegmentInfo:
    compressed = gzip_compress(raw)
    try:
        path.write_bytes(compressed)
    except OSError as exc:
        raise PagefreezeError("ARCHIVE_WRITE", f"cannot write {path.name}: {exc}") from exc
    return SegmentInfo(filename=path.name,
        record_count=record_count,
        raw_size=len(raw),
        gzip_size=len(compressed),
    )


def write_archive(
    archive_dir: Path,
    config: ArchiveConfig,
    hits: list[FetchResult],
    now: datetime,
    config_digest: str,
    user_agent: str,
) -> WriteResult:
    """写出一个或多个 ``seg-NNNNN.warc.gz``，并记录每条记录的真实偏移。"""
    records, responses, revisits = build_records(hits, now, config, config_digest, user_agent)
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for old in archive_dir.glob("seg-*.warc.gz"):
            old.unlink()
    except OSError as exc:
        raise PagefreezeError("ARCHIVE_WRITE", f"cannot prepare archive dir: {exc}") from exc
    result = WriteResult(responses=responses, revisits=revisits)
    current: list[tuple[WarcRecord, bytes]] = []
    current_size = 0
    segment_number = 0

    def flush() -> None:
        nonlocal current, current_size, segment_number
        if not current:
            return
        filename = f"seg-{segment_number:05d}.warc.gz"
        offset = 0
        raw_parts: list[bytes] = []
        for index, (record, raw_record) in enumerate(current):
            record.filename = filename
            record.offset = offset
            record.length = len(raw_record)
            record.record_index = index
            raw_parts.append(raw_record)
            offset += len(raw_record)
        raw = b"".join(raw_parts)
        info = _write_segment(archive_dir / filename, raw, len(current))
        result.segments.append(info)
        result.records.extend(record for record, _ in current)
        segment_number += 1
        current = []
        current_size = 0
    for record in records:
        raw_record = serialize_record(record.headers, record.block)
        if current and current_size + len(raw_record) > config.segment_size_bytes:
            flush()
        current.append((record, raw_record))
        current_size += len(raw_record)
    flush()
    result.requests = sum(1 for record in result.records if record.record_type == "request")
    result.warcinfo = sum(1 for record in result.records if record.record_type == "warcinfo")
    return result
