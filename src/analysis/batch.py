"""批次 PK 计算 + 公共批次查询 + locale 复用判断（EN 批次分析能否直接套用到其它
locale，跳过 LLM 调用）。

分析单元是「批次」，不是单条公告：同一天同一 (source, category, locale) 的全部
status IN (new, changed) 公告合并成一次分析，一行 insights。duplicate_of 不为
NULL 的行（同源同 locale 下标题+正文完全一致的重复公告，见 src/pipeline/dedup.py）
一律排除，不重复计入批次。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Optional


def compute_batch_id(source: str, category: str, locale: str, batch_date: str) -> str:
    """id = SHA256(source || "_" || category || "_" || locale || "_" || batch_date)。"""
    raw = f"{source}_{category}_{locale}_{batch_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class BatchKey:
    source: str
    category: str
    locale: str
    batch_date: str

    @property
    def id(self) -> str:
        return compute_batch_id(self.source, self.category, self.locale, self.batch_date)


def list_batch_keys(
    conn: sqlite3.Connection, sources: tuple[str, ...], batch_date: str, *, include_unchanged: bool = False,
) -> list[BatchKey]:
    """枚举当天有 status IN (new, changed) 公告、且 category != 'other' 的全部
    (source, category, locale) 组合，同一 source×category 内 EN 排最前——
    can_derive_from_en() 依赖 EN 批次先算完并已写入 insights。
    """
    placeholders = ",".join("?" * len(sources))
    status_clause = "status IN ('new', 'changed', 'unchanged')" if include_unchanged else "status IN ('new', 'changed')"
    rows = conn.execute(
        f"""
        SELECT DISTINCT source, category, locale
        FROM announcements
        WHERE {status_clause}
              AND date(fetched_at) = ?
              AND source IN ({placeholders})
              AND category IS NOT NULL AND category != 'other'
              AND duplicate_of IS NULL
        """,
        (batch_date, *sources),
    ).fetchall()

    keys = [BatchKey(source=r["source"], category=r["category"], locale=r["locale"], batch_date=batch_date) for r in rows]
    keys.sort(key=lambda k: (k.source, k.category, k.locale != "EN", k.locale))
    return keys


def get_batch_uids(
    conn: sqlite3.Connection, source: str, category: str, locale: str, batch_date: str,
    *, include_unchanged: bool = False,
) -> list[str]:
    status_clause = "status IN ('new', 'changed', 'unchanged')" if include_unchanged else "status IN ('new', 'changed')"
    rows = conn.execute(
        f"""
        SELECT uid FROM announcements
        WHERE source = ? AND category = ? AND locale = ?
              AND {status_clause} AND date(fetched_at) = ?
              AND duplicate_of IS NULL
        ORDER BY uid
        """,
        (source, category, locale, batch_date),
    ).fetchall()
    return [r["uid"] for r in rows]


def get_batch_rows(
    conn: sqlite3.Connection, source: str, category: str, locale: str, batch_date: str,
    *, include_unchanged: bool = False,
) -> list[sqlite3.Row]:
    status_clause = "status IN ('new', 'changed', 'unchanged')" if include_unchanged else "status IN ('new', 'changed')"
    return conn.execute(
        f"""
        SELECT * FROM announcements
        WHERE source = ? AND category = ? AND locale = ?
              AND {status_clause} AND date(fetched_at) = ?
              AND duplicate_of IS NULL
        ORDER BY uid
        """,
        (source, category, locale, batch_date),
    ).fetchall()


def get_insight(conn: sqlite3.Connection, insight_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM insights WHERE id = ?", (insight_id,)).fetchone()


def _group_ids_for_uids(conn: sqlite3.Connection, uids: list[str]) -> set[str]:
    if not uids:
        return set()
    placeholders = ",".join("?" * len(uids))
    rows = conn.execute(
        f"SELECT group_id FROM announcements WHERE uid IN ({placeholders}) AND group_id IS NOT NULL",
        uids,
    ).fetchall()
    return {r["group_id"] for r in rows}


def can_derive_from_en(
    conn: sqlite3.Connection, source: str, category: str, locale: str, batch_date: str,
    *, include_unchanged: bool = False,
) -> Optional[str]:
    """返回可复用的 EN 批次 insight id；不满足复用条件返回 None。

    复用条件（同时满足）：
    1. locale != 'EN'
    2. 当日同 source × category × EN 的 insights 行已存在
    3. 当前 locale 与 EN 批次的 group_id 集合完全相同。仅做子集判断会让少文章的
       locale 复用包含额外文章的 EN summary，导致看板条数和结论互相矛盾。
    """
    if locale == "EN":
        return None

    en_insight = get_insight(conn, compute_batch_id(source, category, "EN", batch_date))
    if en_insight is None:
        return None

    current_uids = get_batch_uids(
        conn, source, category, locale, batch_date, include_unchanged=include_unchanged,
    )
    if not current_uids:
        return None

    current_group_ids = _group_ids_for_uids(conn, current_uids)
    if not current_group_ids:
        # group_id 缺失（理论上 Phase 3 归组后不应该发生），无法判定，保守起见不复用
        return None

    en_related_uids: list[str] = json.loads(en_insight["related_uids"] or "[]")
    en_group_ids = _group_ids_for_uids(conn, en_related_uids)

    if current_group_ids == en_group_ids:
        return en_insight["id"]
    return None
