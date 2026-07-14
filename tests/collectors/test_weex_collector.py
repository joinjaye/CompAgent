"""WeexCollector 测试（2026-07-14 起改为解析 www.weex.com 前台页面，见
src/collectors/weex.py 顶部注释 + CLAUDE.md「Weex 数据源迁移」）。mock HTTP 层，
不发真实请求；详情页解析用真实抓取的 fixture（tests/fixtures/weex_web_article_
detail_slug.html），列表页用手工构造的最小合法 flight 流片段（构造方式见
_make_list_html，已用真实响应交叉验证过格式）。

覆盖：
- fetch_list()：字段映射（post_time 从 ms 转 ISO、raw_category=section_id）、
  robots.txt disallowed article_id 跳过、totalPage 到頭就停止翻页
- daily 增量受 pagination.max_pages 限制，--force-full（force_full=True）忽略
  这个上限翻到 totalPage
- normalize()：group_id 拼接、source_endpoint、update_time 恒 None（没有
  per-item 更新时间字段，见 sources.yaml 注释）
- fetch_detail()：从详情页 HTML 提取 zendesk-html div 正文
- run() 端到端幂等：full_scan 策略不写 crawl_state，靠 content_hash 判断
  unchanged（不是 watermark 挡住）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collectors.weex import DISALLOWED_ARTICLE_IDS, WeexCollector
from src.db.connection import get_connection, init_db
from src.db.operations import compute_uid

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "locale_path": "en",
    "endpoint": "https://www.weex.com/en/help/categories/18540264809497",
    "pagination": {"type": "query_page", "page_size": 65, "max_pages": 2},
    "rate_limit_ms": 0,
    "detail_mode": "separate_page",
    "has_update_time": False,
    "strategy": "full_scan",
    "headers": {},
}


def _make_list_html(items: list[dict], page: int, total_count: int, total_page: int) -> str:
    payload_text = (
        '"articleListData":' + json.dumps(items)
        + f',"pageInfo":{{"page":{page},"prePage":{page},"pageSize":65}}'
        + f',"totalCount":{total_count},"totalPage":{total_page}'
    )
    escaped = json.dumps(payload_text)[1:-1]
    return f'<html><script>self.__next_f.push([1,"{escaped}"])</script></html>'


def _article(article_id: str, title: str, created_ms: int, section_id: int, url: str) -> dict:
    return {
        "id": article_id,
        "documentId": article_id,
        "name": title,
        "createdAt": str(created_ms),
        "sectionId": section_id,
        "prioritise": False,
        "url": url,
    }


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


# ---------------------------------------------------------------- fetch_list ----

def test_fetch_list_maps_fields_from_single_page(monkeypatch):
    html = _make_list_html(
        [_article("abc123", "Test Article", 1700000000000, 111, "/help/articles/abc123")],
        page=1, total_count=1, total_page=1,
    )
    monkeypatch.setattr("src.collectors.weex.http_fetch", lambda url, **kw: html)

    collector = WeexCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 1
    item = items[0]
    assert item.article_id == "abc123"
    assert item.title == "Test Article"
    assert item.post_time == "2023-11-14T22:13:20Z"  # 1700000000000ms 转 ISO
    assert item.category_raw == 111
    assert item.url == "https://www.weex.com/en/help/articles/abc123"


def test_fetch_list_skips_robots_disallowed_article_ids(monkeypatch):
    disallowed_id = next(iter(DISALLOWED_ARTICLE_IDS))
    html = _make_list_html(
        [
            _article(disallowed_id, "Disallowed", 1700000000000, 111, f"/help/articles/{disallowed_id}"),
            _article("allowed1", "Allowed", 1700000000000, 111, "/help/articles/allowed1"),
        ],
        page=1, total_count=2, total_page=1,
    )
    monkeypatch.setattr("src.collectors.weex.http_fetch", lambda url, **kw: html)

    collector = WeexCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert [i.article_id for i in items] == ["allowed1"]


def test_fetch_list_stops_when_reaching_total_page(monkeypatch):
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        page = int(url.rsplit("page=", 1)[1])
        return _make_list_html(
            [_article(f"a{page}", f"Article {page}", 1700000000000, 111, f"/help/articles/a{page}")],
            page=page, total_count=2, total_page=2,
        )

    monkeypatch.setattr("src.collectors.weex.http_fetch", fake_fetch)

    collector = WeexCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(calls) == 2  # totalPage=2，翻到第 2 页应该停止，不会有第 3 次请求
    assert [i.article_id for i in items] == ["a1", "a2"]


def test_fetch_list_daily_increment_respects_max_pages(monkeypatch):
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        page = int(url.rsplit("page=", 1)[1])
        # totalPage 故意设得很大（10），验证 max_pages=2（daily 默认）会提前停，
        # 而不是翻到 totalPage 为止
        return _make_list_html(
            [_article(f"a{page}", f"Article {page}", 1700000000000, 111, f"/help/articles/a{page}")],
            page=page, total_count=100, total_page=10,
        )

    monkeypatch.setattr("src.collectors.weex.http_fetch", fake_fetch)

    collector = WeexCollector("EN", CFG)  # CFG.pagination.max_pages = 2
    items = collector.fetch_list(since=None)

    assert len(calls) == 2
    assert [i.article_id for i in items] == ["a1", "a2"]


def test_fetch_list_force_full_ignores_max_pages(monkeypatch):
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        page = int(url.rsplit("page=", 1)[1])
        return _make_list_html(
            [_article(f"a{page}", f"Article {page}", 1700000000000, 111, f"/help/articles/a{page}")],
            page=page, total_count=4, total_page=4,
        )

    monkeypatch.setattr("src.collectors.weex.http_fetch", fake_fetch)

    collector = WeexCollector("EN", CFG)
    collector.force_full = True  # run() 会设置这个；这里直接测 fetch_list 行为，手动模拟
    items = collector.fetch_list(since=None)

    assert len(calls) == 4  # 忽略 max_pages=2，翻到 totalPage=4 为止
    assert [i.article_id for i in items] == ["a1", "a2", "a3", "a4"]


def test_fetch_list_stops_on_empty_page(monkeypatch):
    def fake_fetch(url, **kw):
        return _make_list_html([], page=1, total_count=0, total_page=1)

    monkeypatch.setattr("src.collectors.weex.http_fetch", fake_fetch)

    collector = WeexCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert items == []


# ---------------------------------------------------------------- normalize ----

def test_normalize_builds_group_id_and_leaves_update_time_none():
    from src.collectors.base import RawItem

    collector = WeexCollector("EN", CFG)
    raw = RawItem(
        article_id="abc123", title="t", content="<p>hello</p>",
        post_time="2026-07-12T09:30:00Z", category_raw=33143373088665,
        url="https://www.weex.com/en/help/articles/abc123",
    )

    ann = collector.normalize(raw)

    assert ann.source == "Weex"
    assert ann.group_id == "weex_abc123"
    assert ann.raw_category == "33143373088665"
    assert ann.update_time is None
    assert ann.content == "hello"
    assert ann.source_endpoint == CFG["endpoint"]


def test_normalize_handles_missing_content():
    from src.collectors.base import RawItem

    collector = WeexCollector("EN", CFG)
    raw = RawItem(article_id="abc123", title="t", content=None)

    ann = collector.normalize(raw)

    assert ann.content == ""


# ---------------------------------------------------------------- fetch_detail ----

def test_fetch_detail_extracts_body_from_real_fixture(monkeypatch):
    detail_html = (FIXTURES / "weex_web_article_detail_slug.html").read_text(encoding="utf-8")
    monkeypatch.setattr("src.collectors.weex.http_fetch", lambda url, **kw: detail_html)

    from src.collectors.base import RawItem

    collector = WeexCollector("EN", CFG)
    raw = RawItem(article_id="l3v3t1vvcpzqq2vza2y0upyb", url="https://www.weex.com/en/help/articles/l3v3t1vvcpzqq2vza2y0upyb")
    result = collector.fetch_detail(raw)

    assert result.content is not None
    assert "edgeX" in result.content


def test_fetch_detail_no_url_skips_gracefully():
    from src.collectors.base import RawItem

    collector = WeexCollector("EN", CFG)
    raw = RawItem(article_id="x", url=None)
    result = collector.fetch_detail(raw)

    assert result.content is None


# ---------------------------------------------------------------- run() 端到端幂等 ----

def test_run_is_idempotent_via_content_hash_not_watermark(db_path, monkeypatch):
    """full_scan 策略不写 crawl_state.high_watermark，第二轮之所以 unchanged 是因为
    content_hash 没变，不是因为水位线挡住了重新拉取——两轮都应该真正发起相同次数的
    列表/详情请求。"""
    detail_html = (FIXTURES / "weex_web_article_detail_slug.html").read_text(encoding="utf-8")
    list_html = _make_list_html(
        [_article("l3v3t1vvcpzqq2vza2y0upyb", "edgeX article", 1700000000000, 33143373088665,
                   "/help/articles/l3v3t1vvcpzqq2vza2y0upyb")],
        page=1, total_count=1, total_page=1,
    )

    def fake_fetch(url, **kw):
        return detail_html if "articles/l3v3t1vvcpzqq2vza2y0upyb" in url else list_html

    monkeypatch.setattr("src.collectors.weex.http_fetch", fake_fetch)

    collector = WeexCollector("EN", CFG)

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.new == 1
    assert first.failed == 0

    with get_connection(db_path) as conn:
        # full_scan 不写水位线：crawl_state 里不应该有 Weex 的行
        row = conn.execute("SELECT COUNT(*) c FROM crawl_state WHERE source='Weex'").fetchone()
        assert row["c"] == 0

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 1

    uid = compute_uid("Weex", "EN", "l3v3t1vvcpzqq2vza2y0upyb")
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT raw_category, content FROM announcements WHERE uid=?", (uid,)).fetchone()
    assert row["raw_category"] == "33143373088665"
    assert "edgeX" in row["content"]
