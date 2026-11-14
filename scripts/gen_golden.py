"""生成固定时钟下的 golden 摘要；测试只读 fixtures/expected/golden.json。

用法：python scripts/gen_golden.py
本脚本不会把测试数据写入 runtime 以外的持久位置，输出只包含相对文件名与摘要。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from pagefreeze.cdx import read_index  # noqa: E402
from pagefreeze.config import load_settings  # noqa: E402
from pagefreeze.pipeline import execute_demo  # noqa: E402
from pagefreeze.digest import sha256_file  # noqa: E402
from pagefreeze.util import parse_clock, stable_json  # noqa: E402
FIXED_NOW = "20240115000000"


def build_golden() -> dict[str, object]:
    settings = load_settings(ROOT / "config")
    with tempfile.TemporaryDirectory(prefix="pagefreeze-golden-") as temporary:
        result = execute_demo(settings, Path(temporary) / "out", parse_clock(FIXED_NOW))
        rows = read_index(result.paths.index_path)
        segments = sorted(result.paths.archive_dir.glob("seg-*.warc.gz"))
        write = result.write
        return {"now": FIXED_NOW,
            "records": len(write.records),
            "requests": write.requests,
            "responses": write.responses,
            "revisits": write.revisits,
            "rejected": len(result.crawl.rejected),
            "skipped": result.crawl.skip_reasons(),
            "segments": len(write.segments),
            "index_entries": len(rows),
            "verify": result.verify.status,
            "index_sha256": sha256_file(result.paths.index_path),
            "segment_sha256": {path.name: sha256_file(path) for path in segments},
        }


def main() -> int:
    golden = build_golden()
    destination = ROOT / "fixtures" / "expected" / "golden.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(stable_json(golden), encoding="utf-8")
    print(f"GOLDEN_WRITTEN path={destination.relative_to(ROOT)} records={golden['records']}")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
