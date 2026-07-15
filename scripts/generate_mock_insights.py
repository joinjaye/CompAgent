"""
Phase 7（可视化看板）专用：为 data/dashboard_demo.db 里缺失 insights 的
(source, category, locale) 组合生成模拟批次分析。

背景：本 session 明确不调用任何真实 LLM（既不碰 openai_http，也不碰
cursor_agent 后端），但看板要展示"全局视角""ZMX 差异""重点公告"这些依赖
insights 表的模块，7 条真实 insights（全部是 Bitunix）远不足以撑起一个像样的
演示。这个脚本产出的是结构上合法、但内容是模板拼接的**模拟数据**，不是真实
分析结论——每一条都用 prompt_version 加 "-mock" 后缀、llm_tokens_used=-1
这两个哨兵值标记来源，src/dashboard/export_data.py 读到这两个信号时会在
看板 UI 上显式标出"模拟"角标，不会把它跟真实分析结果混在一起呈现而不做区分。

字段级别的诚实原则：
- articles_analysis 的结构化字段（token_symbol/market_type/launch_time 等）
  用简单的正则/关键词从真实标题里提取，提取不到一律留 null（照抄 prompts.py
  本身"提取不到填 null，禁止编造"的规则，不因为是 mock 就允许编造）。
- zmx_diff/diff_type/priority 是没有真实检索作为依据的模拟判断，不编造具体
  的 [Z1]/[Z2] 证据编号（真实 pipeline 只有在 evidence_indices 非空时才会
  引用编号），zmx_evidence_uids 恒为空数组，文案里明确写"模拟数据，非真实
  比对结果"。
- 每个 (source, category, locale) 批次只生成一条 insights 行，跟真实 Phase 4
  设计一致；如果该批次公告数很多（如 Lbank campaign/EN 有 160 条），
  articles_analysis 只对其中最新 8 条做结构化展开（跟真实 prompts.py"每条
  都展开"的设计不同，是本脚本为控制体量做的取舍），但 related_uids 仍然
  包含全部真实 uid，article_count 也是真实总数，不缩水统计口径。
"""
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.batch import compute_batch_id  # noqa: E402

DEMO_DB = ROOT / "data" / "dashboard_demo.db"
BATCH_DATE = "2026-07-15"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
SAMPLE_CAP = 8

CATEGORY_LABEL = {
    "campaign": "活动 campaign",
    "product": "产品 product",
    "listing": "上币 listing",
    "delisting": "下架 delisting",
}

TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}(?:\.\d+)?(?:/[A-Z0-9]{2,10})?USD[TC]?)\b")


def extract_token_symbol(title: str) -> str | None:
    m = TOKEN_RE.search(title or "")
    return m.group(1) if m else None


def guess_market_type(title: str) -> str:
    t = (title or "").lower()
    is_futures = any(k in t for k in ("perpetual", "futures", "contract"))
    is_spot = "spot" in t
    if is_futures and is_spot:
        return "两者均有"
    if is_futures:
        return "合约"
    if is_spot:
        return "现货"
    return "不明"


def guess_delist_reason(title: str) -> str | None:
    t = (title or "").lower()
    if "maintenance" in t or "upgrade" in t:
        return "维护升级"
    if "liquidity" in t:
        return "流动性不足"
    if "compliance" in t or "regulat" in t:
        return "合规原因"
    return None


def stable_choice(seed: str, options: list[str], weights: list[float]) -> str:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    r = (h % 10_000) / 10_000
    cum = 0.0
    for opt, w in zip(options, weights):
        cum += w
        if r < cum:
            return opt
    return options[-1]


def pick_diff_type_and_priority(batch_id: str, category: str) -> tuple[str, str]:
    if category == "delisting":
        priority = stable_choice(batch_id + "p", ["高", "中", "低"], [0.2, 0.5, 0.3])
        return "不适用", priority

    options = ["ZMX已有", "ZMX缺失", "混合"] if category == "listing" else [
        "ZMX已有", "ZMX缺失", "ZMX玩法不同", "混合",
    ]
    weights = [0.4, 0.3, 0.3] if category == "listing" else [0.35, 0.28, 0.2, 0.17]
    diff_type = stable_choice(batch_id + "d", options, weights)

    if diff_type == "ZMX缺失":
        priority = stable_choice(batch_id + "p", ["高", "中", "低"], [0.45, 0.4, 0.15])
    elif diff_type == "ZMX已有":
        priority = stable_choice(batch_id + "p", ["高", "中", "低"], [0.05, 0.35, 0.6])
    else:
        priority = stable_choice(batch_id + "p", ["高", "中", "低"], [0.15, 0.55, 0.3])
    return diff_type, priority


