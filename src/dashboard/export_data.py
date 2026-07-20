"""SQLite -> dashboard.json 导出器（Phase 7，2026-07-20 改版：locale-first ->
category-first schema，配合看板从「按语言分 tab」重构为「按类目分 tab」）。

设计目标：把「今天的数据库长什么样」压缩成一份看板前端可以直接 fetch 的静态 JSON，
这样看板本身（docs/index.html）是纯静态页面，可以直接挂 GitHub Pages；之后接入
定时任务时，调度脚本只需要在每次采集/分析跑完后执行一次

    python -m src.dashboard --db-path data/competitor_intel.db --out docs/data/dashboard.json

再把 docs/ 提交/发布出去即可，不需要额外的后端服务，也不需要改这个模块本身。

「今天」的定义：不依赖调用方传入日期，而是取 announcements 表里（排除 Zoomex 基线）
fetched_at 出现过的最大日期——这样无论调度器在什么时区/什么时刻跑，只要它当天只跑
一次，这个值天然就是"最近一次成功采集的那一天"，不需要额外传参也不需要假设"现在"
就是"数据里的今天"（两者在补数/重跑场景下可能不一致）。

顶层 schema（2026-07-20 起）：meta / overview / trend / campaign / product / listing /
markets / search_index。overview + campaign/product/listing + markets 的"明细"部分
只覆盖最新一批（meta.batch_date 当天、status IN new/changed）；trend 和 search_index
覆盖全部历史，交给前端自己按需切片/筛选，不重复为每种筛选组合单独查库。

诚实性说明：
- push_status 目前恒为 pending（Phase 6 推送引擎尚未实现，见 CLAUDE.md），所以本模块
  不展示"已推送"这类无法验证的数字，改为按 config/push_rules.yaml 记录的真实业务规则，
  计算一个"推送候选（预览）"指标——规则是真实配置，不是猜测，但结果是预览性质。
- 源自 mock 数据的 insights（llm_tokens_used=-1 的哨兵值，见
  scripts/generate_mock_insights.py）在导出的每个数据点上都带 is_mock 标记，前端据此
  渲染"模拟"角标，不会跟真实分析结果混淆。
- 老批次（Phase 4 per-article 字段扩展 -v2 上线前产出的、或本次改动后 LLM 校验失败
  导致 articles_analysis 为 NULL 的批次）不会有 diff_type/priority/follow_up/
  change_kind/listing_kind 这几个新字段——一律用 .get() 取值、取不到就是
  None/False，不抛异常，前端渲染中性默认，如实反映"这条还没有逐条分析结果"，不是 bug。
- 原来按 locale 分 tab 时代的"今日 Summary"（daily-digest-v1 LLM 综述）在本次改版里
  被明确移除，不是遗漏——新的 category-first Overview 设计（4 个 chip + highlights）
  没有它的位置，src/analysis/daily_digest.py 本身原样保留、只是不再被这里调用。
"""
from __future__ import annotations

import html
import json
import re
import sqlite3

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 竞品与语言范围——照抄 CLAUDE.md「竞品与语言范围」表，不是猜测值。
COMPETITORS: dict[str, list[str]] = {
    "Bitunix": ["EN", "FR", "ID"],
    "Weex": ["EN", "FR"],
    "BingX": ["EN", "VN"],
    "Phemex": ["EN", "FR"],
    "Lbank": ["EN", "VN", "ID"],
}
BASELINE_SOURCE = "Zoomex"
CATEGORIES = ["campaign", "product", "listing", "delisting"]

DIFF_TYPE_TAG = {
    "ZMX缺失": "missing",
    "ZMX玩法不同": "diff",
    "ZMX已有": "same",
    "混合": "mixed",
    "不适用": "na",
}
# 排序权重：ZMX缺失最紧急，"已有"/"不适用"（含从未产出逐条分析的老数据/其它噪音）垫底。
DIFF_TYPE_SORT_ORDER = {"missing": 0, "diff": 1, "mixed": 2, "same": 3, "na": 3}
PRIORITY_SORT_ORDER = {"高": 0, "中": 1, "低": 2}

