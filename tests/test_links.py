"""资源发现解析器单元测试：HTML/CSS token、srcset、拒绝与深度标记。"""
from __future__ import annotations

from pagefreeze.css import parse_css_tokens
from pagefreeze.links import (
    discover_css_resources, discover_html_resources, discover_resources,
    parse_srcset,
)

BASE = "http://127.0.0.1:18080/dir/page.html"


def _by_raw(references, raw):
    return next(item for item in references if item.raw == raw)


def test_html_stylesheet_image_and_srcset_candidates() -> None:
    html = (b'<html><head><link rel="stylesheet" href="../css/site.css">'
            b'<link rel="preload" href="skip.js"></head><body>'
            b'<img src="img/a.png" srcset="img/a.png 1x, img/a-2x.png 2x">'
            b'<picture><source srcset="img/b.png 100w, img/b-2x.png 200w"></picture>'
            b'<script src="app.js"></script></body></html>')
    references = discover_html_resources(html, BASE, depth=1)
    assert [item.raw for item in references] == [
        "../css/site.css", "img/a.png", "img/a.png", "img/a-2x.png",
        "img/b.png", "img/b-2x.png",
    ]
    assert _by_raw(references, "../css/site.css").target == \
        "http://127.0.0.1:18080/css/site.css"
    assert _by_raw(references, "../css/site.css").kind == "stylesheet"
    assert all("app.js" not in item.raw for item in references)


def test_srcset_does_not_split_commas_inside_data_url() -> None:
    assert parse_srcset("a.png 1x, b.png 2x") == ["a.png", "b.png"]
    assert parse_srcset("data:image/png;base64,AAA,BBB 1x") == [
        "data:image/png;base64,AAA,BBB"]
    assert parse_srcset("  a.png  1x ,, b.png 2x ") == ["a.png", "b.png"]


def test_css_relative_urls_resolve_against_the_css_itself() -> None:
    css = (b'@import "nested/more.css";\n'
           b'body{background:url("../img/bg.png")}\n'
           b'.x{background:url(deep/thing.png)}\n')
    css_url = "http://127.0.0.1:18080/theme/css/main.css"
    references = discover_css_resources(css, css_url, depth=2)
    imports = [item for item in references if item.kind == "css-import"]
    urls = [item for item in references if item.kind == "css-url"]
    assert imports[0].target == "http://127.0.0.1:18080/theme/css/nested/more.css"
    assert [item.target for item in urls] == [
        "http://127.0.0.1:18080/theme/img/bg.png",
        "http://127.0.0.1:18080/theme/css/deep/thing.png",
    ]
    assert all(item.source_url == css_url for item in references)


def test_css_comments_and_strings_do_not_produce_fake_urls() -> None:
    css = (b'/* url(commented.png) */\n'
           b'.a{content:"url(string-only.png)"}\n'
           b".b{content:'@import \"in-string.css\"'}\n"
           b'.c{background:url(real.png)}\n')
    references = discover_css_resources(css, "http://127.0.0.1:18080/s.css", 1)
    assert [item.raw for item in references] == ["real.png"]
    tokens = parse_css_tokens(css)
    assert [token.raw for token in tokens] == ["real.png"]


def test_css_quotes_brackets_escapes_and_data_uri() -> None:
    css = (b'.a{background:url("quoted name.png")}\n'
           b'.b{background:url(a(b)c.png)}\n'
           b'.c{background:url(sp\\ ace.png)}\n'
           b'.d{background:url("\\65 scaped.png")}\n'
           b'.e{background:url(data:image/png;base64,AAAA)}\n'
           b'.f{background:url(#fragment)}\n')
    references = discover_css_resources(css, "http://127.0.0.1:18080/s.css", 1)
    by_raw = {item.raw: item for item in references}
    assert by_raw["quoted name.png"].target == "http://127.0.0.1:18080/quoted name.png"
    assert by_raw["a(b)c.png"].target == "http://127.0.0.1:18080/a(b)c.png"
    assert by_raw["sp ace.png"].target == "http://127.0.0.1:18080/sp ace.png"
    assert by_raw["escaped.png"].target == "http://127.0.0.1:18080/escaped.png"
    assert by_raw["data:image/png;base64,AAAA"].outcome == "skipped"
    assert by_raw["data:image/png;base64,AAAA"].reason == "data_uri"
    assert by_raw["#fragment"].reason == "fragment_only"


def test_malformed_css_url_is_skipped_without_stopping_others() -> None:
    css = b'.a{background:url(ok.png)} .b{background:url("unclosed.png} .c{url(after.png)}'
    references = discover_css_resources(css, "http://127.0.0.1:18080/s.css", 1)
    assert [item.raw for item in references if item.outcome == "discovered"] == [
        "ok.png", "after.png"]
    malformed = [item for item in references if item.outcome == "skipped"]
    assert len(malformed) == 1
    assert malformed[0].reason == "malformed_reference"


def test_external_absolute_reference_is_resolved_then_policy_rejects() -> None:
    html = b'<link rel="stylesheet" href="http://203.0.113.7/ext.css">'
    references = discover_html_resources(html, BASE, 1)
    # 解析器只做规范化；回环校验由 crawl 层在连接前完成。
    assert references[0].target == "http://203.0.113.7/ext.css"
    assert references[0].outcome == "discovered"


def test_unsupported_scheme_and_empty_reference_are_skipped() -> None:
    html = (b'<img src="javascript:alert(1)">'
            b'<img src="  ">'
            b'<img src="/ok.png">')
    references = discover_html_resources(html, BASE, 1)
    reasons = {item.raw: item.reason for item in references}
    assert reasons["javascript:alert(1)"] == "unsupported_scheme"
    assert reasons["/ok.png"] is None


def test_content_type_selects_parser_and_ignores_others() -> None:
    html = b'<img src="a.png">'
    css = b'.x{background:url(b.png)}'
    assert discover_resources(html, "text/html; charset=utf-8", BASE, 1)[0].raw == "a.png"
    assert discover_resources(css, "text/css", BASE, 1)[0].raw == "b.png"
    assert discover_resources(b"url(nope.png)", "image/png", BASE, 1) == []


def test_nested_import_chain_and_cycle_detection_inputs() -> None:
    """fixture 中 style.css → nested/import.css → deep.css 的嵌套链可解析。"""
    base = "http://127.0.0.1:18080/res/"
    first = discover_css_resources(b'@import "nested/import.css";',
        base + "style.css", 2)
    assert first[0].target == base + "nested/import.css"
    second = discover_css_resources(b'@import "../deep.css";',
        base + "nested/import.css", 3)
    assert second[0].target == base + "deep.css"
    cycle_a = discover_css_resources(b'@import "cycle-b.css";',
        base + "theme/cycle-a.css", 3)
    cycle_b = discover_css_resources(b'@import "cycle-a.css";',
        base + "theme/cycle-b.css", 4)
    assert cycle_a[0].target == base + "theme/cycle-b.css"
    assert cycle_b[0].target == base + "theme/cycle-a.css"


def test_srcset_repeated_candidates_stay_distinct_edges() -> None:
    html = b'<img src="a.png" srcset="a.png 1x, a.png 2x, b.png 2x">'
    references = discover_html_resources(html, BASE, 1)
    raws = [item.raw for item in references]
    assert raws == ["a.png", "a.png", "a.png", "b.png"]
    assert {item.target for item in references if item.raw == "a.png"} == {
        "http://127.0.0.1:18080/dir/a.png"}
