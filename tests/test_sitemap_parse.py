"""Sitemap XML 解析单元测试：根元素、命名空间、相对 loc、畸形与 XXE 防护。"""
from __future__ import annotations

import pytest
from pagefreeze.sitemap import SitemapParseError, parse_sitemap

BASE = "http://127.0.0.1:18080/sitemaps/root.xml"


def test_urlset_with_default_namespace_and_relative_locs() -> None:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>/sm/one</loc></url>"
        "<url><loc>sm/two</loc></url>"
        '<url><loc>http://127.0.0.1:18080/sm/three</loc></url>'
        "</urlset>"
    ).encode("utf-8")
    result = parse_sitemap(body, BASE)
    assert [entry.loc for entry in result.pages] == [
        "http://127.0.0.1:18080/sm/one",
        "http://127.0.0.1:18080/sitemaps/sm/two",
        "http://127.0.0.1:18080/sm/three",
    ]
    assert [entry.raw_loc for entry in result.pages] == [
        "/sm/one", "sm/two", "http://127.0.0.1:18080/sm/three"]
    assert result.child_sitemaps == ()


def test_urlset_without_namespace_is_accepted() -> None:
    body = b'<?xml version="1.0"?><urlset><url><loc>/a</loc></url></urlset>'
    result = parse_sitemap(body, BASE)
    assert result.pages[0].loc == "http://127.0.0.1:18080/a"


def test_sitemap_index_returns_child_entries() -> None:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>/sitemaps/child.xml</loc></sitemap>"
        "<sitemap><loc>sitemaps/loop.xml</loc></sitemap>"
        "</sitemapindex>"
    ).encode("utf-8")
    result = parse_sitemap(body, BASE)
    assert result.pages == ()
    assert [entry.loc for entry in result.child_sitemaps] == [
        "http://127.0.0.1:18080/sitemaps/child.xml",
        "http://127.0.0.1:18080/sitemaps/sitemaps/loop.xml",
    ]
    assert all(entry.is_index for entry in result.child_sitemaps)


def test_unknown_root_element_is_rejected() -> None:
    with pytest.raises(SitemapParseError) as caught:
        parse_sitemap(b'<?xml version="1.0"?><html><loc>/a</loc></html>', BASE)
    assert caught.value.code == "SITEMAP_PARSE_ERROR"


def test_malformed_xml_only_raises_parse_error() -> None:
    with pytest.raises(SitemapParseError) as caught:
        parse_sitemap(b"<?xml version='1.0'?><urlset><url><loc>/a</loc></url>", BASE)
    assert caught.value.code == "SITEMAP_PARSE_ERROR"


def test_empty_document_and_non_xml_are_rejected() -> None:
    for body in (b"", b"not xml at all", b"\xef\xbb\xbfhello"):
        with pytest.raises(SitemapParseError):
            parse_sitemap(body, BASE)


def test_doctype_declaration_is_rejected_before_entity_expansion() -> None:
    body = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE urlset [<!ENTITY x "boom">]>'
        "<urlset><url><loc>/&x;</loc></url></urlset>"
    ).encode("utf-8")
    with pytest.raises(SitemapParseError) as caught:
        parse_sitemap(body, BASE)
    assert caught.value.code == "SITEMAP_PARSE_ERROR"


def test_external_general_entity_does_not_read_local_file(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    body = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        "<urlset><url><loc>/&xxe;</loc></url></urlset>"
    ).encode("utf-8")
    # DOCTYPE 被直接拒绝；即使绕过该层，外部实体 feature 也处于关闭状态。
    with pytest.raises(SitemapParseError):
        parse_sitemap(body, BASE)
    assert secret.read_text(encoding="utf-8") == "SECRET"


def test_external_parameter_entity_is_disabled(tmp_path) -> None:
    dtd = tmp_path / "evil.dtd"
    dtd.write_text('<!ENTITY p "leak">', encoding="utf-8")
    body = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE urlset [<!ENTITY % ext SYSTEM "file://{dtd}"> %ext;]>'
        "<urlset/>"
    ).encode("utf-8")
    with pytest.raises(SitemapParseError):
        parse_sitemap(body, BASE)


def test_invalid_loc_surfaces_as_parse_error() -> None:
    body = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>ftp://127.0.0.1/a</loc></url></urlset>").encode("utf-8")
    with pytest.raises(SitemapParseError):
        parse_sitemap(body, BASE)
