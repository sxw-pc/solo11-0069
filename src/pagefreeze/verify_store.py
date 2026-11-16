"""核验用临时 SQLite：跨卷只保留轻量记录元数据，正文随卷释放。

核验器逐卷读取 WARC，把跨卷关系检查（CDX 定位、revisit 回指）所需的最小字段
写入本库；记录正文不入库，因此大正文的峰值内存只取决于当前处理的一卷。
库文件位于临时目录，:meth:`close` 保证在任何路径下都被清理。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable
from .errors import PagefreezeError

_RECORD_COLUMNS = (
    "file", "record_index", "offset", "length", "record_id", "record_type",
    "payload_digest", "block_digest", "refers_to", "profile", "target_uri",
    "warc_date", "http_valid", "http_status", "http_mime", "normalized_url",
    "urlkey", "ts14", "error_code", "error_message",
)
# 仅这些列允许 NULL：HTTP/日期解析失败时由 error_* 取代，其余列恒有值。
_NULLABLE_COLUMNS = frozenset((
    "http_status", "http_mime", "normalized_url", "urlkey", "ts14",
    "error_code", "error_message",
))
_INTEGER_COLUMNS = frozenset(("record_index", "offset", "length", "http_valid", "http_status"))


class VerifyStoreError(PagefreezeError):
    """临时核验库无法创建或写入。"""

    def __init__(self, message: str) -> None:
        super().__init__("VERIFY_STORE_ERROR", message)


def _record_ddl() -> str:
    columns = ", ".join(
        f"{name} {'INTEGER' if name in _INTEGER_COLUMNS else 'TEXT'}"
        f"{'' if name in _NULLABLE_COLUMNS else ' NOT NULL'}"
        for name in _RECORD_COLUMNS
    )
    return (
        "CREATE TABLE records (seq INTEGER PRIMARY KEY, "
        "segment_seq INTEGER NOT NULL, " + columns + ")"
    )


_SCHEMA = (
    """CREATE TABLE segments (
        seq INTEGER PRIMARY KEY,
        file TEXT NOT NULL UNIQUE,
        gzip_size INTEGER NOT NULL,
        raw_size INTEGER NOT NULL,
        gzip_eof INTEGER NOT NULL,
        parse_offset INTEGER NOT NULL,
        complete INTEGER NOT NULL,
        record_count INTEGER NOT NULL
    )""",
    _record_ddl(),
    "CREATE INDEX idx_records_location ON records(file, offset)",
    "CREATE INDEX idx_records_id ON records(record_id)",
    "CREATE INDEX idx_records_type ON records(record_type)",
)


class VerificationStore:
    """临时库句柄；用作上下文管理器，异常路径也会删除临时目录。"""

    def __init__(self) -> None:
        self.connection: sqlite3.Connection | None = None
        self.tmpdir: Path | None = None
        try:
            self.tmpdir = Path(tempfile.mkdtemp(prefix="pagefreeze-verify-"))
            self.path = self.tmpdir / "verify.sqlite3"
            self.connection = sqlite3.connect(self.path)
            self.connection.execute("PRAGMA journal_mode=MEMORY")
            self.connection.execute("PRAGMA synchronous=OFF")
            for statement in _SCHEMA:
                self.connection.execute(statement)
            self.connection.commit()
        except (OSError, sqlite3.Error) as exc:
            self._cleanup()
            raise VerifyStoreError(f"cannot create verification store: {exc}") from exc

    def _cleanup(self) -> None:
        connection, tmpdir = self.connection, self.tmpdir
        self.connection, self.tmpdir = None, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def close(self) -> None:
        """关闭连接并删除临时目录；重复调用安全。"""
        self._cleanup()

    def __enter__(self) -> "VerificationStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _execute(self, statement: str, params: Iterable[Any] = ()) -> None:
        try:
            assert self.connection is not None
            self.connection.execute(statement, tuple(params))
        except sqlite3.Error as exc:
            raise VerifyStoreError(f"verification store write failed: {exc}") from exc

    def add_segment(self, segment: Any) -> int:
        """登记一卷的 gzip/解析覆盖边界，返回卷序号。"""
        assert self.connection is not None
        seq = self.connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        self._execute(
            "INSERT INTO segments(seq,file,gzip_size,raw_size,gzip_eof,parse_offset,"
            "complete,record_count) VALUES (?,?,?,?,?,?,?,?)",
            (seq, segment.file, segment.gzip_size, segment.raw_size,
                int(segment.gzip_eof), segment.parse_offset, int(segment.complete),
                len(segment.records)),
        )
        return seq

    def add_record(self, segment_seq: int, row: dict[str, Any]) -> None:
        """写入一条记录的轻量元数据（不含正文）。"""
        placeholders = ", ".join("?" for _ in _RECORD_COLUMNS)
        self._execute(
            f"INSERT INTO records(segment_seq,{', '.join(_RECORD_COLUMNS)}) "
            f"VALUES (?,{placeholders})",
            (segment_seq, *(row[name] for name in _RECORD_COLUMNS)),
        )

    def commit(self) -> None:
        try:
            assert self.connection is not None
            self.connection.commit()
        except sqlite3.Error as exc:
            raise VerifyStoreError(f"verification store commit failed: {exc}") from exc

    def _fetch_record(self, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        assert self.connection is not None
        columns = ", ".join(_RECORD_COLUMNS)
        cursor = self.connection.execute(
            f"SELECT {columns} FROM records WHERE {where} "
            "ORDER BY segment_seq,record_index LIMIT 1",
            params,
        )
        values = cursor.fetchone()
        if values is None:
            return None
        return dict(zip(_RECORD_COLUMNS, values))

    def location(self, file: str, offset: int) -> dict[str, Any] | None:
        """按 (文件名, 解压偏移) 取记录元数据。"""
        return self._fetch_record("file=? AND offset=?", (file, offset))

    def by_id(self, record_id: str) -> dict[str, Any] | None:
        """按 WARC-Record-ID 取记录元数据。"""
        return self._fetch_record("record_id=?", (record_id,))

    def segment(self, file: str) -> dict[str, Any] | None:
        assert self.connection is not None
        cursor = self.connection.execute(
            "SELECT seq,file,gzip_size,raw_size,gzip_eof,parse_offset,complete,record_count "
            "FROM segments WHERE file=?",
            (file,),
        )
        values = cursor.fetchone()
        if values is None:
            return None
        return {description[0]: value for description, value in zip(cursor.description, values)}

    def segments(self) -> list[dict[str, Any]]:
        """按卷序号返回全部覆盖信息，顺序即文件名排序。"""
        assert self.connection is not None
        cursor = self.connection.execute(
            "SELECT seq,file,gzip_size,raw_size,gzip_eof,parse_offset,complete,record_count "
            "FROM segments ORDER BY seq"
        )
        return [{description[0]: value for description, value in zip(cursor.description, row)}
            for row in cursor.fetchall()]

    def has_incomplete_segment(self) -> bool:
        assert self.connection is not None
        return bool(self.connection.execute(
            "SELECT 1 FROM segments WHERE complete=0 LIMIT 1").fetchone())

    def indexable_locations(self) -> set[tuple[str, int]]:
        """已解析的 response/revisit 位置，用于反向检查 CDX 漏列。"""
        assert self.connection is not None
        cursor = self.connection.execute(
            "SELECT file,offset FROM records WHERE record_type IN ('response','revisit')"
        )
        return {(row[0], row[1]) for row in cursor.fetchall()}

    def revisits(self) -> list[dict[str, Any]]:
        """按卷序与记录序号稳定返回全部 revisit。"""
        assert self.connection is not None
        columns = ", ".join(_RECORD_COLUMNS)
        cursor = self.connection.execute(
            f"SELECT {columns} FROM records WHERE record_type='revisit' "
            "ORDER BY segment_seq,record_index"
        )
        return [dict(zip(_RECORD_COLUMNS, values)) for values in cursor.fetchall()]
