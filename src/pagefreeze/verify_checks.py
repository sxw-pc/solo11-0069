"""核验检查项分组：稳定原因码到高层检查的映射与汇总状态。"""
from __future__ import annotations

from .verify_types import VerifyCheck, VerifyIssue

# 各检查项负责的稳定原因码；新增原因码时在此登记，报告分组随之确定。
_CHECK_GROUPS: dict[str, set[str]] = {
    "archive_structure": {
        "ARCHIVE_EMPTY", "FILE_READ_ERROR", "WARCINFO_MISSING", "WARC_MALFORMED",
        "WARC_HEADER_MISSING", "WARC_TYPE_UNKNOWN", "WARC_RECORD_ID_DUPLICATE",
        "HTTP_MESSAGE_MALFORMED", "GZIP_EMPTY", "GZIP_TRAILING_DATA",
        "VERIFY_STORE_ERROR",
    },
    "truncation": {"GZIP_TRUNCATED", "GZIP_INVALID", "WARC_TRUNCATED"},
    "digests": {"CONTENT_LENGTH_MISMATCH", "BLOCK_DIGEST_MISMATCH",
        "PAYLOAD_DIGEST_MISMATCH"},
    "index_consistency": {
        "CDX_MISSING", "CDX_MALFORMED", "CDX_READ_ERROR", "CDX_RECORD_INVALID",
        "INDEX_MISSING_RECORD", "INDEX_RECORD_UNRESOLVED", "INDEX_ENTRY_MISSING",
        "INDEX_ENTRY_COUNT_MISMATCH", "INDEX_LENGTH_MISMATCH", "INDEX_URL_MISMATCH",
        "INDEX_URLKEY_MISMATCH", "INDEX_TIMESTAMP_MISMATCH", "INDEX_STATUS_MISMATCH",
        "INDEX_MIME_MISMATCH", "INDEX_DIGEST_MISMATCH",
        "INDEX_RECORD_TYPE_MISMATCH", "INDEX_DUPLICATE_ENTRY",
    },
    "revisit_links": {"REVISIT_TARGET_MISSING", "REVISIT_TARGET_UNRESOLVED",
        "REVISIT_DIGEST_MISMATCH", "REVISIT_PROFILE_INVALID"},
}


def build_checks(issues: list[VerifyIssue]) -> list[VerifyCheck]:
    """按固定检查项顺序汇总每项的 pass/fail 与问题数。"""
    result: list[VerifyCheck] = []
    for name, codes in _CHECK_GROUPS.items():
        failed = [issue for issue in issues if issue.code in codes]
        status = "fail" if failed else "pass"
        detail = f"{len(failed)} issue(s)" if failed else "ok"
        result.append(VerifyCheck(name=name, status=status,
            detail=detail, issue_count=len(failed)))
    return result
