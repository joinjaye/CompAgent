"""BingXCollector 测试。mock HTTP 层，不发真实请求；列表/详情解析用 Phase 1
真实抓取的 fixture（tests/fixtures/bingx_EN*.html）。

覆盖：
- fetch_list()：从真实 __NUXT_DATA__ 页面解析出首屏 20 条，字段映射（时间转
  UTC ISO、raw_category=sectionId）
- force_full 对 BingX 是 no-op（首屏窗口本身不是分页接口，见 src/collectors/
  bingx.py 顶部注释）
- fetch_detail()：从详情页提取正文 + 用详情页的 sectionId 覆盖列表页的值；
  解析失败时优雅降级（正文置空，不抛异常）
- normalize()：group_id/url 拼接
- run() 端到端幂等：full_scan 策略不写 crawl_state，靠 content_hash 判断
  unchanged
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.bingx import BingXCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://bingx.com/en/support/notice-center",
    "detail_endpoint": "https://bingx.com/en/support/articles/{article_id}",
    "method": "GET",
    "headers": {},
    "pagination": {"type": "none"},
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


# ---------------------------------------------------------------- fetch_list ----

def test_fetch_list_maps_fields_from_real_fixture(monkeypatch):
    html = (FIXTURES / "bingx_EN.html").read_text(encoding="utf-8")
    monkeypatch.setattr("src.collectors.bingx.http_fetch", lambda url, **kw: html)

    collector = BingXCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 20
    item = items[0]
    assert item.article_id
    assert item.title
    assert item.post_time.endswith("Z")
    assert item.update_time.endswith("Z")
    assert item.category_raw is not None


def test_fetch_list_force_full_is_noop(monkeypatch):
    calls = []
    html = (FIXTURES / "bingx_EN.html").read_text(encoding="utf-8")

    def fake_fetch(url, **kw):
        calls.append(url)
        return html

    monkeypatch.setattr("src.collectors.bingx.http_fetch", fake_fetch)

    collector = BingXCollector("EN", CFG)
    collector.force_full = True
    items_full = collector.fetch_list(since=None)
    collector.force_full = False
    items_normal = collector.fetch_list(since=None)

    assert len(calls) == 2
    assert [i.article_id for i in items_full] == [i.article_id for i in items_normal]


def test_fetch_list_returns_empty_on_garbage_html(monkeypatch):
    monkeypatch.setattr("src.collectors.bingx.http_fetch", lambda url, **kw: "<html>garbage</html>")

    collector = BingXCollector("EN", CFG)
    assert collector.fetch_list(since=None) == []


# -------------------------------------------------------------- fetch_detail ----

def test_fetch_detail_extracts_body_and_overrides_section(monkeypatch):
    detail_html = (FIXTURES / "bingx_EN_detail.html").read_text(encoding="utf-8")
    monkeypatch.setattr("src.collectors.bingx.http_fetch", lambda url, **kw: detail_html)

    collector = BingXCollector("EN", CFG)
    raw = RawItem(article_id=16835949205007, title="old title", category_raw=1)
    result = collector.fetch_detail(raw)

    assert result.content is not None
    assert "<div" in result.content
    assert result.title != "old title"
    assert result.category_raw != 1  # 被详情页真实 sectionId 覆盖


def test_fetch_detail_handles_parse_failure_gracefully(monkeypatch):
    monkeypatch.setattr("src.collectors.bingx.http_fetch", lambda url, **kw: "<html>garbage</html>")

    collector = BingXCollector("EN", CFG)
    raw = RawItem(article_id=1, title="old title")
    result = collector.fetch_detail(raw)

    assert result.content is None
    assert result.title == "old title"


# ---------------------------------------------------------------- normalize ----

def test_normalize_builds_group_id_and_url_and_cleans_html():
    collector = BingXCollector("EN", CFG)
    raw = RawItem(
        article_id=123, title="t", content="<div>hello</div>",
        post_time="2026-07-14T09:48:29Z", update_time="2026-07-14T09:48:29Z",
        category_raw=11257015822991,
    )

    ann = collector.normalize(raw)

    assert ann.source == "BingX"
    assert ann.group_id == "bingx_123"
    assert ann.raw_category == "11257015822991"
    assert ann.url == "https://bingx.com/en/support/articles/123"
    assert ann.content == "hello"


def test_normalize_handles_missing_content():
    collector = BingXCollector("EN", CFG)
    raw = RawItem(article_id=123, title="t", content=None)

    ann = collector.normalize(raw)

    assert ann.content == ""


# ---------------------------------------------------------------- run() 端到端 ----

def test_run_is_idempotent_via_content_hash(db_path, monkeypatch):
    list_html = (FIXTURES / "bingx_EN.html").read_text(encoding="utf-8")
    detail_html = (FIXTURES / "bingx_EN_detail.html").read_text(encoding="utf-8")

    def fake_fetch(url, **kw):
        return detail_html if "articles/" in url else list_html

    monkeypatch.setattr("src.collectors.bingx.http_fetch", fake_fetch)

    collector = BingXCollector("EN", CFG)

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.failed == 0
    assert first.total == 20

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM crawl_state WHERE source='BingX'").fetchone()
        assert row["c"] == 0  # full_scan 不写水位线

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 20
