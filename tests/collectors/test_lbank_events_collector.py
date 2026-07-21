"""LbankEventsCollector 测试。mock HTTP 层，不发真实请求；列表/详情解析用真实
抓取的 fixture（tests/fixtures/lbank_events_EN.html、
lbank_events_loadingpage_EN.json、lbank_events_rule_content_EN.stxt）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.lbank_events import LbankEventsCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://www.lbank.com/new-popular-events",
    "detail_endpoint": "https://www.lbank.com/lbk-api/huli-bazaar-center/atlasActivity/loadingPage",
    "lang_header": "en-US",
    "pagination": {"type": "none"},
    "rate_limit_ms": 0,
    "strategy": "full_scan",
}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _list_html() -> str:
    return (FIXTURES / "lbank_events_EN.html").read_text(encoding="utf-8")


def _loadingpage_payload() -> dict:
    return json.loads((FIXTURES / "lbank_events_loadingpage_EN.json").read_text(encoding="utf-8"))


def _rule_content() -> str:
    return (FIXTURES / "lbank_events_rule_content_EN.stxt").read_text(encoding="utf-8")


def test_fetch_list_maps_fields_and_sends_lang_header(monkeypatch):
    captured = {}

    def fake_fetch(url, **kw):
        captured["url"] = url
        captured["headers"] = kw.get("headers")
        return _list_html()

    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", fake_fetch)

    collector = LbankEventsCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 8
    assert captured["headers"]["ex-language"] == "en-US"
    item = next(i for i in items if i.article_id == "event-10002315")
    assert item.title == "TENDIES, BRIAN Listing Carnival"
    assert item.post_time == "2026-07-17T12:00:00Z"
    assert item.content == "Share $10,000 Rewards"  # 只是列表页 subtitle，规则正文由 fetch_detail 追加
    assert item.extra["code"] == "10002315-tendies-brian-listing"


def test_fetch_list_returns_empty_on_garbage_html(monkeypatch):
    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", lambda url, **kw: "<html></html>")

    collector = LbankEventsCollector("EN", CFG)
    assert collector.fetch_list(since=None) == []


def test_fetch_detail_appends_rule_content_via_two_hops(monkeypatch):
    captured = {}

    def fake_fetch_json(url, **kw):
        captured["detail_url"] = url
        captured["body"] = json.loads(kw["body"])
        captured["headers"] = kw["headers"]
        return _loadingpage_payload()

    def fake_http_fetch(url, **kw):
        captured["content_url"] = url
        return _rule_content()

    monkeypatch.setattr("src.collectors.lbank_events.fetch_json", fake_fetch_json)
    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", fake_http_fetch)

    collector = LbankEventsCollector("EN", CFG)
    raw = RawItem(
        article_id="event-10002315", title="t", content="Share $10,000 Rewards",
        extra={"code": "10002315-tendies-brian-listing"},
    )
    result = collector.fetch_detail(raw)

    assert captured["detail_url"] == CFG["detail_endpoint"]
    assert captured["body"] == {"activityCode": "10002315-tendies-brian-listing"}
    assert captured["headers"]["ex-language"] == "en-US"
    assert captured["content_url"] == "https://www.lbank.com/static-backend-doc/content/260717/9La_0717195148141.stxt"
    assert "Share $10,000 Rewards" in result.content
    assert "Eligibility" in result.content or "🌟" in result.content  # 真实规则正文


def test_fetch_detail_no_code_returns_item_unchanged():
    collector = LbankEventsCollector("EN", CFG)
    raw = RawItem(article_id="event-1", title="t", content="teaser", extra={})

    result = collector.fetch_detail(raw)

    assert result.content == "teaser"


def test_fetch_detail_loadingpage_failure_falls_back_to_teaser(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("network error")

    monkeypatch.setattr("src.collectors.lbank_events.fetch_json", boom)

    collector = LbankEventsCollector("EN", CFG)
    raw = RawItem(article_id="event-1", title="t", content="teaser", extra={"code": "x"})

    result = collector.fetch_detail(raw)

    assert result.content == "teaser"


def test_normalize_sets_literal_raw_category_and_prefixed_group_id():
    collector = LbankEventsCollector("EN", CFG)
    raw = RawItem(
        article_id="event-10002315", title="t", content="hello", post_time="2026-07-17T12:00:00Z",
        extra={"end_time_ms": 1784548800000},
    )

    ann = collector.normalize(raw)

    assert ann.source == "Lbank"
    assert ann.raw_category == "new_popular_events"
    assert ann.group_id == "lbank_event-10002315"
    assert ann.update_time is None
    assert "活动周期" in ann.content


def test_run_is_idempotent_via_content_hash(db_path, monkeypatch):
    def fake_fetch(url, **kw):
        return _list_html()

    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", fake_fetch)
    monkeypatch.setattr("src.collectors.lbank_events.fetch_json", lambda url, **kw: _loadingpage_payload())

    collector = LbankEventsCollector("EN", CFG)

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.failed == 0
    assert first.total == 8

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.unchanged == 8


def test_run_does_not_collide_with_regular_lbank_article_id(db_path, monkeypatch):
    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", lambda url, **kw: _list_html())
    monkeypatch.setattr("src.collectors.lbank_events.fetch_json", lambda url, **kw: _loadingpage_payload())

    from src.db.operations import upsert_announcement

    collector = LbankEventsCollector("EN", CFG)
    with get_connection(db_path) as conn:
        upsert_announcement(
            conn, source="Lbank", locale="EN", article_id="10002315",
            title="不相关的常规公告", content="不相关内容", post_time="2020-01-01T00:00:00Z",
        )
        stats = collector.run(conn)
        assert stats.new == 8

        rows = conn.execute(
            "SELECT article_id FROM announcements WHERE source='Lbank' ORDER BY article_id"
        ).fetchall()
        article_ids = {r["article_id"] for r in rows}
        assert "10002315" in article_ids
        assert "event-10002315" in article_ids
