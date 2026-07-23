"""BingXEventsCollector 测试。mock `_capture_activity_pages`（真实实现会启动
headless Chromium，单测不依赖真实浏览器），用真实抓取的 API 响应 fixture
（tests/fixtures/bingx_events_api_EN.json）验证字段映射/去重/分页现状告警逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.bingx_events import BingXEventsCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://bingx.com/en/events",
    "pagination": {"type": "page_number", "page_size": 10, "max_pages": 2},
    "rate_limit_ms": 0,
    "strategy": "full_scan",
}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _page_body() -> dict:
    return json.loads((FIXTURES / "bingx_events_api_EN.json").read_text(encoding="utf-8"))


def test_fetch_list_maps_fields_from_real_fixture(monkeypatch):
    monkeypatch.setattr(
        "src.collectors.bingx_events._capture_activity_pages", lambda url, max_pages: [_page_body()]
    )

    collector = BingXEventsCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 10  # fixture 里 page=1 的 10 条
    world_cup = next(i for i in items if i.article_id == 138)
    assert world_cup.title == "World Cup Gold Rush: Predict & win a share in $10M"
    assert "Airdrop" in world_cup.content
    assert world_cup.post_time is None
    assert world_cup.extra["start_time_raw"] == "2026-06-09T10:00:00.000+08:00"


def test_fetch_list_dedups_across_pages(monkeypatch):
    # 两页都返回同一份 fixture（模拟"没找到真实翻页触发方式，重复拿到同一页"的
    # 已知现状），验证按 activityId 去重，不会把同一条活动重复插入。
    monkeypatch.setattr(
        "src.collectors.bingx_events._capture_activity_pages",
        lambda url, max_pages: [_page_body(), _page_body()],
    )

    collector = BingXEventsCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 10  # 去重后仍是 10 条，不是 20 条


def test_fetch_list_returns_empty_when_no_pages_captured(monkeypatch):
    monkeypatch.setattr("src.collectors.bingx_events._capture_activity_pages", lambda url, max_pages: [])

    collector = BingXEventsCollector("EN", CFG)
    assert collector.fetch_list(since=None) == []


def test_normalize_sets_literal_raw_category_and_prefixed_ids():
    collector = BingXEventsCollector("EN", CFG)
    raw = RawItem(
        article_id=138, title="World Cup Gold Rush", content="Airdrop, High Rewards",
        post_time="2026-06-09T02:00:00Z", url="https://bingx.com/activity/worldcup2026",
        extra={"end_time_raw": "2026-07-21T00:00:00.000+08:00"},
    )

    ann = collector.normalize(raw)

    assert ann.source == "BingX"
    assert ann.article_id == "promocenter-138"
    assert ann.raw_category == "activity_center"
    assert ann.group_id == "bingx_promocenter-138"
    assert "活动周期" in ann.content
    assert ann.update_time is None


def test_run_does_not_collide_with_regular_bingx_article_id(db_path, monkeypatch):
    monkeypatch.setattr(
        "src.collectors.bingx_events._capture_activity_pages", lambda url, max_pages: [_page_body()]
    )

    from src.db.operations import upsert_announcement

    collector = BingXEventsCollector("EN", CFG)
    with get_connection(db_path) as conn:
        upsert_announcement(
            conn, source="BingX", locale="EN", article_id="138",
            title="不相关的常规公告", content="不相关内容", post_time="2020-01-01T00:00:00Z",
        )
        stats = collector.run(conn)
        assert stats.new == 10

        rows = conn.execute(
            "SELECT article_id FROM announcements WHERE source='BingX' ORDER BY article_id"
        ).fetchall()
        article_ids = {r["article_id"] for r in rows}
        assert "138" in article_ids
        assert "promocenter-138" in article_ids
