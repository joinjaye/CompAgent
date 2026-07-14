"""地区独占标记：某 group 只在该源配置的非 EN 单一 locale 出现 -> is_region_exclusive=true。

判断"独占"必须按该源在 sources.yaml 里实际配置的 locale 集合来看，不能用全局 locale
集合——Bitunix 是 EN/FR/ID 三语、Weex 只有 EN/FR，两者独立判断。EN-only 是常态，不标记。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class RegionMarkReport:
    total_groups: int = 0
    exclusive_groups: int = 0
    exclusive_rows_updated: int = 0
    non_exclusive_rows_updated: int = 0
    exclusive_samples: list[tuple] = field(default_factory=list)


def compute_region_exclusive(
    conn: sqlite3.Connection,
    source_locales: dict[str, set[str]],
    sources: tuple[str, ...] = ("Bitunix", "Weex"),
) -> dict[str, bool]:
    """返回 group_id -> is_region_exclusive。"""
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"""
        SELECT source, group_id, GROUP_CONCAT(DISTINCT locale) AS locales
        FROM announcements
        WHERE source IN ({placeholders}) AND group_id IS NOT NULL
        GROUP BY source, group_id
        """,
        sources,
    ).fetchall()

    result: dict[str, bool] = {}
    for source, group_id, locales in rows:
        del source  # 只用于分组查询，不参与判断本身（判断只看 locale 集合）
        locale_list = locales.split(",")
        is_exclusive = len(locale_list) == 1 and locale_list[0] != "EN"
        result[group_id] = is_exclusive
    return result


def apply_region_exclusive(
    conn: sqlite3.Connection,
    source_locales: dict[str, set[str]],
    sources: tuple[str, ...] = ("Bitunix", "Weex"),
) -> RegionMarkReport:
    group_flags = compute_region_exclusive(conn, source_locales, sources)
    report = RegionMarkReport(total_groups=len(group_flags))
    report.exclusive_groups = sum(1 for v in group_flags.values() if v)

    placeholders = ",".join("?" * len(sources))
    ann_rows = conn.execute(
        f"SELECT uid, group_id, source, locale, title FROM announcements WHERE source IN ({placeholders})",
        sources,
    ).fetchall()

    exclusive_updates: list[tuple[int, str]] = []
    non_exclusive_updates: list[tuple[int, str]] = []
    for uid, group_id, source, locale, title in ann_rows:
        is_exclusive = group_flags.get(group_id, False)
        if is_exclusive:
            exclusive_updates.append((1, uid))
            if len(report.exclusive_samples) < 20:
                report.exclusive_samples.append((source, locale, group_id, title))
        else:
            non_exclusive_updates.append((0, uid))

    conn.executemany("UPDATE announcements SET is_region_exclusive = ? WHERE uid = ?", exclusive_updates)
    conn.executemany("UPDATE announcements SET is_region_exclusive = ? WHERE uid = ?", non_exclusive_updates)
    report.exclusive_rows_updated = len(exclusive_updates)
    report.non_exclusive_rows_updated = len(non_exclusive_updates)
    return report


def print_report(report: RegionMarkReport) -> None:
    print(f"共 {report.total_groups} 个 group，其中 {report.exclusive_groups} 个判定为地区独占")
    print(f"更新行数：is_region_exclusive=true {report.exclusive_rows_updated} 行，"
          f"=false {report.non_exclusive_rows_updated} 行")
    if report.exclusive_samples:
        print("\n--- 地区独占抽样 ---")
        for source, locale, group_id, title in report.exclusive_samples:
            print(f"  [{source}/{locale}] group_id={group_id} title={title!r}")
