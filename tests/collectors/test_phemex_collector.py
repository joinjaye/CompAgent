"""PhemexCollector 测试（2026-07-14 分页升级后，见 src/collectors/phemex.py
顶部注释 + CLAUDE.md「Phemex 分页升级」）。mock HTTP 层，不发真实请求；列表
解析用真实抓取的 JSON fixture（tests/fixtures/phemex_api_query_*.json），详情
解析仍用 Phase 1 真实抓取的 HTML fixture（tests/fixtures/phemex_*_detail.html，
详情页抓取路径未受这次改动影响）。

覆盖：
- fetch_list()：真分页（pageNo 递增直到 max_pages 或空页停止），url 拼接绝对
  地址
- force_full 不再是 no-op：忽略 max_pages，翻到空页为止
- fetch_detail()：提取正文 + updated_at（仅记录，不参与增量判断），路径不变
- normalize()：raw_category 用采集时的 categories.* key（news/activities/
  newsletter），不是响应里的 category.name（Phase 2.6 订正的设计）
- run() 端到端幂等：full_scan 策略不写 crawl_state，靠 content_hash 判断
  unchanged
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.phemex import PhemexCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "list_endpoint": "https://prod-cms-api.phemex.com/articles/query",
    "language": "en",
    "method": "GET",
    "headers": {},
    "pagination": {"type": "page_number", "page_size": 5, "max_pages": 2},
    "rate_limit_ms": 0,
    "detail_mode": "separate_api",
    "has_update_time": True,
    "strategy": "full_scan",
}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _query_fixture() -> dict:
    return json.loads((FIXTURES / "phemex_api_query_news_en.json").read_text(encoding="utf-8"))


def _empty_page() -> dict:
    return {"data": {"rows": [], "total": 5}}


# ---------------------------------------------------------------- fetch_list ----

def test_fetch_list_maps_fields_and_builds_absolute_url(monkeypatch):
    fixture = _query_fixture()

    def fake_fetch_json(url, **kw):
        return fixture if "pageNo=1" in url else _empty_page()

    monkeypatch.setattr("src.collectors.phemex.fetch_json", fake_fetch_json)

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    items = collector.fetch_list(since=None)

    assert len(items) == 5
    item = items[0]
    assert item.article_id
    assert item.title
    assert item.url.startswith("https://phemex.com/announcements/")
    assert item.post_time.endswith("Z")


def test_fetch_list_paginates_until_max_pages(monkeypatch):
    fixture = _query_fixture()
    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        return fixture  # 每页都非空，验证受 max_pages=2 限制

    monkeypatch.setattr("src.collectors.phemex.fetch_json", fake_fetch_json)

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    items = collector.fetch_list(since=None)

    assert len(calls) == 2  # CFG.pagination.max_pages = 2
    assert len(items) == 10  # 每页 5 条 × 2 页


def test_fetch_list_force_full_ignores_max_pages(monkeypatch):
    fixture = _query_fixture()
    calls = []

    def fake_fetch_json(url, **kw):
        calls.append(url)
        page_no = int(url.rsplit("pageNo=", 1)[1].split("&")[0])
        return _empty_page() if page_no >= 4 else fixture

    monkeypatch.setattr("src.collectors.phemex.fetch_json", fake_fetch_json)

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    collector.force_full = True
    items = collector.fetch_list(since=None)

    assert len(calls) == 4  # 忽略 max_pages=2，翻到空页（第4页）为止
    assert len(items) == 15  # 前 3 页各 5 条


def test_fetch_list_sends_category_id_and_language(monkeypatch):
    captured = {}

    def fake_fetch_json(url, **kw):
        captured["url"] = url
        return _query_fixture() if "pageNo=1" in url else _empty_page()

    monkeypatch.setattr("src.collectors.phemex.fetch_json", fake_fetch_json)

    collector = PhemexCollector("FR", {**CFG, "language": "fr", "list_category_id": 432}, "news")
    collector.fetch_list(since=None)

    assert "categoryKey=AnnouncementCategory432" in captured["url"]
    assert "language=fr" in captured["url"]


def test_fetch_list_returns_empty_on_malformed_response(monkeypatch):
    monkeypatch.setattr("src.collectors.phemex.fetch_json", lambda url, **kw: {})

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    assert collector.fetch_list(since=None) == []


# -------------------------------------------------------------- fetch_detail ----

def test_fetch_detail_extracts_content_and_updated_at(monkeypatch):
    detail_html = (FIXTURES / "phemex_EN_detail.html").read_text(encoding="utf-8")
    monkeypatch.setattr("src.collectors.phemex.http_fetch", lambda url, **kw: detail_html)

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    raw = RawItem(article_id=135329, title="old", url="https://phemex.com/announcements/x")
    result = collector.fetch_detail(raw)

    assert result.content is not None
    assert "<p" in result.content
    assert result.update_time is not None


def test_fetch_detail_no_url_skips_gracefully():
    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    raw = RawItem(article_id=1, url=None)
    result = collector.fetch_detail(raw)

    assert result.content is None


def test_fetch_detail_handles_parse_failure_gracefully(monkeypatch):
    monkeypatch.setattr("src.collectors.phemex.http_fetch", lambda url, **kw: "<html>garbage</html>")

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    raw = RawItem(article_id=1, title="old title", url="https://phemex.com/announcements/x")
    result = collector.fetch_detail(raw)

    assert result.content is None
    assert result.title == "old title"


# ---------------------------------------------------------------- normalize ----

def test_normalize_uses_category_key_not_response_field():
    collector = PhemexCollector("EN", {**CFG, "list_category_id": 442}, "activities")
    raw = RawItem(
        article_id=1, title="t", content="<p>hello</p>",
        post_time="2026-07-13T09:25:01Z", url="https://phemex.com/announcements/x",
    )

    ann = collector.normalize(raw)

    assert ann.source == "Phemex"
    assert ann.group_id == "phemex_1"
    assert ann.raw_category == "activities"
    assert ann.content == "hello"


def test_normalize_handles_missing_content():
    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")
    raw = RawItem(article_id=1, title="t", content=None)

    ann = collector.normalize(raw)

    assert ann.content == ""


# ---------------------------------------------------------------- run() 端到端 ----

def test_run_is_idempotent_via_content_hash(db_path, monkeypatch):
    query_fixture = _query_fixture()
    detail_html = (FIXTURES / "phemex_EN_detail.html").read_text(encoding="utf-8")

    def fake_fetch_json(url, **kw):
        return query_fixture if "pageNo=1" in url else _empty_page()

    monkeypatch.setattr("src.collectors.phemex.fetch_json", fake_fetch_json)
    monkeypatch.setattr("src.collectors.phemex.http_fetch", lambda url, **kw: detail_html)

    collector = PhemexCollector("EN", {**CFG, "list_category_id": 432}, "news")

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.failed == 0
    assert first.total == 5

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM crawl_state WHERE source='Phemex'").fetchone()
        assert row["c"] == 0  # full_scan 不写水位线

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 5