# 每个 category 里"这条公告到底讲了什么"的描述性字段名不同（campaign 是 mechanics，
# product 是 feature_description，以此类推），取来填进 article_index 的统一
# "description" 键，供 Overview highlights / 各品类明细展示用——跟 follow_up（行动
# 建议）是两个不同性质的字段，都保留。
_DESCRIPTIVE_FIELD_BY_CATEGORY = {
    "campaign": "mechanics",
    "product": "feature_description",
    "listing": "project_brief",
    "delisting": "reason",
}

OVERVIEW_HIGHLIGHTS_CAP = 12


def _dict_rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _clean_title(title: Optional[str]) -> str:
    """部分源（如 BingX）的原始标题里留有未解码的 HTML 实体（如 "&amp;"），
    是采集层的既有数据质量问题，不在本次任务范围内改采集代码；这里只做无损的
    实体解码用于展示，不改变语义，避免看板上把 "&" 显示成 "&amp;"。"""
    if not title:
        return "(无标题)"
    return html.unescape(title)


def _format_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "--:--"
    try:
        return iso_ts.split("T")[1][:5]
    except IndexError:
        return "--:--"


def _diff_sort_key(diff_type: Optional[str]) -> int:
    return DIFF_TYPE_SORT_ORDER.get(DIFF_TYPE_TAG.get(diff_type or "", "na"), 4)


def _priority_sort_key(priority: Optional[str]) -> int:
    return PRIORITY_SORT_ORDER.get(priority or "", 3)


_PERP_RE = re.compile(r"\b(perpetual|perp|futures?|contract)\b", re.IGNORECASE)
_SPOT_RE = re.compile(r"\bspot\b", re.IGNORECASE)


def _listing_kind_from_title(title: Optional[str], category: str) -> Optional[str]:
    """Listing/Delisting 不调用 LLM；仅在标题有明确证据时做确定性归约。"""
    if category != "listing" or not title:
        return None
    has_perp = bool(_PERP_RE.search(title))
    has_spot = bool(_SPOT_RE.search(title))
    if has_perp == has_spot:
        return None
    return "perp" if has_perp else "spot"


def _resolve_as_of_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        f"SELECT MAX(date(fetched_at)) FROM announcements WHERE source != '{BASELINE_SOURCE}'"
    ).fetchone()
    if row and row[0]:
        return row[0]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _resolve_generated_at(conn: sqlite3.Connection) -> str:
    """跟 _resolve_as_of_date 同源但不做 date() 截断——meta.generated_at 需要完整
    时间戳，不只是日期。"""
    row = conn.execute(
        f"SELECT MAX(fetched_at) FROM announcements WHERE source != '{BASELINE_SOURCE}'"
    ).fetchone()
    if row and row[0]:
        return row[0]
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_article_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """uid -> 该条公告在 Phase 4 分析里产出的逐条字段。

    每个 insights 行的 articles_analysis 是一个 JSON 数组（每篇公告的结构化分析），
    这里展开成以 uid 为 key 的扁平索引，供后面按 announcements 逐行 join 用。

    老数据 / 校验失败的批次（articles_analysis 为 NULL 或不是合法 JSON 数组）会被
    静默跳过，不抛异常——这正是"老批次没有这几个新字段"这条约束在代码里的落地：
    找不到的 uid，调用方一律用 .get() 取默认值，不会因为这里跳过而报错。
    """
    rows = _dict_rows(
        conn.execute(
            "SELECT category, articles_analysis, llm_tokens_used, is_locale_derived FROM insights"
        )
    )
    index: dict[str, dict] = {}
    for r in rows:
        is_mock = r["llm_tokens_used"] == -1
        is_locale_derived = bool(r["is_locale_derived"])
        desc_field = _DESCRIPTIVE_FIELD_BY_CATEGORY.get(r["category"])
        try:
            articles = json.loads(r["articles_analysis"] or "[]")
        except (json.JSONDecodeError, TypeError):
            articles = []
        if not isinstance(articles, list):
            continue
        for a in articles:
            if not isinstance(a, dict):
                continue
            uid = a.get("uid")
            if not uid:
                continue
            index[uid] = {
                "diff_type": a.get("diff_type"),
                "priority": a.get("priority"),
                "priority_reason": a.get("priority_reason"),
                "action_type": a.get("action_type"),
                "owner": a.get("owner"),
                "follow_up": a.get("follow_up"),
                "change_kind": a.get("change_kind"),
                "listing_kind": a.get("listing_kind"),
                "description": a.get(desc_field) if desc_field else None,
                "is_mock": is_mock,
                "is_locale_derived": is_locale_derived,
            }
    return index


