from __future__ import annotations

import sqlite3

from scripts.migrate_v5 import migrate


def test_migrate_v5_adds_activity_fields_and_preserves_source_cursor(tmp_path):
    db = tmp_path / "v4.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE announcements (
               uid TEXT PRIMARY KEY, source TEXT, raw_category TEXT, content TEXT,
               post_time TEXT, update_time TEXT, fetched_at TEXT
           );
           INSERT INTO announcements VALUES (
               'event-1', 'Lbank', 'new_popular_events',
               '活动周期: 2026-07-22T15:35:00Z ~ 2026-07-30T00:00:00Z',
               '2026-07-22T15:35:00Z', NULL, '2026-07-23T01:30:00Z'
           );
           INSERT INTO announcements VALUES (
               'zmx-1', 'Zoomex', 'platform_events', 'body',
               '2026-07-20T00:00:00Z', '2026-07-21T00:00:00Z', '2026-07-22T00:00:00Z'
           );"""
    )
    conn.commit()
    conn.close()

    migrate(str(db))
    migrate(str(db))  # 幂等

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    event = conn.execute("SELECT * FROM announcements WHERE uid='event-1'").fetchone()
    # 源端没有独立发布日期时，用首次抓取时间作为近似值。
    assert event["post_time"] == "2026-07-23T01:30:00Z"
    assert event["activity_start_time"] == "2026-07-22T15:35:00Z"
    assert event["activity_end_time"] == "2026-07-30T00:00:00Z"
    assert event["update_time"] is None
    zmx = conn.execute("SELECT * FROM announcements WHERE uid='zmx-1'").fetchone()
    assert zmx["fetched_at"] == "2026-07-22T00:00:00Z"
    assert zmx["update_time"] == "2026-07-21T00:00:00Z"
    cursor = conn.execute(
        "SELECT source_version FROM collector_item_state WHERE uid='zmx-1'"
    ).fetchone()
    assert cursor["source_version"] == "2026-07-21T00:00:00Z"
    conn.close()
