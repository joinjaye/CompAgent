"""src/analysis/zmx_index.py 单测：category/locale 过滤、近 90 天窗口、空基线处理、
TF-IDF 检索的相关性排序。全部离线，临时 SQLite 库，不发任何网络请求。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src.analysis.zmx_index import build_index

REFERENCE_DATE = datetime(2026, 7, 14, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE announcements (
            uid TEXT PRIMARY KEY, source TEXT, locale TEXT, category TEXT,
            title TEXT, content TEXT, post_time TEXT
        )
        """
    )
    yield c
    c.close()


def _insert(conn, uid, source, locale, category, title, content, post_time):
    conn.execute(
        "INSERT INTO announcements (uid, source, locale, category, title, content, post_time) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, source, locale, category, title, content, post_time),
    )


def test_build_index_filters_by_category_and_locale(conn):
    _insert(conn, "z1", "Zoomex", "EN", "campaign", "Trading Contest", "Join our trading contest for USDT rewards", "2026-06-01T00:00:00Z")
    _insert(conn, "z2", "Zoomex", "FR", "campaign", "Concours de trading", "Rejoignez notre concours", "2026-06-01T00:00:00Z")
    _insert(conn, "z3", "Zoomex", "EN", "product", "New API", "We launched a new API", "2026-06-01T00:00:00Z")
    conn.commit()

    index = build_index(conn, category="campaign", locale="EN", reference_date=REFERENCE_DATE)
    assert len(index) == 1


def test_build_index_excludes_older_than_lookback_window(conn):
    _insert(conn, "z1", "Zoomex", "EN", "campaign", "Recent Contest", "trading contest rewards", "2026-06-01T00:00:00Z")
    _insert(conn, "z2", "Zoomex", "EN", "campaign", "Old Contest", "trading contest rewards", "2025-01-01T00:00:00Z")
    conn.commit()

    index = build_index(conn, category="campaign", locale="EN", lookback_days=90, reference_date=REFERENCE_DATE)
    assert len(index) == 1


def test_build_index_skips_empty_content(conn):
    _insert(conn, "z1", "Zoomex", "EN", "campaign", "Empty", "", "2026-06-01T00:00:00Z")
    _insert(conn, "z2", "Zoomex", "EN", "campaign", "Null", None, "2026-06-01T00:00:00Z")
    conn.commit()

    index = build_index(conn, category="campaign", locale="EN", reference_date=REFERENCE_DATE)
    assert len(index) == 0


def test_search_returns_empty_list_on_empty_index(conn):
    index = build_index(conn, category="campaign", locale="EN", reference_date=REFERENCE_DATE)
    assert index.search("some query", top_k=5) == []


def test_search_ranks_by_relevance(conn):
    _insert(conn, "z1", "Zoomex", "EN", "campaign", "USDT Trading Contest",
            "Join our USDT trading contest with big rewards for futures trading volume", "2026-06-01T00:00:00Z")
    _insert(conn, "z2", "Zoomex", "EN", "campaign", "Deposit Bonus",
            "Deposit bonus for new users, get free tokens on signup", "2026-06-02T00:00:00Z")
    _insert(conn, "z3", "Zoomex", "EN", "campaign", "Futures Trading Competition",
            "Futures trading competition ranked by trading volume, USDT prize pool", "2026-06-03T00:00:00Z")
    conn.commit()

    index = build_index(conn, category="campaign", locale="EN", reference_date=REFERENCE_DATE)
    results = index.search("Trading Competition USDT futures trading volume contest", top_k=5)

    assert len(results) >= 2
    uids = [r.uid for r in results]
    assert uids[0] in ("z1", "z3")
    assert "z2" not in uids[:1]  # 不相关的存款活动不应该排在最前


def test_search_top_k_limits_results(conn):
    for i in range(10):
        _insert(conn, f"z{i}", "Zoomex", "EN", "campaign", f"Contest {i}", "trading contest reward", "2026-06-01T00:00:00Z")
    conn.commit()

    index = build_index(conn, category="campaign", locale="EN", reference_date=REFERENCE_DATE)
    results = index.search("trading contest reward", top_k=3)
    assert len(results) == 3


def test_content_preview_is_truncated(conn):
    long_content = "trading contest " * 200
    _insert(conn, "z1", "Zoomex", "EN", "campaign", "Contest", long_content, "2026-06-01T00:00:00Z")
    conn.commit()

    index = build_index(conn, category="campaign", locale="EN", preview_chars=50, reference_date=REFERENCE_DATE)
    results = index.search("trading contest", top_k=5)
    assert len(results[0].content_preview) == 50
