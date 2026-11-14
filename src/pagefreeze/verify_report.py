"""核验结果的稳定 JSON 与 Markdown 呈现。"""
from __future__ import annotations

from pathlib import Path
from .util import stable_json, write_text
from .verify import VerifyResult


def result_to_dict(result: VerifyResult) -> dict[str, object]:
    """固定报告键顺序。"""
    return {"schema": "pagefreeze.verify/1",
        "status": result.status,
        "stats": result.stats,
        "checks": [{"name": check.name, "status": check.status, "detail": check.detail}
            for check in result.checks],
        "issues": [{"file": issue.file, "record_index": issue.record_index,
             "code": issue.code, "message": issue.message}
            for issue in result.issues],
    }


def write_verify_files(result: VerifyResult, json_path: Path, md_path: Path) -> None:
    """写出 JSON 与 Markdown 核验报告，内容不包含绝对路径或系统时间。"""
    write_text(json_path, stable_json(result_to_dict(result)))
    lines = ["# 封页台完整性核验",
        "",
        f"- 结论：**{result.status}**",
        f"- 分卷：{result.stats.get('segments', 0)}",
        f"- 记录：{result.stats.get('records', 0)}",
        f"- 索引条目：{result.stats.get('index_entries', 0)}",
        "",
        "## 检查项",
        "",
        "| 检查 | 状态 | 详情 |",
        "| --- | --- | --- |",
    ]
    for check in result.checks:
        lines.append(f"| {check.name} | {check.status} | {check.detail} |")
    lines.extend(["", "## 问题", ""])
    if not result.issues:
        lines.append("无。")
    else:
        lines.extend(["| 文件 | 记录序号 | 原因码 | 说明 |", "| --- | --- | --- | --- |"])
        for issue in result.issues:
            message = issue.message.replace("|", "\\|")
            index = "" if issue.record_index is None else str(issue.record_index)
            lines.append(f"| {issue.file} | {index} | `{issue.code}` | {message} |")
    write_text(md_path, "\n".join(lines) + "\n")