def _push_candidate(row: dict, article: dict) -> bool:
    """按 config/push_rules.yaml 的真实规则做一次预览判断（Phase 6 引擎未上线，
    这里不写库、不影响 push_status，只用于看板展示"如果 Phase 6 上线，这条会不会被推"）。
    """
    if row["push_status"] == "pushed":
        return False
    diff_type = article.get("diff_type")
    if diff_type == "ZMX已有":
        return False
    if row["category"] == "other":
        return False

    if row["category"] == "campaign" and row["status"] in ("new", "changed"):
        return True
    if diff_type == "ZMX缺失" and article.get("priority") == "高" and row["status"] == "new":
        return True
    if row["is_region_exclusive"]:
        return True
    if row["category"] == "delisting" and row["status"] in ("new", "changed"):
        return True
    return False


def build_category_section(
    conn: sqlite3.Connection, category: str, as_of_date: str, article_index: dict[str, dict]
) -> list[dict]:
    """最新一批（as_of_date 当天、status IN new/changed）某个 category 的逐条公告，
    一行一篇公告，按 uid join 到 Phase 4 的逐条分析结果（找不到就是中性默认）。

    这是 campaign/product/listing/delisting 四个 category 共用的构建函数——listing
    对外展示的 section 会把 delisting 的结果也拼进去（调用方负责拼接，这里只管单个
    category），"other" 也可以传进来单独查（只用于 Overview 的 Announcement chip
    计数，不作为独立的顶层导出 section）。
    """
    rows = _dict_rows(
        conn.execute(
            f"""SELECT uid, source, locale, title, post_time, status, url, is_region_exclusive
                FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND category = ?
                  AND date(fetched_at) = ? AND status IN ('new', 'changed')
                ORDER BY post_time DESC""",
            (category, as_of_date),
        )
    )
    out = []
    for r in rows:
        # Listing/Delisting 自本版起不做任何 LLM 分析或 ZMX 比较；即使数据库里
        # 留有旧版本 insight，也不得继续展示过期的 priority/diff/follow_up。
        art = {} if category in ("listing", "delisting") else article_index.get(r["uid"], {})
        out.append({
            "uid": r["uid"],
            "source": r["source"],
            "locale": r["locale"],
            "category": category,
            "title": _clean_title(r["title"]),
            "post_time": r["post_time"],
            "status": r["status"],
            "url": r["url"],
            "is_region_exclusive": bool(r["is_region_exclusive"]),
            "description": art.get("description"),
            "diff_type": art.get("diff_type"),
            "diff_tag": DIFF_TYPE_TAG.get(art.get("diff_type") or "", "na"),
            "priority": art.get("priority"),
            "priority_reason": art.get("priority_reason"),
            "action_type": art.get("action_type"),
            "owner": art.get("owner"),
            "follow_up": art.get("follow_up"),
            "change_kind": art.get("change_kind"),
            "listing_kind": (
                _listing_kind_from_title(r["title"], category)
                if category in ("listing", "delisting")
                else art.get("listing_kind")
            ),
            "is_mock": art.get("is_mock", False),
            "is_locale_derived": art.get("is_locale_derived", False),
        })
    return out


