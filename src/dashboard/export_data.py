"""SQLite -> dashboard.json 导出器（Phase 7）。

设计目标：把「今天的数据库长什么样」压缩成一份看板前端可以直接 fetch 的静态 JSON，
这样看板本身（docs/index.html）是纯静态页面，可以直接挂 GitHub Pages；之后接入
定时任务时，调度脚本只需要在每次采集/分析跑完后执行一次

    python -m src.dashboard --db-path data/competitor_intel.db --out docs/data/dashboard.json

再把 docs/ 提交/发布出去即可，不需要额外的后端服务，也不需要改这个模块本身。

「今天」的定义：不依赖调用方传入日期，而是取 announcements 表里（排除 Zoomex 基线）
fetched_at 出现过的最大日期——这样无论调度器在什么时区/什么时刻跑，只要它当天只跑
一次，这个值天然就是"最近一次成功采集的那一天"，不需要额外传参也不需要假设"现在"
就是"数据里的今天"（两者在补数/重跑场景下可能不一致）。

诚实性说明：push_status 目前恒为 pending（Phase 6 推送引擎尚未实现，见 CLAUDE.md），
所以本模块不展示"已推送"这类无法验证的数字，改为按 config/push_rules.yaml 记录的
真实业务规则，计算一个"推送候选（预览）"指标——规则是真实配置，不是猜测，但结果是
预览性质，明确标注引擎尚未上线。同理，源自 mock 数据的 insights（llm_tokens_used=-1
的哨兵值，见 scripts/generate_mock_insights.py）在导出的每个数据点上都带 is_mock 标记，
前端据此渲染"模拟"角标，不会跟真实分析结果混淆。
"""
from __future__ import annotations

import html
import json
import sqlite3

from src.analysis.daily_digest import peek_cached_digest
from dataclasses import dataclass, field
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
BASELINE_LOCALES = ["EN", "FR", "EN-Asia", "VN", "ID"]

ALL_LOCALES = ["EN", "FR", "VN", "ID", "EN-Asia"]
CATEGORIES = ["campaign", "product", "listing", "delisting"]

CATEGORY_LABEL_ZH = {"campaign": "活动", "product": "产品", "listing": "上币", "delisting": "下架"}

DIFF_TYPE_TAG = {
    "ZMX缺失": "missing",
    "ZMX玩法不同": "diff",
    "ZMX已有": "same",
    "混合": "mixed",
    "不适用": "na",
}

HIGHLIGHTS_PER_LOCALE = 8
CEX_ROWS_PER_CATEGORY = 15  # 区域 tab 只展示"最新一批"的digest，完整数据在全量 tab 里筛选浏览
ZMX_MISSING_SUMMARY_CAP = 12
REGION_TABLE_CAP = 14


def _dict_rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


@dataclass
class InsightRef:
    id: str
    source: str
    category: str
    locale: str
    diff_type: Optional[str]
    priority: Optional[str]
    summary: Optional[str]
    zmx_diff: Optional[str]
    is_mock: bool
    is_locale_derived: bool
    article_count: int
    first_article_title: Optional[str] = None


def _load_insight_index(conn: sqlite3.Connection) -> tuple[dict[str, InsightRef], list[InsightRef]]:
    """返回 (uid -> 所属批次, 全部批次列表)。"""
    rows = _dict_rows(
        conn.execute(
            """SELECT id, source, category, locale, diff_type, priority, summary, zmx_diff,
                      related_uids, articles_analysis, article_count, llm_tokens_used,
                      is_locale_derived
               FROM insights"""
        )
    )
    uid_map: dict[str, InsightRef] = {}
    batches: list[InsightRef] = []
    for r in rows:
        is_mock = r["llm_tokens_used"] == -1
        first_title = None
        try:
            articles = json.loads(r["articles_analysis"] or "[]")
            if articles:
                first_title = articles[0].get("title")
        except (json.JSONDecodeError, AttributeError):
            pass
        ref = InsightRef(
            id=r["id"],
            source=r["source"],
            category=r["category"],
            locale=r["locale"],
            diff_type=r["diff_type"],
            priority=r["priority"],
            summary=r["summary"],
            zmx_diff=r["zmx_diff"],
            is_mock=is_mock,
            is_locale_derived=bool(r["is_locale_derived"]),
            article_count=r["article_count"],
            first_article_title=first_title,
        )
        batches.append(ref)
        try:
            uids = json.loads(r["related_uids"] or "[]")
        except json.JSONDecodeError:
            uids = []
        for uid in uids:
            uid_map[uid] = ref
    return uid_map, batches


