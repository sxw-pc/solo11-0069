"""URL 规范化、白名单、绑定地址与 robots 解析的单元测试。"""
from __future__ import annotations

import pytest
from pagefreeze.errors import PolicyError
from pagefreeze.policy import (
    ensure_allowed_url, is_loopback_target, normalize_url, parse_robots,
    urlkey, validate_bind_host,
)


def test_normalize_removes_fragment_and_default_port() -> None:
    assert normalize_url("HTTP://Example.INVALID:80/a?x=1#fragment") == "http://example.invalid/a?x=1"
    assert normalize_url("../b#x", "http://127.0.0.1:18080/a/c") == "http://127.0.0.1:18080/b"


def test_urlkey_is_surt_style_and_lowercase() -> None:
    value = "http://127.0.0.1:18080/List?Page=1#part"
    assert urlkey(value) == "http://1,0,0,127:18080)/list?page=1"
    assert urlkey("http://Example.INVALID:80/A") == "http://invalid,example)/a"


def test_loopback_allowlist_and_external_rejection() -> None:
    for url in ("http://127.0.0.1:18080/",
        "http://localhost:18080/",
        "http://[::1]:18080/",
    ):
        assert ensure_allowed_url(url)
        assert is_loopback_target(url)
    with pytest.raises(PolicyError) as caught:
        ensure_allowed_url("http://203.0.113.7/outside")
    assert caught.value.code == "TARGET_NOT_LOOPBACK"


def test_non_loopback_bind_host_is_rejected() -> None:
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    with pytest.raises(PolicyError) as caught:
        validate_bind_host("0.0.0.0")
    assert caught.value.code == "BIND_NOT_LOOPBACK"


def test_robots_longest_match_and_allow() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /private/\nAllow: /private/open\n"
        "Disallow: /tmp/*.txt$\n",
        "pagefreeze-test",
    )
    assert rules.allowed("/")
    assert not rules.allowed("/private/secret")
    assert rules.allowed("/private/open/page")
    assert not rules.allowed("/tmp/report.txt")
    assert rules.allowed("/tmp/report.txt?download=1")


def test_robots_specific_agent_overrides_star() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: pagefreeze\nDisallow: /secret\n",
        "pagefreeze-test",
    )
    assert rules.allowed("/public")
    assert not rules.allowed("/secret/page")
