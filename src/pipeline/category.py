"""分类打标：三层结构，本模块只实现第一层（raw_category 字典查找）+ 第二层
（标题关键词），第三层（LLM 兜底）留白——先用 dry-run 摸清楚会有多少行落到
第三层，报给用户确认成本可接受后再实现，见 CLAUDE.md「Phase 3」。

第一层：raw_category 精确字典查找（config/category_mapping.yaml），key 是
raw_category 的原始值字符串，不是人类可读名称（Phase 2.6 订正过的坑）。

第二层：只对「第一层映射到 other」或「raw_category 为 NULL/整源无映射」的行生效，
标题关键词命中优先级从上到下：listing > delisting > campaign > product > other。

raw_category 有值但不在映射表 key 集合里 —— 视为「源站新增了分区」，显式标成
unmapped_native，不静默落到关键词层（那样会掩盖一个需要人工补映射的信号）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("listing", ["list", "listing", "launchpool", "new coin", "initial listing"]),
    ("delisting", ["delist", "delisting", "removal", "下架"]),
    ("campaign", ["competition", "contest", "trading", "reward", "bonus", "airdrop", "giveaway"]),
    ("product", ["update", "upgrade", "launch", "feature", "now supports"]),
    ("other", ["maintenance", "system", "suspension", "risk"]),
]

VALID_CATEGORIES = {"campaign", "product", "listing", "delisting", "other"}


@dataclass
class ClassificationResult:
    category: Optional[str]
    layer: str  # native / native_other / keyword / unmapped_native / llm_pending
    reason: str


def classify_by_keyword(title: Optional[str]) -> Optional[str]:
    """"delisting" 逐字节包含 "listing"（de-listing），纯子串匹配会导致任何下架
    标题在检查到 listing 分类的 "list" 关键词时被提前误判成 listing——用词边界
    （\\b）匹配而不是子串包含来避免这个问题：`\\blist\\b` 不会命中 "delisting"
    内部（"delist" 和 "ing" 之间没有单词边界）。中文关键词（如 "下架"）没有空格
    分词，`\\b` 在连续中文文本里几乎不产生边界，因此中文关键词仍用子串包含。
    """
    if not title:
        return None
    lowered = title.lower()
    for category, keywords in KEYWORD_RULES:
        for kw in keywords:
            if kw.isascii():
                hit = re.search(r"\b" + re.escape(kw.lower()) + r"\b", lowered) is not None
            else:
                hit = kw in title
            if hit:
                return category
    return None


def classify_row(
    source: str,
    raw_category: Optional[str],
    title: Optional[str],
    mapping: dict[str, Optional[dict[str, str]]],
) -> ClassificationResult:
    source_key = source.lower()
    source_map = mapping.get(source_key)

    if source_map is not None and raw_category is not None:
        if raw_category not in source_map:
            return ClassificationResult(
                None,
                "unmapped_native",
                f"raw_category={raw_category!r} 不在 category_mapping.yaml[{source_key}] 里，"
                "疑似源站新增了分区，需要人工补映射",
            )
        layer1_category = source_map[raw_category]
        if layer1_category != "other":
            return ClassificationResult(layer1_category, "native", f"raw_category={raw_category} -> {layer1_category}")
        kw = classify_by_keyword(title)
        if kw:
            return ClassificationResult(
                kw, "keyword", f"raw_category={raw_category} -> other，标题关键词命中 -> {kw}"
            )
        return ClassificationResult("other", "native_other", f"raw_category={raw_category} -> other，无关键词可细分")

    # source_map 为 None（整源无 per-item 映射，如 lbank）或 raw_category 为 NULL
    kw = classify_by_keyword(title)
    if kw:
        return ClassificationResult(kw, "keyword", f"raw_category=NULL，标题关键词命中 -> {kw}")
    return ClassificationResult(None, "llm_pending", "无原生映射、无关键词命中，需要第三层 LLM 兜底")


@dataclass
class DryRunReport:
    total: int = 0
    layer_counts: dict[str, int] = field(default_factory=dict)
    unmapped_samples: list[tuple] = field(default_factory=list)
    llm_pending_samples: list[tuple] = field(default_factory=list)


def dry_run(
    conn: sqlite3.Connection,
    mapping: dict[str, Optional[dict[str, str]]],
    sources: tuple[str, ...] = ("Bitunix", "Weex"),
    sample_size: int = 20,
) -> DryRunReport:
    report = DryRunReport()
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"SELECT uid, source, locale, article_id, title, raw_category FROM announcements "
        f"WHERE source IN ({placeholders})",
        sources,
    ).fetchall()

    for uid, source, locale, article_id, title, raw_category in rows:
        result = classify_row(source, raw_category, title, mapping)
        report.total += 1
        report.layer_counts[result.layer] = report.layer_counts.get(result.layer, 0) + 1
        if result.layer == "unmapped_native" and len(report.unmapped_samples) < sample_size:
            report.unmapped_samples.append((source, locale, article_id, title, raw_category))
        if result.layer == "llm_pending" and len(report.llm_pending_samples) < sample_size:
            report.llm_pending_samples.append((source, locale, article_id, title, raw_category))

    return report


def print_dry_run_report(report: DryRunReport) -> None:
    print(f"共扫描 {report.total} 行")
    for layer in ("native", "native_other", "keyword", "unmapped_native", "llm_pending"):
        n = report.layer_counts.get(layer, 0)
        pct = (n / report.total * 100) if report.total else 0.0
        print(f"  {layer:16s} {n:6d}  ({pct:5.1f}%)")
    other_layers = set(report.layer_counts) - {"native", "native_other", "keyword", "unmapped_native", "llm_pending"}
    for layer in other_layers:
        print(f"  {layer:16s} {report.layer_counts[layer]:6d}  (未预期的 layer 值！)")

    if report.unmapped_samples:
        print(f"\n--- unmapped_native 抽样（源站新增分区，需要补映射）：{len(report.unmapped_samples)} 条 ---")
        for source, locale, article_id, title, raw_category in report.unmapped_samples:
            print(f"  [{source}/{locale}] raw_category={raw_category} article_id={article_id} title={title!r}")

    if report.llm_pending_samples:
        print(f"\n--- llm_pending 抽样（要发给 LLM 的候选）：{len(report.llm_pending_samples)} 条 ---")
        for source, locale, article_id, title, raw_category in report.llm_pending_samples:
            print(f"  [{source}/{locale}] raw_category={raw_category} article_id={article_id} title={title!r}")


def apply_layer1_layer2(
    conn: sqlite3.Connection,
    mapping: dict[str, Optional[dict[str, str]]],
    sources: tuple[str, ...] = ("Bitunix", "Weex"),
) -> dict[str, int]:
    """把 native / native_other / keyword 三种已经解析出确定 category 的行写回
    announcements.category。llm_pending / unmapped_native 保持 category=NULL，
    留给以后的第三层或人工补映射，不在这里猜测。直接 UPDATE，不走
    upsert_announcement（不碰 status/content_hash/push_status/content_history）。
    """
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"SELECT uid, source, title, raw_category FROM announcements WHERE source IN ({placeholders})",
        sources,
    ).fetchall()

    counts: dict[str, int] = {}
    updates: list[tuple[str, str]] = []
    for uid, source, title, raw_category in rows:
        result = classify_row(source, raw_category, title, mapping)
        counts[result.layer] = counts.get(result.layer, 0) + 1
        if result.category is not None:
            updates.append((result.category, uid))

    conn.executemany("UPDATE announcements SET category = ? WHERE uid = ?", updates)
    counts["_written"] = len(updates)
    return counts
