"""共享测试夹具：在会话内跑一次固定时钟的 demo，供只读断言。"""
from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path
from typing import Iterator
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from pagefreeze.config import load_settings  # noqa: E402
from pagefreeze.mocksite import create_mock_server  # noqa: E402
from pagefreeze.pipeline import PipelineResult, execute_demo  # noqa: E402
from pagefreeze.util import parse_clock  # noqa: E402
FIXED_NOW = "20240115000000"


@pytest.fixture(scope="session")
def demo_result(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    out = tmp_path_factory.mktemp("pagefreeze-demo")
    settings = load_settings(ROOT / "config")
    return execute_demo(settings, out, parse_clock(FIXED_NOW))


@pytest.fixture()
def archive_copy(demo_result: PipelineResult, tmp_path: Path) -> Path:
    destination = tmp_path / "archive"
    shutil.copytree(demo_result.paths.archive_dir, destination)
    return destination


@pytest.fixture()
def resource_site() -> Iterator[str]:
    """在随机回环端口启动 mock 站点，返回其 base URL；零外部连接。"""
    server = create_mock_server("127.0.0.1", 0, parse_clock(FIXED_NOW))
    thread = threading.Thread(target=server.serve_forever,
        kwargs={"poll_interval": 0.02},
        daemon=True,
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
