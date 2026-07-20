"""LbankEventsCollector 测试。mock HTTP 层，不发真实请求；列表解析用真实抓取的
fixture（tests/fixtures/lbank_events_EN.html）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.lbank_events import LbankEventsCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://www.lbank.com/new-popular-events",
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


def _fixture_html() -> str:
    return (FIXTURES / "lbank_events_EN.html").read_text(encoding="utf-8")


def test_fetch_list_maps_fields_and_sends_lang_header(monkeypatch):
    captured = {}

    def fake_fetch(url, **kw):
        captured["url"] = url
        captured["headers"] = kw.get("headers")
        return _fixture_html()

    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", fake_fetch)

    collector = LbankEventsCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 8
    assert captured["headers"]["ex-language"] == "en-US"
    item = next(i for i in items if i.article_id == "event-10002315")
    assert item.title == "TENDIES, BRIAN Listing Carnival"
    assert item.post_time == "2026-07-17T12:00:00Z"
    assert "活动周期" in item.content


def test_fetch_list_returns_empty_on_garbage_html(monkeypatch):
    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", lambda url, **kw: "<html></html>")

    collector = LbankEventsCollector("EN", CFG)
    assert collector.fetch_list(since=None) == []


def test_normalize_sets_literal_raw_category_and_prefixed_group_id():
    collector = LbankEventsCollector("EN", CFG)
    raw = RawItem(article_id="event-10002315", title="t", content="hello", post_time="2026-07-13T04:00:00Z")

    ann = collector.normalize(raw)

    assert ann.source == "Lbank"
    assert ann.raw_category == "new_popular_events"
    assert ann.group_id == "lbank_event-10002315"
    assert ann.update_time is None


def test_run_is_idempotent_via_content_hash(db_path, monkeypatch):
    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", lambda url, **kw: _fixture_html())

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
    monkeypatch.setattr("src.collectors.lbank_events.http_fetch", lambda url, **kw: _fixture_html())

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
