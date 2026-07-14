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


def test_html_to_text_table_cell_wrapped_in_nested_p_tag_is_not_dropped():
    """Phase 2.7 回归测试：真实 Weex 上币公告（article_id=56648741969433）的表格
    单元格用 <td><p>text</p></td> 包裹（不是 <td>text</td> 直接文本节点）。
    最初的实现里，<p> 作为块级标签会触发顶层 _flush()，把单元格文字提前推到
    self.blocks，导致 _end_cell() 拿到空 buffer——整行变成一串空 tab
    （"\t\t\t\t"），单元格内容反而以独立段落的形式出现在表格之外。"""
    html = (
        "<table><tr>"
        "<td><p>Trading pair</p></td>"
        "<td><p>Launch time</p></td>"
        "</tr><tr>"
        "<td><p>ADIUSDT</p></td>"
        "<td><p>Apr 4, 2026</p></td>"
        "</tr></table>"
    )
    assert html_to_text(html) == "Trading pair\tLaunch time\nADIUSDT\tApr 4, 2026"


def test_html_to_text_genuinely_empty_cells_still_produce_tabs():
    """空单元格（源站本来就没填内容，如中奖名单待公布的占位表格）应该原样保留成
    空字符串占位，不是本次修的 bug——区别于"内容被 <p> 误吞掉"的场景。"""
    html = "<table><tr><td>Rank</td><td> </td><td>500 USDT</td></tr></table>"
    assert html_to_text(html) == "Rank\t\t500 USDT"


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
