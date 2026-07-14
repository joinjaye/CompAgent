"""LbankCollector 测试（2026-07-14 重写，走真实 JSON API，见
src/collectors/lbank.py 顶部注释 + CLAUDE.md「Lbank 真实 API 重写」）。mock
HTTP 层，不发真实请求；列表/详情解析用真实抓取的 fixture
（tests/fixtures/lbank_api_*.json）。

覆盖：
- fetch_list()：真分页（pageNo 递增直到 max_pages 或空页停止）、字段映射、
  content 直接来自列表接口（不依赖详情接口）
- force_full 不再是 no-op：忽略 max_pages，翻到空页为止
- fetch_detail()：只补 update_time/category_raw（columnId），不覆盖
  content/title
- normalize()：raw_category 优先用详情接口 columnId，详情失败时兜底退回
  categoryCode；group_id 用 noticeId
- run() 端到端幂等：full_scan 策略不写 crawl_state，靠 content_hash 判断
  unchanged
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.lbank import LbankCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://www.lbank.com/lbk-api/huamao-media-center/notice/latestList",
    "detail_endpoint": "https://www.lbank.com/lbk-api/huamao-media-center/notice/content/{code}?noticeCode={code}",
    "lang_header": "en-US",
    "locale_path": "",
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
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


def _list_fixture() -> dict:
    return json.loads((FIXTURES / "lbank_api_latestlist_new_listings_en.json").read_text(encoding="utf-8"))


def _detail_fixture() -> dict:
    return json.loads((FIXTURES / "lbank_api_detail_en.json").read_text(encoding="utf-8"))


def _empty_page() -> dict:
    return {"data": {"page": {"pageSize": 5, "pageNo": 2}, "resultList": [], "totalCount": 5, "totalPage": 1}}


# ---------------------------------------------------------------- fetch_list ----

def test_fetch_list_maps_fields_from_real_fixture(monkeypatch):
    fixture = _list_fixture()

    # 第 1 页给真实数据，第 2 页给空页，验证遇到空页停止翻页
    def fake_fetch(url, **kw):
        body = json.loads(kw["body"])
        return fixture if body["pageNo"] == 1 else _empty_page()

    monkeypatch.setattr("src.collectors.lbank.fetch_json", fake_fetch)

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    items = collector.fetch_list(since=None)

    assert len(items) == 5
    item = items[0]
    assert item.article_id
    assert item.title
    assert item.content  # 直接来自列表接口
    assert item.post_time.endswith("Z")
    assert item.extra["code"]


def test_fetch_list_paginates_until_max_pages(monkeypatch):
    fixture = _list_fixture()
    calls = []

    def fake_fetch(url, **kw):
        body = json.loads(kw["body"])
        calls.append(body["pageNo"])
        return fixture  # 每页都返回非空，验证受 max_pages=2 限制而不是无限翻页

    monkeypatch.setattr("src.collectors.lbank.fetch_json", fake_fetch)

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    items = collector.fetch_list(since=None)

    assert calls == [1, 2]  # CFG.pagination.max_pages = 2
    assert len(items) == 10  # 每页 5 条 × 2 页


def test_fetch_list_force_full_ignores_max_pages(monkeypatch):
    fixture = _list_fixture()
    calls = []

    def fake_fetch(url, **kw):
        body = json.loads(kw["body"])
        calls.append(body["pageNo"])
        if body["pageNo"] >= 4:
            return _empty_page()
        return fixture

    monkeypatch.setattr("src.collectors.lbank.fetch_json", fake_fetch)

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    collector.force_full = True
    items = collector.fetch_list(since=None)

    assert calls == [1, 2, 3, 4]  # 忽略 max_pages=2，翻到空页（第4页）为止
    assert len(items) == 15  # 前 3 页各 5 条，第 4 页空


def test_fetch_list_sends_category_code_and_lang_header(monkeypatch):
    fixture = _list_fixture()
    captured = {}

    def fake_fetch(url, **kw):
        captured["url"] = url
        captured["headers"] = kw["headers"]
        captured["body"] = json.loads(kw["body"])
        return fixture if captured["body"]["pageNo"] == 1 else _empty_page()

    monkeypatch.setattr("src.collectors.lbank.fetch_json", fake_fetch)

    collector = LbankCollector("VN", {**CFG, "lang_header": "vi-VN"}, "new_listings", "CO00000053")
    collector.fetch_list(since=None)

    assert captured["headers"]["ex-language"] == "vi-VN"
    assert captured["body"]["categoryCode"] == "CO00000053"
    assert captured["body"]["topCategory"] == "NOTICE"


def test_fetch_list_returns_empty_on_malformed_response(monkeypatch):
    monkeypatch.setattr("src.collectors.lbank.fetch_json", lambda url, **kw: {})

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    assert collector.fetch_list(since=None) == []


# -------------------------------------------------------------- fetch_detail ----

def test_fetch_detail_only_patches_update_time_and_category(monkeypatch):
    detail_payload = _detail_fixture()
    monkeypatch.setattr("src.collectors.lbank.fetch_json", lambda url, **kw: detail_payload)

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    raw = RawItem(
        article_id=17020, title="original title", content="original content",
        post_time="2026-01-01T00:00:00Z", extra={"code": "2077027077202116608"},
    )
    result = collector.fetch_detail(raw)

    assert result.title == "original title"  # 不被详情接口覆盖
    assert result.content == "original content"  # 不被详情接口覆盖（详情接口 content 是 URL，不用）
    assert result.update_time is not None
    assert result.category_raw is not None


def test_fetch_detail_handles_parse_failure_gracefully(monkeypatch):
    monkeypatch.setattr("src.collectors.lbank.fetch_json", lambda url, **kw: {})

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    raw = RawItem(article_id=1, title="old", content="old content", extra={"code": "x"})
    result = collector.fetch_detail(raw)

    assert result.title == "old"
    assert result.content == "old content"
    assert result.update_time is None


# ---------------------------------------------------------------- normalize ----

def test_normalize_uses_detail_column_id_as_raw_category():
    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    raw = RawItem(
        article_id=17020, title="t", content="hello",
        post_time="2026-07-14T13:20:00Z", update_time="2026-07-14T13:20:00Z",
        category_raw=54, extra={"code": "2077027077202116608"},
    )

    ann = collector.normalize(raw)

    assert ann.source == "Lbank"
    assert ann.group_id == "lbank_17020"
    assert ann.raw_category == "54"
    assert ann.url == "https://www.lbank.com/support/articles/2077027077202116608"
    assert ann.content == "hello"


def test_normalize_falls_back_to_category_code_when_detail_failed():
    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    raw = RawItem(
        article_id=17020, title="t", content="hello", extra={"code": "abc"}, category_raw=None,
    )

    ann = collector.normalize(raw)

    assert ann.raw_category == "CO00000053"


def test_normalize_uses_locale_path_prefix_for_non_en():
    collector = LbankCollector("VN", {**CFG, "locale_path": "vi-VN/"}, "new_listings", "CO00000053")
    raw = RawItem(article_id=1, title="t", content="x", extra={"code": "abc"})

    ann = collector.normalize(raw)

    assert ann.url == "https://www.lbank.com/vi-VN/support/articles/abc"


def test_normalize_handles_missing_content():
    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")
    raw = RawItem(article_id=1, title="t", content=None, extra={})

    ann = collector.normalize(raw)

    assert ann.content == ""


# ---------------------------------------------------------------- run() 端到端 ----

def test_run_is_idempotent_via_content_hash(db_path, monkeypatch):
    list_fixture = _list_fixture()
    detail_fixture = _detail_fixture()

    def fake_fetch(url, **kw):
        if "detail" in url or "content" in url:
            return detail_fixture
        body = json.loads(kw["body"])
        return list_fixture if body["pageNo"] == 1 else _empty_page()

    monkeypatch.setattr("src.collectors.lbank.fetch_json", fake_fetch)

    collector = LbankCollector("EN", CFG, "new_listings", "CO00000053")

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.failed == 0
    assert first.total == 5

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM crawl_state WHERE source='Lbank'").fetchone()
        assert row["c"] == 0  # full_scan 不写水位线

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 5