def build_overview(
    as_of_date: str,
    campaign_rows: list[dict],
    product_rows: list[dict],
    listing_only_rows: list[dict],
    delisting_rows: list[dict],
    other_rows: list[dict],
) -> dict:
    """4 个 chip（Campaign / Product / Listing / Announcement=delisting+other）+
    跨品类 highlights（priority=高，按优先级/差异类型排序，供 Stage 3 前端直接复用
    同一套排序规则）。"""

    def chip_from_rows(rows: list[dict]) -> dict:
        count_new = sum(1 for r in rows if r["status"] == "new")
        count_changed = sum(1 for r in rows if r["status"] == "changed")
        diff_breakdown = {"missing": 0, "diff": 0, "same": 0, "mixed": 0, "na": 0}
        for r in rows:
            diff_breakdown[r["diff_tag"]] = diff_breakdown.get(r["diff_tag"], 0) + 1
        return {"count_new": count_new, "count_changed": count_changed, "diff_breakdown": diff_breakdown}

    chips = {
        "campaign": chip_from_rows(campaign_rows),
        "product": chip_from_rows(product_rows),
        "listing": chip_from_rows(listing_only_rows),
        "announcement": chip_from_rows(delisting_rows + other_rows),
    }

    all_rows = campaign_rows + product_rows + listing_only_rows + delisting_rows
    highlights_pool = [r for r in all_rows if r["priority"] == "高"]
    highlights_pool.sort(key=lambda r: (r["is_mock"], _diff_sort_key(r["diff_type"])))
    highlights = [
        {
            "source": r["source"],
            "category": r["category"],
            "title": r["title"],
            "one_line_summary": (r["description"] or r["follow_up"] or "")[:160],
            "diff_type": r["diff_type"],
            "diff_tag": r["diff_tag"],
            "time": _format_time(r["post_time"]),
            "url": r["url"],
            "is_mock": r["is_mock"],
        }
        for r in highlights_pool[:OVERVIEW_HIGHLIGHTS_CAP]
    ]
    return {"batch_date": as_of_date, "chips": chips, "highlights": highlights}


def build_trend(conn: sqlite3.Connection) -> dict:
    """全部历史（不限 as_of_date）按天 x category 的公告计数，前端自己切
    7d/30d/全部——跟 search_index 一样"整段下发，交给前端筛"的思路，不为每种
    时间窗口单独查库。"""
    placeholders = ",".join("?" * len(CATEGORIES))
    rows = _dict_rows(
        conn.execute(
            f"""SELECT date(fetched_at) as d, category, COUNT(*) as n
                FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND category IN ({placeholders})
                GROUP BY d, category
                ORDER BY d""",
            CATEGORIES,
        )
    )
    dates = sorted({r["d"] for r in rows if r["d"]})
    series = {c: {d: 0 for d in dates} for c in CATEGORIES}
    for r in rows:
        if r["d"] and r["category"] in series:
            series[r["category"]][r["d"]] = r["n"]
    return {
        "dates": dates,
        "series": {c: [series[c][d] for d in dates] for c in CATEGORIES},
    }


def build_markets(conn: sqlite3.Connection) -> dict:
    """跨地区矩阵：每个 group_id（跨语言归组）在哪些 locale 出现过、是否被标记为
    地区独占——覆盖全部历史（不限 as_of_date），不是只看最新一批。按 locale 切片
    的视图是前端对 campaign/product/listing/search_index 的客户端再过滤，这里不
    重复导出，避免同一份数据在 JSON 里出现两遍。"""
    rows = _dict_rows(
        conn.execute(
            f"""SELECT group_id, source, locale, title, is_region_exclusive, post_time
                FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND group_id IS NOT NULL
                ORDER BY post_time DESC"""
        )
    )
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group_id"], {
            "group_id": r["group_id"],
            "source": r["source"],
            "title": r["title"] or "(无标题)",
            "locales": set(),
            "exclusive": False,
            "post_time": r["post_time"],
        })
        g["locales"].add(r["locale"])
        if r["is_region_exclusive"]:
            g["exclusive"] = True

    picked = sorted(groups.values(), key=lambda g: (-len(g["locales"]), g["post_time"] or ""))
    regions = [
        {
            "group_id": g["group_id"],
            "title": g["title"][:70],
            "source": g["source"],
            "locales": sorted(g["locales"]),
            "exclusive": g["exclusive"],
        }
        for g in picked
    ]
    return {"regions": regions}


