"""配置加载、字段校验与输出路径解析。

配置文件是该工具唯一的外部输入面。所有校验失败都抛出带稳定错误码的
``ConfigError``，CLI 只负责呈现，不重复解释配置。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from .errors import ConfigError
from .digest import sha256_bytes


@dataclass(frozen=True)
class CrawlConfig:
    """有界抓取参数。"""
    seed_urls: tuple[str, ...]
    max_depth: int
    max_pages: int
    max_concurrency: int
    request_interval_ms: int
    timeout_seconds: float
    max_body_bytes: int
    allowed_mime: tuple[str, ...]
    respect_robots: bool
    user_agent: str
    sitemap_entry: str
    sitemap_max_depth: int
    sitemap_max_files: int


@dataclass(frozen=True)
class ArchiveConfig:
    """归档卷、索引、台账与报告布局。"""
    archive_dir: str
    segment_size_bytes: int
    digest_algorithm: str
    index_filename: str
    ledger_filename: str
    report_dir: str
    uuid_namespace: uuid.UUID


@dataclass(frozen=True)
class Settings:
    """一次运行使用的完整配置。"""
    crawl: CrawlConfig
    archive: ArchiveConfig
    digest: str


@dataclass(frozen=True)
class OutputPaths:
    """由 ``--out`` 与配置拼出的、已解析的产物路径。"""
    root: Path
    archive_dir: Path
    index_path: Path
    ledger_path: Path
    report_dir: Path
    verify_json: Path
    verify_md: Path
    report_json: Path
    report_md: Path


def _require(mapping: dict[str, Any], key: str, section: str) -> Any:
    if key not in mapping:
        raise ConfigError("CFG_MISSING", f"{section}.{key} is required")
    return mapping[key]


def _integer(mapping: dict[str, Any], key: str, section: str, code: str) -> int:
    value = _require(mapping, key, section)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("CFG_TYPE", f"{section}.{key} must be an integer")
    return value


def _number(mapping: dict[str, Any], key: str, section: str) -> float:
    value = _require(mapping, key, section)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("CFG_TYPE", f"{section}.{key} must be a number")
    return float(value)


def _string(mapping: dict[str, Any], key: str, section: str) -> str:
    value = _require(mapping, key, section)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("CFG_EMPTY", f"{section}.{key} must be a non-empty string")
    return value.strip()


def _boolean(mapping: dict[str, Any], key: str, section: str) -> bool:
    value = _require(mapping, key, section)
    if not isinstance(value, bool):
        raise ConfigError("CFG_TYPE", f"{section}.{key} must be a boolean")
    return value


def _safe_relative(value: str, code: str, label: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ConfigError(code, f"{label} must be a relative path without '..'")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError("CFG_FILE", f"config file not found: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("CFG_JSON", f"cannot parse {path.name}") from exc
    if not isinstance(value, dict):
        raise ConfigError("CFG_TYPE", f"{path.name} must contain a JSON object")
    return value


def _validate_crawl(raw: dict[str, Any]) -> CrawlConfig:
    seeds = _require(raw, "seed_urls", "crawl")
    if not isinstance(seeds, list) or not seeds:
        raise ConfigError("CFG_EMPTY", "crawl.seed_urls must be a non-empty list")
    seed_tuple: list[str] = []
    for seed in seeds:
        if not isinstance(seed, str):
            raise ConfigError("CFG_SEED_URL", "seed URL must be a string")
        parsed = urlsplit(seed)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError("CFG_SEED_URL", "seed URL must be absolute http(s)")
        seed_tuple.append(seed)
    depth = _integer(raw, "max_depth", "crawl", "CFG_DEPTH_RANGE")
    if not 1 <= depth <= 10:
        raise ConfigError("CFG_DEPTH_RANGE", "crawl.max_depth must be 1..10")
    pages = _integer(raw, "max_pages", "crawl", "CFG_PAGES_RANGE")
    if not 1 <= pages <= 5000:
        raise ConfigError("CFG_PAGES_RANGE", "crawl.max_pages must be 1..5000")
    concurrency = _integer(raw, "max_concurrency", "crawl", "CFG_CONCURRENCY_RANGE")
    if not 1 <= concurrency <= 16:
        raise ConfigError("CFG_CONCURRENCY_RANGE", "crawl.max_concurrency must be 1..16")
    interval = _integer(raw, "request_interval_ms", "crawl", "CFG_RATE_RANGE")
    if not 0 <= interval <= 60000:
        raise ConfigError("CFG_RATE_RANGE", "crawl.request_interval_ms must be 0..60000")
    timeout = _number(raw, "timeout_seconds", "crawl")
    if not 0.05 <= timeout <= 60:
        raise ConfigError("CFG_TIMEOUT_RANGE", "crawl.timeout_seconds must be 0.05..60")
    body_limit = _integer(raw, "max_body_bytes", "crawl", "CFG_BODY_RANGE")
    if not 1 <= body_limit <= 100_000_000:
        raise ConfigError("CFG_BODY_RANGE", "crawl.max_body_bytes must be 1..100000000")
    mime = _require(raw, "allowed_mime", "crawl")
    if not isinstance(mime, list) or not mime:
        raise ConfigError("CFG_MIME", "crawl.allowed_mime must be a non-empty list")
    mime_values: list[str] = []
    for item in mime:
        if not isinstance(item, str) or "/" not in item:
            raise ConfigError("CFG_MIME", "each allowed_mime entry must look like type/subtype")
        mime_values.append(item.strip().lower())
    entry = _string(raw, "sitemap_entry", "crawl")
    if entry not in ("priority", "bfs"):
        raise ConfigError("CFG_SITEMAP_ENTRY",
            "crawl.sitemap_entry must be 'priority' or 'bfs'")
    sitemap_depth = _integer(raw, "sitemap_max_depth", "crawl", "CFG_SITEMAP_DEPTH_RANGE")
    if not 0 <= sitemap_depth <= 4:
        raise ConfigError("CFG_SITEMAP_DEPTH_RANGE", "crawl.sitemap_max_depth must be 0..4")
    sitemap_files = _integer(raw, "sitemap_max_files", "crawl", "CFG_SITEMAP_FILES_RANGE")
    if not 1 <= sitemap_files <= 256:
        raise ConfigError("CFG_SITEMAP_FILES_RANGE", "crawl.sitemap_max_files must be 1..256")
    return CrawlConfig(seed_urls=tuple(seed_tuple),
        max_depth=depth,
        max_pages=pages,
        max_concurrency=concurrency,
        request_interval_ms=interval,
        timeout_seconds=timeout,
        max_body_bytes=body_limit,
        allowed_mime=tuple(mime_values),
        respect_robots=_boolean(raw, "respect_robots", "crawl"),
        user_agent=_string(raw, "user_agent", "crawl"),
        sitemap_entry=entry,
        sitemap_max_depth=sitemap_depth,
        sitemap_max_files=sitemap_files,
    )


def _validate_archive(raw: dict[str, Any]) -> ArchiveConfig:
    archive_dir = _safe_relative(_string(raw, "archive_dir", "archive"), "CFG_PATH", "archive.archive_dir")
    size = _integer(raw, "segment_size_bytes", "archive", "CFG_SEGMENT_RANGE")
    if not 1024 <= size <= 1_000_000_000:
        raise ConfigError("CFG_SEGMENT_RANGE", "archive.segment_size_bytes must be >=1024")
    algorithm = _string(raw, "digest_algorithm", "archive").lower()
    if algorithm != "sha256":
        raise ConfigError("CFG_DIGEST", "archive.digest_algorithm must be sha256")
    index_name = _safe_relative(
        _string(raw, "index_filename", "archive"), "CFG_PATH", "archive.index_filename")
    ledger_name = _safe_relative(
        _string(raw, "ledger_filename", "archive"), "CFG_PATH", "archive.ledger_filename")
    report_dir = _safe_relative(_string(raw, "report_dir", "archive"), "CFG_PATH", "archive.report_dir")
    if not index_name.endswith(".cdx"):
        raise ConfigError("CFG_PATH", "archive.index_filename must end with .cdx")
    if not ledger_name.endswith(".sqlite3"):
        raise ConfigError("CFG_PATH", "archive.ledger_filename must end with .sqlite3")
    raw_namespace = _string(raw, "uuid_namespace", "archive")
    try:
        namespace = uuid.UUID(raw_namespace)
    except ValueError as exc:
        raise ConfigError("CFG_UUID", "archive.uuid_namespace must be a UUID") from exc
    return ArchiveConfig(archive_dir=archive_dir,
        segment_size_bytes=size,
        digest_algorithm=algorithm,
        index_filename=index_name,
        ledger_filename=ledger_name,
        report_dir=report_dir,
        uuid_namespace=namespace,
    )


def _config_files(path: str | Path | None) -> tuple[Path, Path]:
    if path is None:
        base = Path(__file__).resolve().parents[2] / "config"
        return base / "crawl.json", base / "archive.json"
    candidate = Path(path)
    if candidate.is_dir():
        return candidate / "crawl.json", candidate / "archive.json"
    if candidate.name == "archive.json":
        return candidate.parent / "crawl.json", candidate
    return candidate, candidate.parent / "archive.json"


def _canonical(settings: CrawlConfig, archive: ArchiveConfig) -> str:
    payload: dict[str, Any] = {"tool": "pagefreeze/0.1.0",
        "crawl": {"seed_urls": list(settings.seed_urls),
            "max_depth": settings.max_depth,
            "max_pages": settings.max_pages,
            "max_concurrency": settings.max_concurrency,
            "request_interval_ms": settings.request_interval_ms,
            "timeout_seconds": settings.timeout_seconds,
            "max_body_bytes": settings.max_body_bytes,
            "allowed_mime": list(settings.allowed_mime),
            "respect_robots": settings.respect_robots,
            "user_agent": settings.user_agent,
            "sitemap_entry": settings.sitemap_entry,
            "sitemap_max_depth": settings.sitemap_max_depth,
            "sitemap_max_files": settings.sitemap_max_files,
        },
        "archive": {"archive_dir": archive.archive_dir,
            "segment_size_bytes": archive.segment_size_bytes,
            "digest_algorithm": archive.digest_algorithm,
            "index_filename": archive.index_filename,
            "ledger_filename": archive.ledger_filename,
            "report_dir": archive.report_dir,
            "uuid_namespace": str(archive.uuid_namespace),
        },
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256_bytes(text.encode("utf-8"))


def load_settings(path: str | Path | None = None) -> Settings:
    """读取并校验 crawl.json / archive.json，计算配置摘要。"""
    crawl_path, archive_path = _config_files(path)
    crawl = _validate_crawl(_read_json(crawl_path))
    archive = _validate_archive(_read_json(archive_path))
    return Settings(crawl=crawl, archive=archive, digest=_canonical(crawl, archive))


def paths_for(settings: Settings, out_root: str | Path) -> OutputPaths:
    """在 ``--out`` 下解析全部产物路径，并再次阻止路径逃逸。"""
    root = Path(out_root)
    archive_dir = root / settings.archive.archive_dir
    report_dir = root / settings.archive.report_dir
    return OutputPaths(root=root,
        archive_dir=archive_dir,
        index_path=archive_dir / settings.archive.index_filename,
        ledger_path=root / settings.archive.ledger_filename,
        report_dir=report_dir,
        verify_json=report_dir / "verify.json",
        verify_md=report_dir / "verify.md",
        report_json=report_dir / "report.json",
        report_md=report_dir / "report.md",
    )