def synth_article(row: dict, category: str) -> dict:
    uid, title, status = row["uid"], row["title"] or "", row["status"]
    base = {"uid": uid, "title": title}
    if category == "campaign":
        base.update({
            "mechanics": f"（模拟）依据标题判断玩法方向，细节未经真实模型解析：{title[:60]}",
            "time_window": None,
            "target_users": "（模拟）该地区活跃交易用户",
            "change_summary": "（模拟）规则或奖励发生调整，具体以原文为准" if status == "changed" else None,
        })
    elif category == "product":
        base.update({
            "feature_description": f"（模拟）依据标题判断功能方向：{title[:60]}",
            "affected_users": "（模拟）使用该功能的相关用户群体",
            "change_summary": "（模拟）功能参数发生调整，具体以原文为准" if status == "changed" else None,
        })
    elif category == "listing":
        base.update({
            "token_symbol": extract_token_symbol(title),
            "market_type": guess_market_type(title),
            "launch_time": row["post_time"],
            "project_brief": None,
        })
    else:  # delisting
        base.update({
            "token_symbol": extract_token_symbol(title),
            "market_type": guess_market_type(title),
            "delist_time": row["post_time"],
            "reason": guess_delist_reason(title),
        })
    return base


def build_mock_insight(conn: sqlite3.Connection, source: str, category: str, locale: str) -> dict:
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT uid, title, content, post_time, status FROM announcements
               WHERE source=? AND category=? AND locale=? AND status IN ('new','changed')
               ORDER BY post_time DESC""",
            (source, category, locale),
        )
    ]
    if not rows:
        return {}

    batch_id = compute_batch_id(source, category, locale, BATCH_DATE)
    related_uids = [r["uid"] for r in rows]
    sample_rows = rows[:SAMPLE_CAP]
    articles_analysis = [synth_article(r, category) for r in sample_rows]
    diff_type, priority = pick_diff_type_and_priority(batch_id, category)

    sample_titles = "；".join(r["title"] for r in sample_rows[:3] if r["title"])
    label = CATEGORY_LABEL[category]
    truncated_note = (
        f"（仅对最新 {SAMPLE_CAP} 条做结构化展开，其余 {len(rows) - SAMPLE_CAP} 条计入统计未逐条列出）"
        if len(rows) > SAMPLE_CAP
        else ""
    )
    summary = (
        f"[模拟数据] 本批次 {source}/{locale} 在 {label} 类目下共 {len(rows)} 条公告"
        f"{truncated_note}，样例包括：{sample_titles}。本条为可视化演示占位内容，"
        f"非真实 LLM 分析结果。"
    )

    if category == "delisting":
        zmx_diff = None
    else:
        priority_reason = {
            "高": "模拟判定为高优先级，用于演示高优先级样式",
            "中": "模拟判定为中等优先级",
            "低": "模拟判定为低优先级",
        }[priority]
        zmx_diff = (
            f"[模拟数据] 未执行真实 ZMX 基线检索，diff_type 仅为演示用途的模拟赋值"
            f"（{diff_type}），不代表真实比对结论。\n优先级依据：{priority_reason}（模拟）"
        )

    return {
        "id": batch_id,
        "batch_date": BATCH_DATE,
        "source": source,
        "category": category,
        "locale": locale,
        "article_count": len(rows),
        "related_uids": json.dumps(related_uids, ensure_ascii=False),
        "is_locale_derived": 0,
        "derived_from_id": None,
        "summary": summary,
        "articles_analysis": json.dumps(articles_analysis, ensure_ascii=False),
        "zmx_diff": zmx_diff,
        "diff_type": diff_type,
        "priority": priority,
        "zmx_evidence_uids": "[]",
        "prompt_version": f"{category}-v1-mock",
        "llm_tokens_used": -1,
        "created_at": NOW,
        "updated_at": NOW,
    }


def main() -> None:
    conn = sqlite3.connect(str(DEMO_DB))
    conn.row_factory = sqlite3.Row

    existing = {
        (r["source"], r["category"], r["locale"])
        for r in conn.execute("SELECT source, category, locale FROM insights")
    }
    combos = [
        tuple(r)
        for r in conn.execute(
            """SELECT DISTINCT source, category, locale FROM announcements
               WHERE category IS NOT NULL AND category != 'other'
                 AND source != 'Zoomex' AND status IN ('new','changed')
               ORDER BY source, category, locale"""
        )
    ]
    gaps = [c for c in combos if c not in existing]

    inserted = 0
    for source, category, locale in gaps:
        insight = build_mock_insight(conn, source, category, locale)
        if not insight:
            continue
        cols = ", ".join(insight.keys())
        placeholders = ", ".join("?" for _ in insight)
        conn.execute(
            f"INSERT INTO insights ({cols}) VALUES ({placeholders})", tuple(insight.values())
        )
        inserted += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    mock = conn.execute(
        "SELECT COUNT(*) FROM insights WHERE llm_tokens_used = -1"
    ).fetchone()[0]
    conn.close()
    print(f"gaps found: {len(gaps)}, inserted: {inserted}")
    print(f"insights total: {total} (mock: {mock}, real: {total - mock})")


if __name__ == "__main__":
    main()
