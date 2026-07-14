"""html_text.py 离线单测：HTML 正文 -> 纯文本，基于真实 fixture（Bitunix/Weex 的 Zendesk
articles.json body 字段）+ 手写小样本覆盖边界情况。表格格式要求跟 slate_json.py 对齐
（行 "\n" 分隔、列 "\t" 分隔），见 test_slate_json.py 里同款断言。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.html_text import html_to_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _weex_article_body_with_table() -> str:
    payload = json.loads((FIXTURES / "weex_EN.json").read_text(encoding="utf-8"))
    article = next(a for a in payload["articles"] if a["id"] == 57773585831833)
    return article["body"]


def _bitunix_first_article_body() -> str:
    payload = json.loads((FIXTURES / "bitunix_EN.json").read_text(encoding="utf-8"))
    return payload["articles"][0]["body"]


# ---------------------------------------------------------------- 正常解析（真实 fixture） ----

def test_html_to_text_extracts_plain_text_from_real_bitunix_fixture():
    text = html_to_text(_bitunix_first_article_body())
    assert "Dear Bitunix Users," in text
    assert "Bitunix will launch SKHYUSDT in USDT-M Perpetual Futures" in text
    assert "<div>" not in text
    assert "<strong>" not in text


def test_html_to_text_preserves_table_structure_from_real_weex_fixture():
    text = html_to_text(_weex_article_body_with_table())
    assert "WXT commitment amount\tReward multiplier" in text
    assert "300 ≤ X < 3,000\t1" in text


# ---------------------------------------------------------------- 表格（手写样本） ----

def test_html_to_text_table_rows_newline_separated_cells_tab_separated():
    html = (
        "<table><tr><td>Header A</td><td>Header B</td></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    assert html_to_text(html) == "Header A\tHeader B\n1\t2"


def test_html_to_text_table_between_paragraphs_stays_a_contiguous_block():
    html = "<p>before</p><table><tr><td>a</td><td>b</td></tr></table><p>after</p>"
    assert html_to_text(html) == "before\na\tb\nafter"


# ---------------------------------------------------------------- 模板内容剔除 ----

def test_html_to_text_strips_nav_header_footer_script_style():
    html = (
        "<nav>Home | About</nav>"
        "<header>Site Header</header>"
        "<script>var x = 1;</script>"
        "<style>.a{color:red}</style>"
        "<p>real content</p>"
        "<footer>Copyright 2026</footer>"
    )
    assert html_to_text(html) == "real content"


def test_html_to_text_strips_elements_with_noise_class_or_id():
    html = (
        '<div class="site-footer">footer noise</div>'
        '<div id="cookie-banner">accept cookies</div>'
        "<div>keep me</div>"
    )
    assert html_to_text(html) == "keep me"


# ---------------------------------------------------------------- 空白压缩 ----

def test_html_to_text_collapses_internal_whitespace_and_strips_line_ends():
    html = "<div>  lots   of   \n\n  whitespace  </div><div>next</div>"
    assert html_to_text(html) == "lots of whitespace\nnext"


def test_html_to_text_paragraphs_separated_by_single_newline_not_blank_lines():
    html = "<p>one</p><p>two</p><p>three</p>"
    assert html_to_text(html) == "one\ntwo\nthree"


def test_html_to_text_br_produces_line_break():
    html = "<p>line one<br>line two</p>"
    assert html_to_text(html) == "line one\nline two"


# ---------------------------------------------------------------- 畸形 HTML 不崩 ----

def test_html_to_text_unclosed_tags_do_not_raise():
    html = "<div>unterminated <b>bold text"
    assert html_to_text(html) == "unterminated bold text"


def test_html_to_text_stray_angle_brackets_do_not_raise():
    html = "<<<>>>malformed<div>ok</div>"
    assert "ok" in html_to_text(html)


def test_html_to_text_none_or_empty_returns_empty_string():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_html_to_text_plain_text_with_no_tags_passes_through():
    assert html_to_text("just plain text") == "just plain text"