def _resolve_as_of_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        f"SELECT MAX(date(fetched_at)) FROM announcements WHERE source != '{BASELINE_SOURCE}'"
    ).fetchone()
    if row and row[0]:
        return row[0]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _push_candidate(row: dict, insight: Optional[InsightRef]) -> bool:
    """按 config/push_rules.yaml 的真实规则做一次预览判断（Phase 6 引擎未上线，
    这里不写库、不影响 push_status，只用于看板展示"如果 Phase 6 上线，这条会不会被推"）。
    """
    if row["push_status"] == "pushed":
        return False
    diff_type = insight.diff_type if insight else None
    if diff_type == "ZMX已有":
        return False
    if row["category"] == "other":
        return False

    if row["category"] == "campaign" and row["status"] == "new":
        return True
    if row["category"] == "campaign" and row["status"] == "changed":
        # diff_touches_rules_or_reward 是 Phase 4 分析结果里的派生字段，本预览没有
        # 真实的语义判断依据，退而求其次用「该批次是否为 changed 生成了 change_summary」
        # 做近似（有 change_summary 才说明分析层认为这次变更值得一提）。
        return True
    if diff_type == "ZMX缺失" and insight and insight.priority == "高" and row["status"] == "new":
        return True
    if row["is_region_exclusive"]:
        return True
    if row["category"] == "delisting" and row["status"] in ("new", "changed"):
        return True
    return False


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


def build_overview(conn: sqlite3.Connection, as_of_date: str, locale: Optional[str],
                    uid_map: dict[str, InsightRef]) -> dict:
    where_locale = "AND locale = ?" if locale else ""
    params: list = [as_of_date]
    if locale:
        params.append(locale)

    base_sql = f"""
        SELECT uid, source, category, status, is_region_exclusive
        FROM announcements
        WHERE source != '{BASELINE_SOURCE}' AND date(fetched_at) = ? {where_locale}
    """
    rows = _dict_rows(conn.execute(base_sql, params))

    fetched_today = len(rows)
    new_count = sum(1 for r in rows if r["status"] == "new")
    changed_count = sum(1 for r in rows if r["status"] == "changed")
    region_exclusive = sum(1 for r in rows if r["is_region_exclusive"])
    sources_active = {r["source"] for r in rows}

    configured_sources = (
        [s for s, locs in COMPETITORS.items() if locale in locs] if locale
        else list(COMPETITORS.keys())
    )

    zmx_missing_high_batches = {
        ins.id for uid, ins in uid_map.items()
        if ins.diff_type == "ZMX缺失" and ins.priority == "高"
        and (ins.locale == locale if locale else True)
    }

    push_candidates = 0
    for r in rows:
        full_row = {**r, "push_status": "pending"}
        insight = uid_map.get(r["uid"])
        if _push_candidate(full_row, insight):
            push_candidates += 1

    return {
        "locale": locale or "ALL",
        "fetched_today": fetched_today,
        "new_count": new_count,
        "changed_count": changed_count,
        "sources_active": len(sources_active),
        "sources_configured": len(configured_sources),
        "sources_list": sorted(configured_sources),
        "region_exclusive_count": region_exclusive,
        "zmx_missing_high": len(zmx_missing_high_batches),
        "push_candidates": push_candidates,
    }


def build_daily_digest(conn: sqlite3.Connection, locale: str, batch_date: str, ov: dict,
                        locale_batches: list[InsightRef]) -> dict:
    """每个 locale tab 顶部的"今日 Summary"。优先读取真实 LLM 生成的当日综述
    （src/analysis/daily_digest.py，读 llm_cache，只读不触发调用——看板导出是静态
    快照生成，不应该在这个过程里发起网络请求）；本次 session 没有跑过那一步
    （明确要求不调用真实 LLM），所以实际总是落到 stats_fallback 分支，但导出层
    已经按"接入后即生效"的方式接好了，一旦生产环境真的跑了
    `generate_daily_digest(..., dry_run=False)` 并写入缓存，这里会自动改用真实
    LLM 结果，不需要再改代码。
    """
    cached = peek_cached_digest(conn, locale, batch_date)
    if cached is not None and cached.generated:
        return {"text": cached.daily_summary, "priority_focus": cached.priority_focus, "source": "llm"}
    return {
        "text": _build_stats_digest_fallback(locale, ov, locale_batches),
        "priority_focus": None,
        "source": "stats_fallback",
    }


