"""`BaseCollector.run(lookback_days=...)` 的通用行为测试（跟具体交易所无关）。

背景：Bitunix（watermark 策略）在 crawl_state 为空时 since=None，早停条件永远不触发，
翻页翻到底——等价于全量历史回填；Weex/BingX/Phemex/Lbank（full_scan 策略）完全不做
日期过滤，只靠固定页数窗口圈内容，窗口对应的时间跨度可能横跨很久。`lookback_days`
是这两个问题的统一修复，用两个最小 fake collector（不依赖任何真实交易所的 parser/
HTTP 逻辑）覆盖 base.py 里新增的播种/过滤/force_full 豁免/默认行为不变四种场景。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.db.connection import get_connection, init_db

OLD_TIME = "2020-01-01T00:00:00Z"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeWatermarkCollector(BaseCollector):
    source_name = "FakeWatermark"

    def __init__(self, locale: str, config: dict, items: Optional[list[RawItem]] = None):
        super().__init__(locale, config)
        self._items = items or []
        self.received_since: Optional[str] = "UNSET"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        self.received_since = since
        return list(self._items)

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=str(item.article_id),
            title=item.title,
            content=item.content or "body",
            post_time=item.post_time,
            update_time=item.update_time,
            group_id=f"fake_{item.article_id}",
        )


class _FakeFullScanCollector(BaseCollector):
    source_name = "FakeFullScan"

    def __init__(self, locale: str, config: dict, items: Optional[list[RawItem]] = None):
        super().__init__(locale, config)
        self._items = items or []

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        return list(self._items)

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=str(item.article_id),
            title=item.title,
            content=item.content or "body",
            post_time=item.post_time,
            update_time=item.update_time,
            group_id=f"fake_{item.article_id}",
        )


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    with get_connection(db_path) as connection:
        yield connection


def test_watermark_seeds_since_from_lookback_when_crawl_state_empty(conn):
    collector = _FakeWatermarkCollector("EN", {"strategy": "watermark"}, items=[])

    collector.run(conn, lookback_days=1)

    assert collector.received_since is not None
    # 播种的 cutoff 应该是"最近 1 天"量级，不是 None（不然又会退化成全量回填）。
    cutoff_dt = datetime.strptime(collector.received_since, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert (datetime.now(timezone.utc) - cutoff_dt) < timedelta(days=1, minutes=5)


def test_watermark_since_stays_none_without_lookback_days(conn):
    """回归安全网：不传 lookback_days（默认 None）时行为跟修改前逐字节一致。"""
    collector = _FakeWatermarkCollector("EN", {"strategy": "watermark"}, items=[])

    collector.run(conn)

    assert collector.received_since is None


def test_full_scan_filters_old_items_by_lookback(conn):
    items = [
        RawItem(article_id="old-1", title="old", post_time=OLD_TIME),
        RawItem(article_id="new-1", title="new", post_time=_now_iso()),
    ]
    collector = _FakeFullScanCollector("EN", {"strategy": "full_scan"}, items=items)

    stats = collector.run(conn, lookback_days=1)

    assert stats.skipped_by_date == 1
    assert stats.new == 1
    assert stats.total == 1


def test_full_scan_collection_date_keeps_only_that_utc_day(conn):
    items = [
        RawItem(article_id="before", title="before", post_time="2026-07-20T23:59:59Z"),
        RawItem(article_id="today", title="today", post_time="2026-07-21T12:00:00Z"),
        RawItem(article_id="after", title="after", post_time="2026-07-22T00:00:00Z"),
        RawItem(article_id="undated", title="undated", post_time=None),
    ]
    collector = _FakeFullScanCollector("EN", {"strategy": "full_scan"}, items=items)

    stats = collector.run(conn, collection_date="2026-07-21")

    assert stats.new == 1
    assert stats.skipped_by_date == 3
    assert conn.execute("SELECT article_id FROM announcements").fetchone()[0] == "today"


def test_watermark_collection_date_seeds_exact_utc_day_start(conn):
    collector = _FakeWatermarkCollector("EN", {"strategy": "watermark"}, items=[])

    collector.run(conn, collection_date="2026-07-21")

    assert collector.received_since == "2026-07-21T00:00:00Z"


def test_full_scan_keeps_all_items_without_lookback_days(conn):
    """回归安全网：不传 lookback_days 时，旧条目照常处理，不做任何日期过滤。"""
    items = [RawItem(article_id="old-1", title="old", post_time=OLD_TIME)]
    collector = _FakeFullScanCollector("EN", {"strategy": "full_scan"}, items=items)

    stats = collector.run(conn)

    assert stats.skipped_by_date == 0
    assert stats.new == 1


def test_force_full_ignores_lookback_days_for_full_scan(conn):
    items = [RawItem(article_id="old-1", title="old", post_time=OLD_TIME)]
    collector = _FakeFullScanCollector("EN", {"strategy": "full_scan"}, items=items)

    stats = collector.run(conn, force_full=True, lookback_days=1)

    assert stats.skipped_by_date == 0
    assert stats.new == 1


def test_force_full_ignores_lookback_days_for_watermark(conn):
    collector = _FakeWatermarkCollector("EN", {"strategy": "watermark"}, items=[])

    collector.run(conn, force_full=True, lookback_days=1)

    assert collector.received_since is None