def build_search_index(conn: sqlite3.Connection, article_index: dict[str, dict]) -> dict:
    """全部历史的扁平投影，只给 Search tab 用——字段刻意窄（不含正文/content），
    需要看全文的话点 url 回源站。跟 build_markets/build_trend 一样"整段下发，
    前端筛"，不做服务端分页/筛选。"""
    rows = _dict_rows(
        conn.execute(
            f"""SELECT uid, source, locale, category, title, post_time, status, url
                FROM announcements WHERE source != '{BASELINE_SOURCE}'
                ORDER BY post_time DESC"""
        )
    )
    out = []
    for r in rows:
        art = article_index.get(r["uid"], {})
        out.append({
            "uid": r["uid"],
            "source": r["source"],
            "locale": r["locale"],
            "category": r["category"] or "other",
            "title": _clean_title(r["title"]),
            "post_time": r["post_time"],
            "status": r["status"],
            "diff_type": art.get("diff_type"),
            "priority": art.get("priority"),
            "url": r["url"],
        })
    dates = [r["post_time"][:10] for r in out if r["post_time"]]
    return {
        "rows": out,
        "total": len(out),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def build_dashboard_data(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    as_of_date = _resolve_as_of_date(conn)
    generated_at = _resolve_generated_at(conn)
    article_index = _load_article_index(conn)

    campaign_rows = build_category_section(conn, "campaign", as_of_date, article_index)
    product_rows = build_category_section(conn, "product", as_of_date, article_index)
    listing_only_rows = build_category_section(conn, "listing", as_of_date, article_index)
    delisting_rows = build_category_section(conn, "delisting", as_of_date, article_index)
    other_rows = build_category_section(conn, "other", as_of_date, article_index)
    listing_rows = listing_only_rows + delisting_rows

    overview = build_overview(as_of_date, campaign_rows, product_rows, listing_only_rows, delisting_rows, other_rows)

    # 推送候选预览：附加在每个 category section 行上，跟 Phase 6 引擎无关，纯预览。
    for rows in (campaign_rows, product_rows, listing_rows):
        for r in rows:
            r["push_candidate"] = _push_candidate({**r, "push_status": "pending"}, r)

    insights_total = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    insights_mock = conn.execute("SELECT COUNT(*) FROM insights WHERE llm_tokens_used = -1").fetchone()[0]
    zoomex_total = conn.execute(
        f"SELECT COUNT(*) FROM announcements WHERE source = '{BASELINE_SOURCE}'"
    ).fetchone()[0]

    source_coverage = {}
    for source, locs in COMPETITORS.items():
        n_today = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE source=? AND date(fetched_at)=?",
            (source, as_of_date),
        ).fetchone()[0]
        n_total = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE source=?", (source,)
        ).fetchone()[0]
        source_coverage[source] = {
            "locales": locs,
            "today": n_today,
            "total": n_total,
            "active": n_total > 0,
        }

    data = {
        "meta": {
            "generated_at": generated_at,
            "batch_date": as_of_date,
            "db_path": str(Path(db_path).name),
            "insights_total": insights_total,
            "insights_mock": insights_mock,
            "zoomex_baseline_total": zoomex_total,
            "source_coverage": source_coverage,
        },
        "overview": overview,
        "trend": build_trend(conn),
        "campaign": campaign_rows,
        "product": product_rows,
        "listing": listing_rows,
        "markets": build_markets(conn),
        "search_index": build_search_index(conn, article_index),
    }
    conn.close()
    return data


def export(db_path: str, out_path: str) -> dict:
    data = build_dashboard_data(db_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
