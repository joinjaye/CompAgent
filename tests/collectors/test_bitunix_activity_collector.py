"""BitunixActivityCollector 测试。mock HTTP 层，不发真实请求；列表解析用真实
抓取的 fixture（tests/fixtures/bitunix_activity_EN.html）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.bitunix_activity import BitunixActivityCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://www.bitunix.com/activity/act-center",
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
    return (FIXTURES / "bitunix_activity_EN.html").read_text(encoding="utf-8")


def test_fetch_list_maps_fields_from_real_fixture(monkeypatch):
    monkeypatch.setattr("src.collectors.bitunix_activity.http_fetch", lambda url: _fixture_html())

    collector = BitunixActivityCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 2
    item = next(i for i in items if i.article_id == "actcenter-6223")
    assert item.title == "Bitcoin Pizza Day Giveaway!"
    assert "活动周期" in item.content
    assert item.post_time == "2026-05-22T10:00:00Z"
    assert item.url == "https://www.bitunix.com/activity/basic/pizza-day-2026"


def test_fetch_list_returns_empty_on_garbage_html(monkeypatch):
    monkeypatch.setattr("src.collectors.bitunix_activity.http_fetch", lambda url: "<html></html>")

    collector = BitunixActivityCollector("EN", CFG)
    assert collector.fetch_list(since=None) == []


def test_normalize_sets_literal_raw_category_and_prefixed_group_id():
    collector = BitunixActivityCollector("EN", CFG)
    raw = RawItem(
        article_id="actcenter-6223", title="t", content="<p>hello</p>",
        post_time="2026-05-22T10:00:00Z", url="https://www.bitunix.com/activity/basic/pizza-day-2026",
    )

    ann = collector.normalize(raw)

    assert ann.source == "Bitunix"
    assert ann.raw_category == "campaign_center"
    assert ann.group_id == "bitunix_actcenter-6223"
    assert ann.content == "hello"  # html_to_text 已剥掉标签
    assert ann.update_time is None


def test_normalize_handles_missing_content():
    collector = BitunixActivityCollector("EN", CFG)
    raw = RawItem(article_id="actcenter-1", title="t", content=None)

    ann = collector.normalize(raw)

    assert ann.content == ""


def test_run_is_idempotent_via_content_hash(db_path, monkeypatch):
    monkeypatch.setattr("src.collectors.bitunix_activity.http_fetch", lambda url: _fixture_html())

    collector = BitunixActivityCollector("EN", CFG)

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.failed == 0
    assert first.total == 2

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM crawl_state WHERE source='Bitunix'").fetchone()
        assert row["c"] == 0  # full_scan 不写水位线

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 2


def test_run_does_not_collide_with_regular_bitunix_article_id(db_path, monkeypatch):
    # article_id 前缀防误撞：手动插入一条跟活动中心 id 数值相同、但来自常规
    # Zendesk 公告流的行，验证两者是完全独立的两行，不会被 upsert_announcement
    # 误判成同一条内容。
    monkeypatch.setattr("src.collectors.bitunix_activity.http_fetch", lambda url: _fixture_html())

    from src.db.operations import upsert_announcement

    collector = BitunixActivityCollector("EN", CFG)
    with get_connection(db_path) as conn:
        upsert_announcement(
            conn, source="Bitunix", locale="EN", article_id="6223",
            title="不相关的常规公告", content="不相关内容", post_time="2020-01-01T00:00:00Z",
        )
        stats = collector.run(conn)
        assert stats.new == 2  # 两条活动都被当作新行插入，没有被现有的 "6223" 行拦下

        rows = conn.execute(
            "SELECT article_id FROM announcements WHERE source='Bitunix' ORDER BY article_id"
        ).fetchall()
        article_ids = {r["article_id"] for r in rows}
        assert "6223" in article_ids
        assert "actcenter-6223" in article_ids
