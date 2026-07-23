#!/usr/bin/env python3
"""Schema v4 -> v5：统一公告时间语义，并为活动增加独立起止时间。"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timezone


ACTIVITY_CATEGORIES = ("campaign_center", "rewards", "activity_center", "new_popular_events")
ISO_RE = re.compile(r"活动周期:\s*([^\s]+)\s*~\s*([^\s]+)")


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(announcements)")}
        if "activity_start_time" not in columns:
            conn.execute("ALTER TABLE announcements ADD COLUMN activity_start_time TEXT")
        if "activity_end_time" not in columns:
            conn.execute("ALTER TABLE announcements ADD COLUMN activity_end_time TEXT")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS collector_item_state (
                   uid TEXT PRIMARY KEY REFERENCES announcements (uid) ON DELETE CASCADE,
                   source_version TEXT,
                   observed_at TEXT
               )"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO collector_item_state (uid, source_version, observed_at)
               SELECT uid, update_time, fetched_at FROM announcements
               WHERE source = 'Zoomex' AND update_time IS NOT NULL"""
        )

        placeholders = ",".join("?" for _ in ACTIVITY_CATEGORIES)
        rows = conn.execute(
            f"""SELECT uid, post_time, content FROM announcements
                WHERE raw_category IN ({placeholders})
                  AND activity_start_time IS NULL""",
            ACTIVITY_CATEGORIES,
        ).fetchall()
        for row in rows:
            start = row["post_time"]
            end = None
            match = ISO_RE.search(row["content"] or "")
            if match:
                start = None if match.group(1) == "?" else match.group(1)
                end = None if match.group(2) == "?" else match.group(2)
            conn.execute(
                """UPDATE announcements
                   SET activity_start_time = COALESCE(activity_start_time, ?),
                       activity_end_time = COALESCE(activity_end_time, ?),
                       post_time = NULL
                   WHERE uid = ?""",
                (start, end, row["uid"]),
            )

        # 历史通用字段只补空值，不覆盖已有事实：
        # - fetched_at 缺失时依次用已有更新时间、发布时间、活动开始时间近似；
        # - post_time 缺失时用首次抓取时间近似，确保旧消费者仍能得到发布日期；
        # - update_time 允许为空，未来仅在 content_hash 发生变化时写入。
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """UPDATE announcements
               SET fetched_at = COALESCE(fetched_at, update_time, post_time,
                                         activity_start_time, ?)""",
            (now,),
        )
        conn.execute(
            "UPDATE announcements SET post_time = fetched_at WHERE post_time IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db_path")
    args = parser.parse_args()
    migrate(args.db_path)


if __name__ == "__main__":
    main()
