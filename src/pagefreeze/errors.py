"""统一异常类型：CLI 依赖稳定的错误码，而不是解析异常文本。"""
from __future__ import annotations


class PagefreezeError(Exception):
    """所有可预期错误的基类；``code`` 会原样出现在 CLI 与报告中。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigError(PagefreezeError):
    """配置缺失、类型错误或越界。"""


class PolicyError(PagefreezeError):
    """URL / 绑定地址违反回环白名单。"""


class FetchError(PagefreezeError):
    """网络抓取失败。"""


class FetchTimeout(FetchError):
    """连接或读取超时。"""


class BodyTooLarge(FetchError):
    """响应体超过配置上限；只记录大小，不保存正文。"""

    def __init__(self, size: int, message: str = "response body exceeds limit") -> None:
        super().__init__("BODY_TOO_LARGE", message)
        self.size = size


class WarcError(PagefreezeError):
    """WARC 记录构造或序列化错误。"""


class WarcParseError(WarcError):
    """WARC 解析错误，带文件内位置，供核验定位。"""

    def __init__(
        self,
        code: str,
        message: str,
        record_index: int = 0,
        offset: int = 0,
    ) -> None:
        super().__init__(code, message)
        self.record_index = record_index
        self.offset = offset


class CdxError(PagefreezeError):
    """CDX 索引读取或写入错误。"""


class LedgerError(PagefreezeError):
    """SQLite 台账错误。"""


class ReplayError(PagefreezeError):
    """回放请求错误；``http_status`` 直接映射到 HTTP 响应。"""

    def __init__(self, code: str, message: str, http_status: int = 500) -> None:
        super().__init__(code, message)
        self.http_status = http_status