def _build_stats_digest_fallback(locale: str, ov: dict, locale_batches: list[InsightRef]) -> str:
    """真实 LLM 综述缺席时的占位内容。纯粹是对已经落库的批次元数据（diff_type/
    priority/来源/是否 mock）做确定性聚合，拼成一段可读的简报文字——不是"当日
    insight"，只是统计口径的鸟瞰视角，前端会明确标注这是占位符而不是 LLM 产出。
    """
    if not locale_batches:
        return f"{locale} 本次没有产出任何分析批次（无 status=new/changed 的 campaign/product/listing/delisting 公告）。"

    real_n = sum(1 for b in locale_batches if not b.is_mock)
    mock_n = sum(1 for b in locale_batches if b.is_mock)
    sources = sorted({b.source for b in locale_batches})
    by_cat = {c: sum(1 for b in locale_batches if b.category == c) for c in CATEGORIES}
    cat_parts = "、".join(f"{CATEGORY_LABEL_ZH[c]} {n} 批" for c, n in by_cat.items() if n)

    missing_high = sum(1 for b in locale_batches if b.diff_type == "ZMX缺失" and b.priority == "高")
    missing_total = sum(1 for b in locale_batches if b.diff_type == "ZMX缺失")
    diff_total = sum(1 for b in locale_batches if b.diff_type == "ZMX玩法不同")
    same_total = sum(1 for b in locale_batches if b.diff_type == "ZMX已有")

    parts = [
        f"{locale} 本次共产出 {len(locale_batches)} 个分析批次（真实 {real_n} / 模拟 {mock_n}），"
        f"覆盖 {len(sources)} 个竞品源（{'、'.join(sources)}）。"
    ]
    if cat_parts:
        parts.append(f"按类目：{cat_parts}。")
    zmx_bits = []
    if missing_total:
        zmx_bits.append(f"ZMX缺失 {missing_total} 批（其中高优先级 {missing_high} 批）")
    if diff_total:
        zmx_bits.append(f"玩法不同 {diff_total} 批")
    if same_total:
        zmx_bits.append(f"已有对应功能 {same_total} 批")
    if zmx_bits:
        parts.append("ZMX 基线比对：" + "，".join(zmx_bits) + "。")
    if ov["region_exclusive_count"]:
        parts.append(f"另有 {ov['region_exclusive_count']} 条地区独占公告。")
    return " ".join(parts)


def build_analysis_blocks(locale: str, batches: list[InsightRef]) -> list[dict]:
    """每个 locale tab 的核心内容：按分类分组、展示已经跑好的批次分析全文
    （batch_summary + zmx_diff），不是只挑 priority=高 的一部分（那是 highlights
    的职责），这里是完整的"今天这个类目都分析出了什么"。
    """
    locale_batches = [b for b in batches if b.locale == locale]
    order = {c: i for i, c in enumerate(CATEGORIES)}
    locale_batches.sort(key=lambda b: (order.get(b.category, 99), b.is_mock, b.source))
    return [
        {
            "category": b.category,
            "source": b.source,
            "summary": b.summary,
            "zmx_diff": b.zmx_diff,
            "diff_type": b.diff_type,
            "diff_tag": DIFF_TYPE_TAG.get(b.diff_type or "", "na"),
            "priority": b.priority,
            "article_count": b.article_count,
            "is_mock": b.is_mock,
            "is_locale_derived": b.is_locale_derived,
        }
        for b in locale_batches
    ]


def build_highlights(conn: sqlite3.Connection, locale: str, batches: list[InsightRef],
                      uid_map: dict[str, InsightRef]) -> list[dict]:
    locale_batches = [b for b in batches if b.locale == locale and b.priority == "高"]
    # 真实数据优先，同优先级下按 article_count 降序，让内容更丰富的批次排前面
    locale_batches.sort(key=lambda b: (b.is_mock, -b.article_count))

    out = []
    for b in locale_batches[:HIGHLIGHTS_PER_LOCALE]:
        row = conn.execute(
            """SELECT title, post_time, status FROM announcements
               WHERE source=? AND category=? AND locale=? AND status IN ('new','changed')
               ORDER BY post_time DESC LIMIT 1""",
            (b.source, b.category, b.locale),
        ).fetchone()
        title = _clean_title(row["title"] if row else b.first_article_title)
        out.append({
            "source": b.source,
            "category": b.category,
            "title": title,
            "summary": (b.summary or "")[:160],
            "diff_type": b.diff_type,
            "diff_tag": DIFF_TYPE_TAG.get(b.diff_type or "", "na"),
            "priority": b.priority,
            "time": _format_time(row["post_time"] if row else None),
            "status": row["status"] if row else "new",
            "is_mock": b.is_mock,
            "article_count": b.article_count,
        })
    return out


