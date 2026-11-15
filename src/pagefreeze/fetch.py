"""单 URL 抓取：白名单前置、禁用自动跳转、限速、超时与体积上限。"""
from __future__ import annotations

import http.client
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from .config import CrawlConfig
from .errors import BodyTooLarge, FetchError, FetchTimeout, PolicyError
from .httpmsg import header_value, mime_type
from .policy import ensure_allowed_url, origin_of
from .ratelimit import OriginRegistry, RateLimiter

_LOGGER = logging.getLogger(__name__)
_HTTP_VERSIONS = {9: "HTTP/0.9", 10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2"}


@dataclass(frozen=True)
class FetchResult:
    """一条 HTTP 响应；body 保留线上字节（不自动解压）。"""
    url: str
    status: int
    reason: str
    version: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    mime: str

    @property
    def size(self) -> int:
        return len(self.body)

    @property
    def redirect_location(self) -> str | None:
        if 300 <= self.status < 400:
            return header_value(self.headers, "Location")
        return None

    @property
    def is_gzip(self) -> bool:
        return "gzip" in (header_value(self.headers, "Content-Encoding") or "").lower()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """把 301/302 当作普通响应交回调用方，以便显式归档每一跳。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _version_string(raw: object) -> str:
    if isinstance(raw, int):
        return _HTTP_VERSIONS.get(raw, "HTTP/1.1")
    return "HTTP/1.1"


def _declared_length(headers: tuple[tuple[str, str], ...]) -> int | None:
    raw = header_value(headers, "Content-Length")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _read_limited(stream: object, limit: int, declared: int | None) -> bytes:
    if declared is not None and declared > limit:
        raise BodyTooLarge(declared)
    reader = getattr(stream, "read")
    data = reader(limit + 1)
    if not isinstance(data, bytes):
        raise FetchError("FETCH_ERROR", "response stream did not return bytes")
    if len(data) > limit:
        raise BodyTooLarge(len(data))
    return data


def _normalize_headers(headers: tuple[tuple[str, str], ...], body: bytes
) -> tuple[tuple[str, str], ...]:
    """修正 urllib 已去除 chunked 编码后的 Content-Length，保留其余原始头。"""
    result: list[tuple[str, str]] = []
    saw_length = False
    for name, value in headers:
        lower = name.lower()
        if lower == "transfer-encoding":
            continue
        if lower == "content-length":
            result.append((name, str(len(body))))
            saw_length = True
        else:
            result.append((name, value))
    if not saw_length and body:
        result.append(("Content-Length", str(len(body))))
    return tuple(result)


def _timeout_error(url: str, detail: object) -> FetchTimeout:
    return FetchTimeout("FETCH_TIMEOUT", f"timed out fetching {url}: {detail}")


def fetch_once(
    url: str,
    config: CrawlConfig,
    limiter: RateLimiter | OriginRegistry | None = None,
) -> FetchResult:
    """抓取一条已规范化 URL；HTTP 4xx/5xx 作为正常响应返回。

    限速等待由调度线程在派发前完成（见 ``crawl.CrawlEngine``）；这里保留
    limiter 参数仅为兼容直接调用，传入 :class:`OriginRegistry` 时按最终
    请求 origin 取独立等待槽。
    """
    try:
        target = ensure_allowed_url(url)
    except PolicyError:
        raise
    if isinstance(limiter, OriginRegistry):
        limiter.get(origin_of(target)).wait()
    elif limiter is not None:
        limiter.wait()
    request = urllib.request.Request(target,
        method="GET",
        headers={"User-Agent": config.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=config.timeout_seconds)
        try:
            raw_headers = tuple((k, v) for k, v in response.headers.items())
            declared = _declared_length(raw_headers)
            body = _read_limited(response, config.max_body_bytes, declared)
            headers = _normalize_headers(raw_headers, body)
            return FetchResult(url=target,
                status=int(response.status),
                reason=str(response.reason or ""),
                version=_version_string(getattr(response, "version", 11)),
                headers=headers,
                body=body,
                mime=mime_type(headers),
            )
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        try:
            raw_headers = tuple((k, v) for k, v in exc.headers.items())
            declared = _declared_length(raw_headers)
            body = _read_limited(exc, config.max_body_bytes, declared)
            headers = _normalize_headers(raw_headers, body)
            return FetchResult(url=target,
                status=int(exc.code),
                reason=str(exc.reason or ""),
                version=_version_string(getattr(exc, "version", 11)),
                headers=headers,
                body=body,
                mime=mime_type(headers),
            )
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
            raise _timeout_error(target, reason) from exc
        raise FetchError("FETCH_ERROR", f"cannot fetch {target}: {reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise _timeout_error(target, exc) from exc
    except BodyTooLarge:
        raise
    except (http.client.HTTPException, OSError) as exc:
        _LOGGER.debug("fetch failed for %s: %s", target, exc)
        raise FetchError("FETCH_ERROR", f"cannot fetch {target}: {exc}") from exc
