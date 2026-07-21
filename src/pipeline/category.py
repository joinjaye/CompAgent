"""分类打标：三层结构，本模块只实现第一层（raw_category 字典查找）+ 第二层
（标题关键词），第三层（LLM 兜底）留白——先用 dry-run 摸清楚会有多少行落到
第三层，报给用户确认成本可接受后再实现，见 CLAUDE.md「Phase 3」。

第一层：raw_category 精确字典查找（config/category_mapping.yaml），key 是
raw_category 的原始值字符串，不是人类可读名称（Phase 2.6 订正过的坑）。

第二层：只对「第一层映射到 other」或「raw_category 为 NULL/整源无映射」的行生效，
标题关键词命中优先级从上到下：listing > delisting > campaign > product > other。

raw_category 有值但不在映射表 key 集合里 —— 视为「源站新增了分区」，显式标成
unmapped_native，不静默落到关键词层（那样会掩盖一个需要人工补映射的信号）。

第二层内部还有两种"兜底"子层，均只在 KEYWORD_RULES 全不命中时才检查，互不影响优先级：
- LISTING_FALLBACK_KEYWORDS：全源共用，专为 Zoomex menu_id=26 设计（历史遗留的已知
  风险：没有按 source 限定，理论上任何源命中这几个短语都会被拉成 listing）。
- WEEX_LATEST_UPDATES_PRODUCT_FALLBACK：2026-07-20 新增，按 (source, raw_category)
  精确限定，只在 source="weex" 且 raw_category="18540289930137"（"Latest updates"
  混合 section，见 CLAUDE.md「Weex 补充 section 采集」）时生效，不会影响其它源或
  Weex 其它 section 的 other 判定——刻意比 LISTING_FALLBACK_KEYWORDS 收紧的设计。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("listing", ["list", "listing", "listed", "launchpool", "new coin", "initial listing"]),
    ("delisting", ["delist", "delisting", "removal", "下架"]),
    #("campaign", ["competition", "contest", "trading", "reward", "bonus", "airdrop", "giveaway"]),
    #("product", ["update", "upgrade", "launch", "feature", "now supports"]),
    #("other", ["maintenance", "system", "suspension", "risk"]),
]

# 【2026-07-14 新增】只在 KEYWORD_RULES 全部不命中时才检查的兜底层，不参与上面的
# 优先级排序、不会覆盖任何已经被 KEYWORD_RULES 命中的分类。背景：Zoomex
# menu_id=26"Platform Announcement"是唯一一个把 listing/delisting/product 全部
# 混在一起、不分 section 的 raw_category（Bitunix/Weex 的 listing/delisting 各自
# 有专属 section，靠第一层 raw_category 映射就能解决，几乎不依赖关键词层），真实
# 标题抽样发现 Zoomex 描述新币种/新合约上线完全不用"list"/"listing"这个词，而是
# "X are now live"/"X is now available on Zoomex Spot"/"perpetual contract(s) are
# available"/"Launching Soon on Zoomex Spot"这类措辞，导致这部分内容原本落进
# native_other（完全没有关键词命中，才判 other，不是被误判）。
#
# 特意做成"仅在无命中时兜底"而不是塞进 KEYWORD_RULES 的 listing 分组：后者会因为
# listing 排在 campaign/product 前面，抢先命中那些标题里恰好也包含"trading"（如
# "...on Zoomex Spot Trading Platform"）等词的行——这些行在改动前已经被
# KEYWORD_RULES 命中过（即使命中的词本身是误报，比如"trading"），把它们从
# campaign/product 改判成 listing 不在本次修复范围内（用户明确要求这次改动只影响
# 原本完全没有关键词命中、落进 other 的行，不要动其它已经"命中过"的分类结果）。
# 已用真实数据核对：能从 Zoomex 该分类下真正落进 native_other 的行里正确拉出 275
# 条真实上新公告，且跟已有 220 条 delisting 判定没有冲突（0 条被误翻成 listing）。
# 这几个短语本身足够具体，不太可能在 Bitunix/Weex 的"other"分区（维护/系统更新类
# 标题）里误触发，但因为这是全源共用的兜底层，等 Bitunix/Weex 数据重新采集回来后
# 应该跑一次 dry-run 交叉确认没有引入新的误判。
LISTING_FALLBACK_KEYWORDS: list[str] = [
    "now live", "is now available", "now available on",
    "contract are available", "contracts are available",
    "launching soon",
]

# 【2026-07-20 新增】Weex sectionId=18540289930137（"Latest updates"）专属兜底层，
# 只在 classify_by_keyword() 的 source/raw_category 参数精确匹配这个 (source,
# raw_category) 组合时才会被检查——不是塞进 KEYWORD_RULES/LISTING_FALLBACK_KEYWORDS
# 那种全源共用的列表，避免这批词在其它源/其它 Weex section 里误触发。
#
# 真实抽样这个 section 全部 251 条标题（2026-07-20 抓取）：KEYWORD_RULES（listing/
# delisting）全不命中的 243 条里，人工核对发现约 80 条是明确的 product 内容——质押
# 新品上线（"WEEX is about to Launch X Staking!"）、期货杠杆/保证金调整
# （"WEEX Futures Adjusts Leverage for..."）、App 版本更新公告、跟单交易新增交易对、
# 手续费/最小下单量调整等。这份关键词列表就是从这批真实标题里提炼的，未命中的其余
# 163 条（含约 39 条真实 campaign 内容——领奖/抽奖/WXT 销毁播报，本次有意不处理，
# 留给以后单独加 campaign 层）里没有被误伤的样本。
WEEX_LATEST_UPDATES_PRODUCT_FALLBACK: list[str] = [
    r"\bstaking\b",
    r"\bleverage\b",
    r"copy trading",
    r"\bapp\b \d",  # "WEEX App 3.4.2 Update Announcement"
    r"mandatory update for weex app",
    r"officially launch",
    r"launches the new",
    r"\bintegrates\b",
    r"minimum order size",
    r"platform token",
    r"new experience",
    r"auto earn",
    r"maker fee",
    r"spot pro system",
]

VALID_CATEGORIES = {"campaign", "product", "listing", "delisting", "other"}


@dataclass
class ClassificationResult:
    category: Optional[str]
    layer: str  # native / native_other / keyword / unmapped_native / llm_pending
    reason: str


def classify_by_keyword(
    title: Optional[str],
    source: Optional[str] = None,
    raw_category: Optional[str] = None,
) -> Optional[str]:
    """"delisting" 逐字节包含 "listing"（de-listing），纯子串匹配会导致任何下架
    标题在检查到 listing 分类的 "list" 关键词时被提前误判成 listing——用词边界
    （\\b）匹配而不是子串包含来避免这个问题：`\\blist\\b` 不会命中 "delisting"
    内部（"delist" 和 "ing" 之间没有单词边界）。中文关键词（如 "下架"）没有空格
    分词，`\\b` 在连续中文文本里几乎不产生边界，因此中文关键词仍用子串包含。

    `source`/`raw_category` 是可选参数，默认 None（不传时行为跟只传 title 完全
    一致）——只有 WEEX_LATEST_UPDATES_PRODUCT_FALLBACK 这个精确限定的兜底层需要
    用它们判断是否命中作用范围，LISTING_FALLBACK_KEYWORDS 等全源共用的兜底层不
    受这两个参数影响。
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
    for kw in LISTING_FALLBACK_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", lowered) is not None:
            return "listing"
    if source is not None and source.lower() == "weex" and raw_category == "18540289930137":
        for pattern in WEEX_LATEST_UPDATES_PRODUCT_FALLBACK:
            if re.search(pattern, lowered) is not None:
                return "product"
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
        kw = classify_by_keyword(title, source, raw_category)
        if kw:
            return ClassificationResult(
                kw, "keyword", f"raw_category={raw_category} -> other，标题关键词命中 -> {kw}"
            )
        return ClassificationResult("other", "native_other", f"raw_category={raw_category} -> other，无关键词可细分")

    # source_map 为 None（整源无 per-item 映射，如 lbank）或 raw_category 为 NULL
    kw = classify_by_keyword(title, source, raw_category)
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
