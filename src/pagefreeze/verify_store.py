"""核验用的临时 SQLite 索引：跨卷关系检查不再依赖全量内存记录。

每卷解析后只把轻量元数据（位置、record_id、类型、digest、revisit 目标和 CDX
对照所需标量）写入临时库，完整记录正文随卷释放。临时文件在任何退出路径下都会
被清理，包括初始化或写入失败。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

_SCHEMA = (
    """CREATE TABLE segments (
        seq INTEGER PRIMARY KEY,
        filename TEXT NOT NULL UNIQUE,
        gzip_size INTEGER NOT NULL,
        raw_size INTEGER NOT NULL,
        record_count INTEGER NOT NULL,
        gzip_complete INTEGER NOT NULL,
        readable INTEGER NOT NULL,
        parsed_offset INTEGER NOT NULL,
        fully_covered INTEGER NOT NULL
    )""",
    """CREATE TABLE records (
        seq INTEGER NOT NULL,
        record_index INTEGER NOT NULL,
        filename TEXT NOT NULL,
        offset INTEGER NOT NULL,
        length INTEGER NOT NULL,
        record_id TEXT NOT NULL,
        record_type TEXT NOT NULL,
        target_url TEXT NOT NULL,
        urlkey TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        block_digest TEXT NOT NULL,
        refers_to TEXT NOT NULL,
        profile TEXT NOT NULL,
        http_status INTEGER,
        mime TEXT NOT NULL,
        meta_error TEXT NOT NULL,
        meta_message TEXT NOT NULL,
        PRIMARY KEY (seq, record_index)
    )""",
    "CREATE UNIQUE INDEX idx_records_location ON records(filename, offset)",
    "CREATE INDEX idx_records_id ON records(record_id)",
    "CREATE INDEX idx_records_type ON records(record_type)",
)


class VerifyStore:
    """核验期间的临时关系索引；用上下文管理器保证临时文件清理。"""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.connection: sqlite3.Connection | None = None
        fd, name = tempfile.mkstemp(prefix="pagefreeze-verify-", suffix=".sqlite3")
        os.close(fd)
        self.path = Path(name)
        try:
            self.connection = sqlite3.connect(str(self.path))
            for statement in _SCHEMA:
                self.connection.execute(statement)
            self.connection.commit()
        except sqlite3.Error:
            self.close()
            raise

    def __enter__(self) -> "VerifyStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """关闭连接并删除临时文件；重复调用安全，关闭异常不阻止文件清理。"""
        connection, self.connection = self.connection, None
        path, self.path = self.path, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def add_segment(
        self,
        *,
        seq: int,
        filename: str,
        gzip_size: int,
        raw_size: int,
        record_count: int,
        gzip_complete: bool,
        readable: bool,
        parsed_offset: int,
        fully_covered: bool,
    ) -> None:
        assert self.connection is not None
        self.connection.execute(
            "INSERT INTO segments(seq, filename, gzip_size, raw_size, record_count,"
            " gzip_complete, readable, parsed_offset, fully_covered) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, filename, gzip_size, raw_size, record_count, int(gzip_complete),
                int(readable), parsed_offset, int(fully_covered)),
        )

    def add_record(self, values: tuple[object, ...]) -> None:
        assert self.connection is not None
        self.connection.execute(
            "INSERT INTO records(seq, record_index, filename, offset, length, record_id,"
            " record_type, target_url, urlkey, timestamp, payload_digest, block_digest,"
            " refers_to, profile, http_status, mime, meta_error, meta_message) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )

    def commit(self) -> None:
        assert self.connection is not None
        self.connection.commit()

    def segment_coverage(self, filename: str) -> sqlite3.Row | None:
        """返回某卷的覆盖边界；卷不在归档中时返回 None。"""
        assert self.connection is not None
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            "SELECT * FROM segments WHERE filename = ?", (filename,),
        ).fetchone()

    def segment_sizes(self) -> dict[str, int]:
        assert self.connection is not None
        rows = self.connection.execute(
            "SELECT filename, record_count FROM segments ORDER BY seq",
        ).fetchall()
        return {filename: count for filename, count in rows}

    def location_lookup(self, filename: str, offset: int) -> sqlite3.Row | None:
        """按 (卷, 偏移) 取记录轻量行；顺序按卷序与记录序号稳定。"""
        assert self.connection is not None
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            "SELECT * FROM records WHERE filename = ? AND offset = ?",
            (filename, offset),
        ).fetchone()

    def iter_indexable_records(self) -> Iterator[sqlite3.Row]:
        """按稳定顺序产出 response/revisit 记录，用于索引覆盖反向核对。"""
        assert self.connection is not None
        self.connection.row_factory = sqlite3.Row
        yield from self.connection.execute(
            "SELECT * FROM records WHERE record_type IN ('response', 'revisit') "
            "ORDER BY seq, record_index",
        )

    def iter_revisits(self) -> Iterator[sqlite3.Row]:
        """按稳定顺序产出全部 revisit 记录。"""
        assert self.connection is not None
        self.connection.row_factory = sqlite3.Row
        yield from self.connection.execute(
            "SELECT * FROM records WHERE record_type = 'revisit' "
            "ORDER BY seq, record_index",
        )

    def lookup_record_id(self, record_id: str) -> sqlite3.Row | None:
        assert self.connection is not None
        self.connection.row_factory = sqlite3.Row
        return self.connection.execute(
            "SELECT * FROM records WHERE record_id = ? ORDER BY seq, record_index LIMIT 1",
            (record_id,),
        ).fetchone()

    def count_indexable(self) -> int:
        assert self.connection is not None
        row = self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type IN ('response', 'revisit')",
        ).fetchone()
        return int(row[0])

    def count_records(self) -> int:
        assert self.connection is not None
        return int(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def has_incomplete_segment(self) -> bool:
        """是否存在未完整结束（不可读、gzip 未 EOF 或 WARC 未解析到尾）的卷。"""
        assert self.connection is not None
        row = self.connection.execute(
            "SELECT COUNT(*) FROM segments WHERE fully_covered = 0",
        ).fetchone()
        return int(row[0]) > 0

    def type_counts(self) -> dict[str, int]:
        assert self.connection is not None
        rows = self.connection.execute(
            "SELECT record_type, COUNT(*) FROM records GROUP BY record_type",
        ).fetchall()
        return {record_type: count for record_type, count in rows}
