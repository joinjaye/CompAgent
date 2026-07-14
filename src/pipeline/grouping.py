"""跨语言归组（group_id）一致性防御性扫描。

group_id 在采集阶段就已经生成（Zendesk/Zoomex article_id 跨 locale 一致，
group_id = f"{prefix}_{article_id}"，见 CLAUDE.md「Phase 2.7」的验证记录）。
本模块**不做归组**，只做两类不该出现的异常扫描：

1. 同一 source + article_id 出现了不止一个 group_id 值 —— 说明 group_id 拼接逻辑
   本身有 bug（同一篇文章被拆成了两个不同的组）。
2. 某个 group 里出现的 locale 数超过该源在 sources.yaml 里实际配置的 locale 数
   —— 说明有条目的 group_id 撞车（不同源文章碰巧拼出同一个 group_id）或者
   locale 配置本身有问题。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class GroupConsistencyReport:
    duplicate_group_id: list[tuple[str, str, list[str]]] = field(default_factory=list)
    # (source, article_id, [group_id, ...]) —— 同一篇文章有多个 group_id

    locale_overflow: list[tuple[str, str, list[str], set[str]]] = field(default_factory=list)
    # (source, group_id, [实际出现的 locale...], 该源允许的 locale 集合)

    total_groups_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.duplicate_group_id and not self.locale_overflow


def scan_group_consistency(
    conn: sqlite3.Connection,
    source_locales: dict[str, set[str]],
    sources: tuple[str, ...] = ("Bitunix", "Weex", "Zoomex"),
) -> GroupConsistencyReport:
    report = GroupConsistencyReport()
    if not sources:
        return report
    placeholders = ",".join("?" * len(sources))

    dup_rows = conn.execute(
        f"""
        SELECT source, article_id, GROUP_CONCAT(DISTINCT group_id) AS gids, COUNT(DISTINCT group_id) AS n
        FROM announcements
        WHERE source IN ({placeholders}) AND group_id IS NOT NULL
        GROUP BY source, article_id
        HAVING n > 1
        """,
        sources,
    ).fetchall()
    for source, article_id, gids, _n in dup_rows:
        report.duplicate_group_id.append((source, article_id, gids.split(",")))

    group_rows = conn.execute(
        f"""
        SELECT source, group_id, GROUP_CONCAT(DISTINCT locale) AS locales, COUNT(DISTINCT locale) AS n
        FROM announcements
        WHERE source IN ({placeholders}) AND group_id IS NOT NULL
        GROUP BY source, group_id
        """,
        sources,
    ).fetchall()
    report.total_groups_checked = len(group_rows)
    for source, group_id, locales, _n in group_rows:
        actual_locales = locales.split(",")
        allowed = source_locales.get(source.lower(), set())
        if allowed and len(actual_locales) > len(allowed):
            report.locale_overflow.append((source, group_id, actual_locales, allowed))

    return report


def print_report(report: GroupConsistencyReport) -> None:
    print(f"检查了 {report.total_groups_checked} 个 group。")
    print(f"group_id 重复异常（同一篇文章多个 group_id）：{len(report.duplicate_group_id)} 条")
    for source, article_id, gids in report.duplicate_group_id[:20]:
        print(f"  source={source} article_id={article_id} group_ids={gids}")
    print(f"locale 数溢出异常：{len(report.locale_overflow)} 条")
    for source, group_id, actual_locales, allowed in report.locale_overflow[:20]:
        print(f"  source={source} group_id={group_id} locales={actual_locales} allowed={sorted(allowed)}")
    print("结论：" + ("PASS，0 异常" if report.ok else "FAIL，发现异常，见上"))
