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
    activity_start_time: Optional[str] = None,
    activity_end_time: Optional[str] = None,
    category: Optional[str] = None,
    raw_category: Optional[str] = None,
    is_region_exclusive: bool = False,
    source_endpoint: Optional[str] = None,
    group_id: Optional[str] = None,
) -> UpsertResult:
    """插入或更新一条公告，返回落库后的 status。

    判断逻辑：
    - uid 不存在            → INSERT，status=new
    - uid 存在且 hash 变了   → 旧版本写入 content_history，UPDATE 主表，status=changed，
                                push_status 重置为 pending（新内容需要重新走一遍推送判断）
    - uid 存在且 hash 相同   → 不改三个通用时间字段与 status，只补充非内容元数据

    统一时间语义：fetched_at=首次抓取；update_time=本系统检测到内容变化的时间；
    post_time=源端内容发布时间。调用方传入的源端 update_time 不参与存储，避免不同
    平台把“源端编辑时间/发布流程时间/活动时间”混进同一字段。
    """
    uid = compute_uid(source, locale, article_id)
    content_hash = compute_content_hash(content)
    fetched_at = fetched_at or utcnow_iso()
    # 首次入库不是“内容发生更新”。新内容的 update_time 保持为空，只有后续
    # content_hash 真正变化时才写入检测到变化的时间。
    content_updated_at = fetched_at

    row = conn.execute(
        "SELECT content_hash, fetched_at, update_time, raw_category FROM announcements WHERE uid = ?",
        (uid,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO announcements (
                uid, group_id, source, locale, article_id, url, title, content,
                raw_category, content_hash, post_time, update_time, fetched_at,
                activity_start_time, activity_end_time, status,
                category, is_region_exclusive, push_status, source_endpoint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, 'pending', ?)
            """,
            (
                uid, group_id, source, locale, article_id, url, title, content,
                raw_category, content_hash, post_time, None, fetched_at,
                activity_start_time, activity_end_time,
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
            (row["update_time"] or row["fetched_at"], uid),
        )
        conn.execute(
            """
            UPDATE announcements
            SET url = ?, title = ?, content = ?, raw_category = ?, content_hash = ?,
                post_time = ?, update_time = ?, activity_start_time = ?, activity_end_time = ?,
                status = 'changed',
                push_status = 'pending', source_endpoint = ?
            WHERE uid = ?
            """,
            (url, title, content, raw_category, content_hash, post_time, content_updated_at,
             activity_start_time, activity_end_time, source_endpoint, uid),
        )
        return UpsertResult(uid=uid, status="changed")

    if row["raw_category"] != raw_category:
        # 正文没变但源端分类归属变了（如 Zendesk 后台把文章挪到另一个 section）。
        # 这不是内容变更，不进 content_history、不动 status/push_status，只补正
        # raw_category，否则会一直停在第一次抓到的旧分类上，见 CLAUDE.md「Phase 2.6」。
        conn.execute(
            """UPDATE announcements SET raw_category = ?,
               post_time = COALESCE(?, post_time),
               activity_start_time = COALESCE(?, activity_start_time),
               activity_end_time = COALESCE(?, activity_end_time)
               WHERE uid = ?""",
            (raw_category, post_time, activity_start_time, activity_end_time, uid),
        )
        return UpsertResult(uid=uid, status="unchanged")

    conn.execute(
        """UPDATE announcements SET post_time = COALESCE(?, post_time),
           activity_start_time = COALESCE(?, activity_start_time),
           activity_end_time = COALESCE(?, activity_end_time)
           WHERE uid = ?""",
        (post_time, activity_start_time, activity_end_time, uid),
    )
    return UpsertResult(uid=uid, status="unchanged")


def get_announcement(conn: sqlite3.Connection, uid: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM announcements WHERE uid = ?", (uid,)).fetchone()


def get_content_history(conn: sqlite3.Connection, uid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM content_history WHERE uid = ? ORDER BY id", (uid,)
    ).fetchall()


def get_collector_source_version(conn: sqlite3.Connection, uid: str) -> Optional[str]:
    row = conn.execute(
        "SELECT source_version FROM collector_item_state WHERE uid = ?", (uid,)
    ).fetchone()
    return row["source_version"] if row else None


def set_collector_source_version(
    conn: sqlite3.Connection, uid: str, source_version: Optional[str], observed_at: Optional[str] = None,
) -> None:
    if source_version is None:
        return
    conn.execute(
        """INSERT INTO collector_item_state (uid, source_version, observed_at)
           VALUES (?, ?, ?)
           ON CONFLICT(uid) DO UPDATE SET
               source_version = excluded.source_version,
               observed_at = excluded.observed_at""",
        (uid, source_version, observed_at or utcnow_iso()),
    )


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