def build_cex_table(conn: sqlite3.Connection, locale: str,
                     uid_map: dict[str, InsightRef]) -> tuple[dict[str, list[dict]], dict[str, int]]:
    result: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for category in CATEGORIES:
        total = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE locale=? AND category=? AND source != ?",
            (locale, category, BASELINE_SOURCE),
        ).fetchone()[0]
        counts[category] = total

        rows = _dict_rows(
            conn.execute(
                """SELECT uid, source, title, post_time, status FROM announcements
                   WHERE locale=? AND category=? AND source != ?
                   ORDER BY post_time DESC LIMIT ?""",
                (locale, category, BASELINE_SOURCE, CEX_ROWS_PER_CATEGORY),
            )
        )
        out = []
        for r in rows:
            insight = uid_map.get(r["uid"])
            out.append({
                "source": r["source"],
                "title": _clean_title(r["title"]),
                "time": _format_time(r["post_time"]),
                "status": r["status"],
                "diff_tag": DIFF_TYPE_TAG.get(insight.diff_type, "na") if insight and insight.diff_type else "pending",
                "is_mock": insight.is_mock if insight else False,
            })
        result[category] = out
    return result, counts


def build_full_archive(conn: sqlite3.Connection, uid_map: dict[str, InsightRef]) -> dict:
    """区域 tab 只展示最新一批（见 CEX_ROWS_PER_CATEGORY），完整历史数据集中放在
    「全量」tab，前端在这一份扁平数组上做 region/时间范围/来源/分类筛选 + 分页，
    不再需要为每种筛选组合单独查库——数据量级（几千行 × 数个字段）完全在静态
    JSON 一次性下发的合理范围内。
    """
    rows = _dict_rows(
        conn.execute(
            f"""SELECT uid, source, locale, category, title, post_time, status
                FROM announcements WHERE source != '{BASELINE_SOURCE}'
                ORDER BY post_time DESC"""
        )
    )
    out = []
    for r in rows:
        insight = uid_map.get(r["uid"])
        post_time = r["post_time"] or ""
        out.append({
            "source": r["source"],
            "locale": r["locale"],
            "category": r["category"] or "other",
            "title": _clean_title(r["title"]),
            "date": post_time[:10] if post_time else None,
            "time": _format_time(post_time),
            "status": r["status"],
            "diff_tag": DIFF_TYPE_TAG.get(insight.diff_type, "na") if insight and insight.diff_type else "pending",
            "is_mock": insight.is_mock if insight else False,
        })
    dates = [r["date"] for r in out if r["date"]]
    return {
        "rows": out,
        "total": len(out),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def build_activity_ranking(conn: sqlite3.Connection, as_of_date: str) -> list[dict]:
    rows = _dict_rows(
        conn.execute(
            f"""SELECT source, COUNT(*) as n FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND date(fetched_at) = ?
                GROUP BY source ORDER BY n DESC""",
            (as_of_date,),
        )
    )
    active = {r["source"]: r["n"] for r in rows}
    out = []
    for source in COMPETITORS:
        out.append({"source": source, "count": active.get(source, 0)})
    out.sort(key=lambda x: -x["count"])
    max_count = max((x["count"] for x in out), default=0) or 1
    for x in out:
        x["pct"] = round(100 * x["count"] / max_count, 1)
    return out


def build_category_distribution(conn: sqlite3.Connection, as_of_date: str) -> list[dict]:
    rows = _dict_rows(
        conn.execute(
            f"""SELECT source, category, COUNT(*) as n FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND date(fetched_at) = ?
                  AND category IS NOT NULL
                GROUP BY source, category""",
            (as_of_date,),
        )
    )
    by_source: dict[str, dict[str, int]] = {s: {c: 0 for c in [*CATEGORIES, "other"]} for s in COMPETITORS}
    for r in rows:
        if r["source"] in by_source and r["category"] in by_source[r["source"]]:
            by_source[r["source"]][r["category"]] = r["n"]
    return [{"source": s, **counts} for s, counts in by_source.items()]


def build_baseline_by_locale(conn: sqlite3.Connection) -> dict[str, dict]:
    """Zoomex 是基线不是竞品，EN-Asia locale 目前只有 Zoomex 配置（见 CLAUDE.md
    「竞品与语言范围」表），没有任何竞品数据可看，这个 tab 只能展示基线自身。"""
    rows = _dict_rows(
        conn.execute(
            f"""SELECT locale, category, COUNT(*) as n FROM announcements
                WHERE source = '{BASELINE_SOURCE}' AND category IS NOT NULL
                GROUP BY locale, category"""
        )
    )
    out: dict[str, dict] = {loc: {"total": 0, "categories": {}} for loc in BASELINE_LOCALES}
    for r in rows:
        if r["locale"] in out:
            out[r["locale"]]["categories"][r["category"]] = r["n"]
            out[r["locale"]]["total"] += r["n"]
    return out


def build_zmx_missing_summary(batches: list[InsightRef]) -> list[dict]:
    hits = [b for b in batches if b.diff_type == "ZMX缺失" and b.priority == "高"]
    hits.sort(key=lambda b: (b.is_mock, -b.article_count))
    return [
        {
            "source": b.source,
            "category": b.category,
            "locale": b.locale,
            "title": b.first_article_title or "(无标题)",
            "is_mock": b.is_mock,
        }
        for b in hits[:ZMX_MISSING_SUMMARY_CAP]
    ]


def build_region_table(conn: sqlite3.Connection, as_of_date: str) -> list[dict]:
    rows = _dict_rows(
        conn.execute(
            f"""SELECT group_id, source, locale, title, is_region_exclusive, post_time
                FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND date(fetched_at) = ?
                  AND group_id IS NOT NULL
                ORDER BY post_time DESC""",
            (as_of_date,),
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
    out = []
    for g in picked[:REGION_TABLE_CAP]:
        out.append({
            "title": g["title"][:70],
            "source": g["source"],
            "locales": sorted(g["locales"]),
            "exclusive": g["exclusive"],
        })
    return out


def build_dashboard_data(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    as_of_date = _resolve_as_of_date(conn)
    uid_map, batches = _load_insight_index(conn)

    overview = {locale: build_overview(conn, as_of_date, locale, uid_map) for locale in ALL_LOCALES}
    overview["global"] = build_overview(conn, as_of_date, None, uid_map)

    highlights = {locale: build_highlights(conn, locale, batches, uid_map) for locale in ALL_LOCALES}
    analysis_blocks = {locale: build_analysis_blocks(locale, batches) for locale in ALL_LOCALES}
    daily_digest = {
        locale: build_daily_digest(conn, locale, as_of_date, overview[locale],
                                    [b for b in batches if b.locale == locale])
        for locale in ALL_LOCALES
    }
    cex_tables: dict[str, dict] = {}
    cex_counts: dict[str, dict] = {}
    for locale in ALL_LOCALES:
        cex_tables[locale], cex_counts[locale] = build_cex_table(conn, locale, uid_map)

    zoomex_total = conn.execute(
        f"SELECT COUNT(*) FROM announcements WHERE source = '{BASELINE_SOURCE}'"
    ).fetchone()[0]

    source_status = {}
    for source, locs in COMPETITORS.items():
        n_today = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE source=? AND date(fetched_at)=?",
            (source, as_of_date),
        ).fetchone()[0]
        n_total = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE source=?", (source,)
        ).fetchone()[0]
        source_status[source] = {
            "locales": locs,
            "today": n_today,
            "total": n_total,
            "active": n_total > 0,
        }

    mock_insight_count = sum(1 for b in batches if b.is_mock)

    data = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "as_of_date": as_of_date,
            "db_path": str(Path(db_path).name),
            "insights_total": len(batches),
            "insights_mock": mock_insight_count,
            "zoomex_baseline_total": zoomex_total,
        },
        "sources": source_status,
        "locales": ALL_LOCALES,
        "overview": overview,
        "daily_digest": daily_digest,
        "analysis_blocks": analysis_blocks,
        "highlights": highlights,
        "cex_tables": cex_tables,
        "cex_counts": cex_counts,
        "activity_ranking": build_activity_ranking(conn, as_of_date),
        "category_distribution": build_category_distribution(conn, as_of_date),
        "zmx_missing_summary": build_zmx_missing_summary(batches),
        "region_table": build_region_table(conn, as_of_date),
        "zoomex_by_locale": build_baseline_by_locale(conn),
        "archive": build_full_archive(conn, uid_map),
    }
    conn.close()
    return data


def export(db_path: str, out_path: str) -> dict:
    data = build_dashboard_data(db_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
