"""URL 规范化、白名单、绑定地址与 robots 解析的单元测试。"""
from __future__ import annotations

import pytest
from pagefreeze.errors import PolicyError
from pagefreeze.policy import (
    ensure_allowed_url, is_loopback_target, normalize_url, origin_of, parse_robots,
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


def test_crawl_delay_integer_is_parsed_for_selected_group() -> None:
    rules = parse_robots("User-agent: *\nCrawl-delay: 7\nDisallow: /private/\n",
        "pagefreeze-test",
    )
    assert rules.crawl_delay_seconds == 7.0
    assert not rules.allowed("/private/x")
    assert rules.allowed("/public")
    assert rules.warnings == ()


def test_crawl_delay_specific_agent_takes_precedence_over_star() -> None:
    rules = parse_robots(
        "User-agent: *\nCrawl-delay: 9\n\n"
        "User-agent: pagefreeze\nCrawl-delay: 3\nDisallow: /secret\n",
        "pagefreeze-test",
    )
    assert rules.crawl_delay_seconds == 3.0
    assert not rules.allowed("/secret/x")
    star_only = parse_robots(
        "User-agent: *\nCrawl-delay: 9\n\n"
        "User-agent: other\nCrawl-delay: 3\n",
        "pagefreeze-test",
    )
    assert star_only.crawl_delay_seconds == 9.0


def test_crawl_delay_decimal_and_garbage_are_ignored_with_warnings() -> None:
    text = ("User-agent: *\n"
        "Crawl-delay: 1.5\n"
        "Crawl-delay: -3\n"
        "Crawl-delay: soon\n"
        "Crawl-delay:\n"
        "Crawl-delay: 4\n"
        "Disallow: /private/\n")
    rules = parse_robots(text, "pagefreeze-test")
    assert rules.crawl_delay_seconds == 4.0
    assert not rules.allowed("/private/secret")
    reasons = {warning.split(":", 1)[0] for warning in rules.warnings}
    assert reasons == {"crawl_delay_decimal_unsupported", "crawl_delay_invalid",
        "crawl_delay_negative", "crawl_delay_empty"}


def test_crawl_delay_negative_value_is_invalid() -> None:
    rules = parse_robots("User-agent: *\nCrawl-delay: -10\n", "pagefreeze-test")
    assert rules.crawl_delay_seconds is None
    assert any(warning.startswith("crawl_delay_negative") for warning in rules.warnings)


def test_conflicting_duplicate_crawl_delay_warns_and_last_wins() -> None:
    rules = parse_robots("User-agent: *\nCrawl-delay: 2\nCrawl-delay: 5\n",
        "pagefreeze-test")
    assert rules.crawl_delay_seconds == 5.0
    assert any(warning.startswith("crawl_delay_conflict") for warning in rules.warnings)
    repeated = parse_robots("User-agent: *\nCrawl-delay: 2\nCrawl-delay: 2\n",
        "pagefreeze-test")
    assert repeated.crawl_delay_seconds == 2.0
    assert repeated.warnings == ()


def test_crawl_delay_in_unselected_group_does_not_leak() -> None:
    rules = parse_robots(
        "User-agent: other-bot\nCrawl-delay: 30\n\nUser-agent: *\nDisallow: /\n",
        "pagefreeze-test",
    )
    assert rules.crawl_delay_seconds is None
    assert not rules.allowed("/anything")


def test_specific_agent_group_with_only_delay_blocks_star_fallback() -> None:
    rules = parse_robots(
        "User-agent: pagefreeze\nCrawl-delay: 8\n\n"
        "User-agent: *\nDisallow: /\n",
        "pagefreeze-test",
    )
    assert rules.crawl_delay_seconds == 8.0
    assert rules.allowed("/anything")
    star = parse_robots(
        "User-agent: other\nCrawl-delay: 8\n\nUser-agent: *\nDisallow: /\n",
        "pagefreeze-test",
    )
    assert not star.allowed("/anything")


def test_origin_of_normalizes_to_scheme_host_port() -> None:
    assert origin_of("http://127.0.0.1:18080/a/b?x=1#f") == "http://127.0.0.1:18080"
    assert origin_of("http://localhost:80/") == "http://localhost"
    with pytest.raises(PolicyError):
        origin_of("not-a-url")
