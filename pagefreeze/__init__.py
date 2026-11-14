"""零安装启动垫片：把 ``src`` 加入导入路径后交给真正的实现包。"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 与 src/pagefreeze/__init__.py 保持一致；子模块通过包属性读取软件标识。
__version__ = "0.1.0"
SOFTWARE = f"pagefreeze/{__version__}"

# 让 ``python -m pagefreeze`` 在未安装时也能从 src 布局解析子模块。
__path__ = [str(_SRC / "pagefreeze")]

