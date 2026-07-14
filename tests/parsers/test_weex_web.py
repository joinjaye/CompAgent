"""src/parsers/weex_web.py 离线单测：用真实抓取的页面快照
（tests/fixtures/weex_web_*.html，2026-07-14 抓取，见 CLAUDE.md「Weex 数据源迁移」）
验证列表/详情解析，不发任何网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.html_text import html_to_text
from src.parsers.weex_web import (
    extract_article_body_html,
    parse_article_list,
    parse_page_info,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_article_list_category_page():
    html = _read_fixture("weex_web_category_list_page1.html")
    items = parse_article_list(html)
    assert len(items) == 65
    first = items[0]
    assert first["article_id"]
    assert first["title"]
    assert isinstance(first["post_time_ms"], (int, str))
    assert first["section_id"] is not None
    assert "prioritise" in first


def test_parse_article_list_section_page():
    html = _read_fixture("weex_web_section_list_page1.html")
    items = parse_article_list(html)
    assert len(items) > 0
    for item in items:
        assert item["article_id"]


def test_parse_article_list_known_article_present():
    html = _read_fixture("weex_web_category_list_page1.html")
    items = parse_article_list(html)
    ids = {item["article_id"] for item in items}
    assert "l3v3t1vvcpzqq2vza2y0upyb" in ids
    edge = next(item for item in items if item["article_id"] == "l3v3t1vvcpzqq2vza2y0upyb")
    assert int(edge["post_time_ms"]) == 1783848600000


def test_parse_article_list_returns_empty_on_garbage_html():
    assert parse_article_list("<html><body>nothing here</body></html>") == []


def test_parse_article_list_uses_id_not_document_id_for_legacy_duplicate_records():
    """回归测试：2026-07-14 真实采集撞见同一篇旧文章（同一个数字 id、同一个 url）
    在列表里出现两条不同 documentId 的记录（likeId 相邻但不同，疑似 Weex 自己 CMS
    迁移历史文章时留下的重复 document 记录）。如果拿 documentId 当 article_id 会把
    同一篇文章的正文重复插成两行；用 id 就能让两条记录落到同一个 article_id，交给
    upsert_announcement 的去重逻辑自然合并。"""
    entry = {
        "id": 54056485866137, "name": "A", "createdAt": "1767863213000",
        "url": "/help/articles/54056485866137", "sectionId": 111, "prioritise": False,
    }
    items_list = [
        {**entry, "documentId": "d9ni7j7fn8k2nb9dzk4zpi69"},
        {**entry, "documentId": "cd65xr4bh3djdce304e8fenp"},
    ]
    payload_text = '"articleListData":' + json.dumps(items_list)
    escaped = json.dumps(payload_text)[1:-1]
    html = f'<html><script>self.__next_f.push([1,"{escaped}"])</script></html>'
    items = parse_article_list(html)
    assert len(items) == 2
    assert items[0]["article_id"] == items[1]["article_id"] == "54056485866137"


def test_parse_article_list_decodes_non_ascii_titles_correctly():
    """回归测试：早期实现用 encode('utf-8').decode('unicode_escape') 处理拼接后的
    flight 流文本，把已经正确解码的多字节 UTF-8 字符（如法语重音字符）二次损坏成
    mojibake（如 "spéciale" 被解析成 "spÃ©ciale"），2026-07-14 真实抓取法语 P2P
    公告标题时发现，见 CLAUDE.md「Weex 数据源迁移」。"""
    html = _read_fixture("weex_web_section_list_fr.html")
    items = parse_article_list(html)
    titles = {item["title"] for item in items}
    assert "Offre spéciale WEEX P2P : des réductions pour tous !" in titles
    assert not any("Ã" in t for t in titles)


def test_parse_page_info():
    html = _read_fixture("weex_web_category_list_page1.html")
    info = parse_page_info(html)
    assert info is not None
    total_count, total_page = info
    assert total_count > 0
    assert total_page > 0


def test_parse_page_info_none_on_garbage_html():
    assert parse_page_info("<html></html>") is None


def test_extract_article_body_html_slug_id():
    html = _read_fixture("weex_web_article_detail_slug.html")
    body_html = extract_article_body_html(html)
    assert body_html is not None
    assert "edgeX" in body_html
    text = html_to_text(body_html)
    assert "edgeX (EDGE) landed on WE-Launch" in text
    assert "<div" not in text and "<p>" not in text


def test_extract_article_body_html_numeric_id():
    html = _read_fixture("weex_web_article_detail_numeric.html")
    body_html = extract_article_body_html(html)
    assert body_html is not None
    text = html_to_text(body_html)
    assert "SUNFI" in text


def test_extract_article_body_html_none_when_div_missing():
    assert extract_article_body_html("<html><body><div class=\"other\">x</div></body></html>") is None


def test_extract_article_body_html_handles_nested_divs_correctly():
    html = (
        '<div class="zendesk-html wrap">'
        '<div class="inner"><p>text</p></div>'
        '<figure class="image"><img src="x.png"></figure>'
        "</div>"
        '<div class="unrelated-footer">should not be captured</div>'
    )
    body_html = extract_article_body_html(html)
    assert body_html is not None
    assert "should not be captured" not in body_html
    assert "<p>text</p>" in body_html
