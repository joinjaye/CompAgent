"""同源同 locale 下的重复公告检测：标题+正文完全一致、但 article_id 不同的行，
标记为重复（duplicate_of 指回最早一条的 uid），下游分析/看板据此排除，不重复计入。

判重口径固定为「同 source + locale + title + content」，不能只按 content_hash——
2026-07-21 真实数据核查发现两类情况：
1. 真重复：源站对同一条通知换了新 article_id 重新发布（如 BingX 同一条维护保证金率
   公告，标题、正文完全一致，相隔约 2 小时各出现一个新 ID）。
2. 假重复：Zoomex 自己的 CMS 里，不同事件（如两次不同代币的合约上线公告）复用了
   同一段模板正文，只有标题（代币名）不同，content_hash 因此相同——这种不能合并，
   合并会丢失两条独立的竞品情报。
只看 content_hash 会把 2 也判成重复；title+content 双重匹配能正确排除 2、保留 1。

内容为空的行（content_hash 等于 SHA256("")）不参与判重——空内容本身是另一个问题
（见 Zoomex 采集器 fetch_detail() 的 title 兜底），大量空内容行会在 content_hash 上
互相"撞车"，产生大量无意义的假聚类。

只处理当前 duplicate_of IS NULL 的行——已标记过的行不参与新一轮聚类，避免重复处理；
多次运行是幂等的，后续新出现的第三份重复会自然指向已存在的规范行。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DedupCluster:
    source: str
    locale: str
    title: str
    canonical_uid: str
    duplicate_uids: list[str]


@dataclass
class DedupReport:
    clusters_found: int = 0
    rows_marked: int = 0
    samples: list[DedupCluster] = field(default_factory=list)


def find_duplicate_clusters(
    conn: sqlite3.Connection,
    sources: Optional[tuple[str, ...]] = None,
) -> list[DedupCluster]:
    where_source = ""
    params: list[str] = []
    if sources:
        placeholders = ",".join("?" * len(sources))
        where_source = f"AND source IN ({placeholders})"
        params.extend(sources)

    rows = conn.execute(
        f"""
        SELECT uid, source, locale, title, content_hash, post_time, fetched_at
        FROM announcements
        WHERE duplicate_of IS NULL
              AND title IS NOT NULL AND title != ''
              AND content IS NOT NULL AND content != ''
              {where_source}
        ORDER BY source, locale, title, content_hash,
                 COALESCE(post_time, fetched_at, ''), uid
        """,
        params,
    ).fetchall()

    clusters: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (row["source"], row["locale"], row["title"], row["content_hash"])
        clusters.setdefault(key, []).append(row)

    result: list[DedupCluster] = []
    for (source, locale, title, _content_hash), group in clusters.items():
        if len(group) < 2:
            continue
        # 已按 (post_time/fetched_at, uid) 排过序，group[0] 是最早的一条 -> 规范行
        canonical, *duplicates = group
        result.append(
            DedupCluster(
                source=source,
                locale=locale,
                title=title,
                canonical_uid=canonical["uid"],
                duplicate_uids=[r["uid"] for r in duplicates],
            )
        )
    return result


def apply_dedup(
    conn: sqlite3.Connection,
    sources: Optional[tuple[str, ...]] = None,
) -> DedupReport:
    clusters = find_duplicate_clusters(conn, sources)
    report = DedupReport(clusters_found=len(clusters))

    updates: list[tuple[str, str]] = []
    for cluster in clusters:
        for dup_uid in cluster.duplicate_uids:
            updates.append((cluster.canonical_uid, dup_uid))
        if len(report.samples) < 20:
            report.samples.append(cluster)

    conn.executemany("UPDATE announcements SET duplicate_of = ? WHERE uid = ?", updates)
    report.rows_marked = len(updates)
    return report


def print_report(report: DedupReport) -> None:
    print(f"发现 {report.clusters_found} 组重复，标记 duplicate_of {report.rows_marked} 行")
    if report.samples:
        print("\n--- 抽样 ---")
        for cluster in report.samples:
            print(
                f"  [{cluster.source}/{cluster.locale}] canonical={cluster.canonical_uid} "
                f"duplicates={cluster.duplicate_uids} title={cluster.title!r}"
            )
