"""内置离线 mock 站点，仅监听回环地址。"""
from .server import create_mock_server

__all__ = ["create_mock_server"]
