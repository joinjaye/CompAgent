"""announcements / content_history / crawl_state 的读写操作。

这一层是后续 collectors（Phase 2）落库时唯一应该调用的接口：
调用方只管传入抓到的原始字段，去重、变更检测、历史归档的规则统一在这里实现，
避免每个 collector 各自实现一遍容易出现不一致的判断逻辑。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow_iso() -> str:
    """当前 UTC 时间，ISO8601（秒精度，Z 结尾）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_uid(source: str, locale: str, article_id: str) -> str:
    """uid = SHA256(f"{source}_{locale}_{article_id}")。"""
    raw = f"{source}_{locale}_{article_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_content_hash(content: Optional[str]) -> str:
    """content_hash = SHA256(content)。空内容也返回稳定 hash。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


@dataclass
class UpsertResult:
    uid: str
    status: str  # new / changed / unchanged


def upsert_announcement(
    conn: sqlite3.Connection,
    *,
    source: str,
    locale: str,
    article_id: str,
    url: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    post_time: Optional[str] = None,
    update_time: Optional[str] = None,
    fetched_at: Optional[str] = None,
    category: Optional[str] = None,
    is_region_exclusive: bool = False,
    source_endpoint: Optional[str] = None,
    group_id: Optional[str] = None,
) -> UpsertResult:
    """插入或更新一条公告，返回落库后的 status。

    判断逻辑：
    - uid 不存在            → INSERT，status=new
    - uid 存在且 hash 变了   → 旧版本写入 content_history，UPDATE 主表，status=changed，
                                push_status 重置为 pending（新内容需要重新走一遍推送判断）
    - uid 存在且 hash 相同   → 只更新 fetched_at，status=unchanged
    """
    uid = compute_uid(source, locale, article_id)
    content_hash = compute_content_hash(content)
    fetched_at = fetched_at or utcnow_iso()

    row = conn.execute(
        "SELECT content_hash, fetched_at FROM announcements WHERE uid = ?",
        (uid,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO announcements (
                uid, group_id, source, locale, article_id, url, title, content,
                content_hash, post_time, update_time, fetched_at, status,
                category, is_region_exclusive, push_status, source_endpoint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, 'pending', ?)
            """,
            (
                uid, group_id, source, locale, article_id, url, title, content,
                content_hash, post_time, update_time, fetched_at,
                category, bool(is_region_exclusive), source_endpoint,
            ),
        )
        return UpsertResult(uid=uid, status="new")

    if row["content_hash"] != content_hash:
        conn.execute(
            """
            INSERT INTO content_history (uid, content_hash, content, captured_at)
            SELECT uid, content_hash, content, ?
            FROM announcements WHERE uid = ?
            """,
            (row["fetched_at"], uid),
        )
        conn.execute(
            """
            UPDATE announcements
            SET url = ?, title = ?, content = ?, content_hash = ?,
                post_time = ?, update_time = ?, fetched_at = ?, status = 'changed',
                push_status = 'pending', source_endpoint = ?
            WHERE uid = ?
            """,
            (url, title, content, content_hash, post_time, update_time, fetched_at, source_endpoint, uid),
        )
        return UpsertResult(uid=uid, status="changed")

    conn.execute(
        "UPDATE announcements SET fetched_at = ?, status = 'unchanged' WHERE uid = ?",
        (fetched_at, uid),
    )
    return UpsertResult(uid=uid, status="unchanged")


def get_announcement(conn: sqlite3.Connection, uid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM announcements WHERE uid = ?", (uid,)).fetchone()


def get_content_history(conn: sqlite3.Connection, uid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM content_history WHERE uid = ? ORDER BY id", (uid,)
    ).fetchall()


def get_crawl_state(
    conn: sqlite3.Connection, source: str, locale: str, category: str = ""
) -> Optional[sqlite3.Row]:
    """category 留空代表单分类源（Bitunix/Weex 等），多分类源（如 Zoomex 的各 menu_id）
    各自传入独立的 category 字符串，互不覆盖水位线。"""
    return conn.execute(
        "SELECT * FROM crawl_state WHERE source = ? AND locale = ? AND category = ?",
        (source, locale, category),
    ).fetchone()


def set_crawl_state(
    conn: sqlite3.Connection,
    *,
    source: str,
    locale: str,
    high_watermark: Optional[str],
    strategy: str = "watermark",
    updated_at: Optional[str] = None,
    category: str = "",
) -> None:
    """写入/更新某个 source×locale×category 的水位线（upsert on PRIMARY KEY）。"""
    updated_at = updated_at or utcnow_iso()
    conn.execute(
        """
        INSERT INTO crawl_state (source, locale, category, high_watermark, strategy, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, locale, category) DO UPDATE SET
            high_watermark = excluded.high_watermark,
            strategy = excluded.strategy,
            updated_at = excluded.updated_at
        """,
        (source, locale, category, high_watermark, strategy, updated_at),
    )
