"""src/parsers/bingx_web.py 离线单测：用 Phase 1 侦察阶段真实抓取的页面快照
（tests/fixtures/bingx_EN*.html/bingx_VN*.html）验证 __NUXT_DATA__ devalue 解析，
不发任何网络请求。
"""

from __future__ import annotations

from pathlib import Path

from src.parsers.bingx_web import parse_article_detail, parse_article_list

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_article_list_returns_first_screen_items():
    html = _read_fixture("bingx_EN.html")
    items = parse_article_list(html)
    assert len(items) == 20
    first = items[0]
    assert first["article_id"]
    assert first["title"]
    assert first["create_time"]
    assert first["update_time"]
    assert first["section_id"] is not None


def test_parse_article_list_create_equals_update_time_matches_recon():
    # Phase 1 已确认 createTime/updateTime 抽样恒等（watermark 不可靠的依据），
    # 用真实 fixture 复核一次这个观察仍然成立。
    html = _read_fixture("bingx_EN.html")
    items = parse_article_list(html)
    assert all(i["create_time"] == i["update_time"] for i in items)


def test_parse_article_list_vn_locale_also_works():
    html = _read_fixture("bingx_VN.html")
    items = parse_article_list(html)
    assert len(items) > 0


def test_parse_article_list_returns_empty_on_garbage_html():
    assert parse_article_list("<html><body>nothing here</body></html>") == []


def test_parse_article_detail_extracts_body_and_section():
    html = _read_fixture("bingx_EN_detail.html")
    detail = parse_article_detail(html)
    assert detail is not None
    assert detail["title"]
    assert "<div" in detail["body"]
    assert detail["section_id"] is not None
    assert detail["create_time"]


def test_parse_article_detail_none_on_garbage_html():
    assert parse_article_detail("<html><body>nothing here</body></html>") is None
