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
from datetime import datetime, timedelta, timezone
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

# 每个 category 里"这条公告到底讲了什么"的描述性字段名不同。campaign/product 的
# 字段名是 Phase②（staged.py 接入）Stage1 事实抽取产出的字段（mechanism/feature，
# 取代了 Phase②之前的 mechanics/feature_description——旧字段名仍在
# _load_article_index 里做兼容读取，不是这里要处理的事）；listing/delisting 是更早
# 的历史遗留字段名，这两个类目现在完全不产出 insights（Phase 4 v3 政策收紧后
# 只分析 campaign/product），只在展示极老的历史数据时可能用到。
_DESCRIPTIVE_FIELD_BY_CATEGORY = {
    "campaign": "mechanism",
    "product": "feature",
    "listing": "project_brief",
    "delisting": "reason",
}

OVERVIEW_HIGHLIGHTS_CAP = 12


def _dedupe_business_rows(rows: list[dict]) -> list[dict]:
    """Dashboard 业务计数去重。

    group_id 仍是主键；但 Phemex 等源的同一公告会在不同 locale 分配不同 article_id，
    从而产生不同 group_id。对这种情况再用 source + 规范化标题兜底，避免同标题 EN/FR
    在 Daily Summary / Highlights 重复出现。标题为空时不启用兜底，避免误合并。
    """
    seen_groups: set[str] = set()
    seen_title_locales: dict[tuple[str, str], set[str]] = {}
    result = []
    for row in rows:
        group_id = row.get("group_id") or row["uid"]
        normalized_title = re.sub(r"\s+", " ", (row.get("title") or "").strip()).casefold()
        title_key = (row["source"], normalized_title) if normalized_title else None
        title_is_cross_locale_duplicate = (
            title_key is not None
            and title_key in seen_title_locales
            and row.get("locale") not in seen_title_locales[title_key]
        )
        if group_id in seen_groups or title_is_cross_locale_duplicate:
            continue
        seen_groups.add(group_id)
        if title_key is not None:
            seen_title_locales.setdefault(title_key, set()).add(row.get("locale"))
        result.append(row)
    return result


def _dict_rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _clean_title(title: Optional[str]) -> str:
    """部分源（如 BingX）的原始标题里留有未解码的 HTML 实体（如 "&amp;"），
    是采集层的既有数据质量问题，不在本次任务范围内改采集代码；这里只做无损的
    实体解码用于展示，不改变语义，避免看板上把 "&" 显示成 "&amp;"。"""
    if not title:
        return "(无标题)"
    return html.unescape(re.sub(r"<[^>]+>", "", title)).strip() or "(无标题)"


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
_AMOUNT_RE = re.compile(
    r"(?:[$€]\s?[\d,.]+(?:\s?(?:K|M))?|[\d,.]+\s?(?:USDT|USDC|BTC|ETH|WXT|STABLE|EDGE))",
    re.IGNORECASE,
)

def _product_category(title: Optional[str], feature: Optional[str]) -> str:
    text = f"{title or ''} {feature or ''}".casefold()
    rules = (
        ("Copy Trading", ("copy trading", "copy trade", "跟单")),
        ("Card & Payment", (" card", "payment", "支付", "消费卡")),
        ("Security", ("proof of reserve", "merkle", "insurance", "custody", "储备金", "保险", "托管")),
        ("Earn", ("earn", "staking", "apr", "apy", "理财", "质押")),
        ("API", (" api", "websocket")),
        ("Bot", ("trading bot", "grid bot", "strategy trading", "机器人", "策略交易")),
        ("Wallet", ("wallet", "deposit", "withdraw", "钱包", "充提")),
        ("Convert", ("convert", "swap", "兑换", "置换")),
        ("Institutional", ("institutional", "broker", "vip", "机构")),
        ("Trading", ("perpetual", "futures", "leverage", "risk limit", "tick size", "trading", "交易", "合约", "杠杆")),
    )
    for label, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return label
    return "Others"


def _product_update_kind(title: Optional[str], status: str) -> str:
    text = (title or "").casefold()
    if any(k in text for k in ("reminder", "notice", "提醒", "通知")):
        return "Operational Notice"
    if status == "new" and not any(k in text for k in ("update", "adjust", "remove", "upgrade", "调整", "移除", "升级")):
        return "New Product"
    if any(k in text for k in ("remove", "disable", "sunset", "移除", "下线", "停止")):
        return "Feature Removed"
    if any(k in text for k in ("ui", "interface", "界面")):
        return "UI Updated"
    if any(k in text for k in ("performance", "latency", "speed", "性能", "延迟")):
        return "Performance Updated"
    if any(k in text for k in ("rule", "limit", "fee", "risk", "tick size", "adjust", "规则", "限额", "费率", "风控", "调整")):
        return "Rule Updated"
    return "Feature Added" if status == "new" else "Rule Updated"


def _campaign_type(title: Optional[str], mechanics: Optional[str]) -> Optional[str]:
    text = f"{title or ''} {mechanics or ''}".casefold()
    rules = (
        ("邀请/推荐", ("invite", "referral", "推荐", "邀请")),
        ("交易竞赛", ("trading competition", "leaderboard", "交易赛", "交易竞赛")),
        ("入金激励", ("deposit", "入金", "充值")),
        ("空投/Launch", ("airdrop", "we-launch", "launchpool", "空投")),
        ("预测活动", ("predict", "prediction", "bull or bear", "预测")),
        ("积分活动", ("points", "积分")),
    )
    for label, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return label
    return None


def _reward_summary(mechanics: Optional[str]) -> Optional[str]:
    """从 LLM 已提取的 mechanics 中摘取明确金额，不读取原文、不新增语义判断。"""
    if not mechanics:
        return None
    values = []
    for match in _AMOUNT_RE.findall(mechanics):
        value = re.sub(r"\s+", " ", match.strip())
        if value not in values:
            values.append(value)
    return " / ".join(values[:3]) or None


def _split_time_window(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    parts = re.split(r"\s*(?:~|→|至)\s*", value, maxsplit=1)
    if len(parts) != 2:
        return None, None
    def clean(part: str) -> Optional[str]:
        part = part.strip()
        return None if not part or part.casefold() in {"null", "none", "unknown", "?"} else part

    return clean(parts[0]), clean(parts[1])


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


def _format_zmx_reward(amount: object, token: object, form: object) -> Optional[str]:
    joined = " ".join(str(p) for p in (amount, token) if p is not None and p != "") or None
    form_text = str(form) if form is not None and form != "" else None
    if joined and form_text:
        return f"{joined}（{form_text}）"
    return joined or form_text


def _load_article_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """uid -> 该条公告在 Phase 4 分析里产出的逐条字段。

    每个 insights 行的 articles_analysis 是一个 JSON 数组（每篇公告的结构化分析），
    这里展开成以 uid 为 key 的扁平索引，供后面按 announcements 逐行 join 用。

    字段名对应 Phase②（staged.py 接入）之后 run.py 产出的新形状（event_type/
    mechanism/feature/diff_type/diff_detail/zmx_counterpart_uids/priority/
    change_kind——不再有 action_type/owner/follow_up/listing_kind，这几个字段
    AI 不再产出，follow_up 留给 Phase⑤ 的确定性规则填充）。老数据 / 校验失败的
    批次（articles_analysis 为 NULL、不是合法 JSON 数组，或是 Phase②之前旧形状
    产出的字段名）会被静默跳过或取不到值，不抛异常——找不到的字段，调用方一律用
    .get() 取默认值 None，前端渲染中性默认，如实反映"这条还没有（新形状）逐条
    分析结果"，不是 bug。
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
                "diff_detail": a.get("diff_detail"),
                "priority": a.get("priority"),
                "priority_reason": a.get("priority_reason"),
                "action_type": a.get("action_type"),
                "owner": a.get("owner"),
                "follow_up": a.get("follow_up"),
                "change_kind": a.get("change_kind"),
                "listing_kind": a.get("listing_kind"),
                "description": a.get(desc_field) if desc_field else None,
                "mechanism_type": a.get("mechanism_type"),
                "zmx_counterpart_uids": a.get("zmx_counterpart_uids") or [],
                "reward_range": a.get("reward_range") or _format_zmx_reward(
                    (a.get("reward") or {}).get("amount"), (a.get("reward") or {}).get("currency"),
                    (a.get("reward") or {}).get("type"),
                ),
                "target_users": (
                    ", ".join(a["target_users"]) if isinstance(a.get("target_users"), list) else a.get("target_users")
                ),
                "time_window": a.get("time_window"),
                "start_date": a.get("start_at") or a.get("start_date"),
                "end_date": a.get("end_at") or a.get("end_date"),
                "feature": a.get("feature") or a.get("feature_description"),
                "token_symbol": a.get("token_symbol"),
                "launch_time": a.get("launch_time"),
                "is_mock": is_mock,
                "is_locale_derived": is_locale_derived,
            }
    return index


def _load_zmx_catalog_index(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """(category, mechanism_type) -> Zoomex 能力目录条目。在导出时现查，不在写入
    articles_analysis 时冗余存一份——目录可能在竞品分析之后单独重新 rollup，导出
    应该反映目录的当前状态，不是分析当时的快照（SQLite 是唯一真相源）。
    """
    rows = _dict_rows(conn.execute(
        "SELECT category, mechanism_type, exists_flag, capability_desc, typical_reward FROM zmx_catalog_entry"
    ))
    return {
        (r["category"], r["mechanism_type"]): {
            "exists_flag": r["exists_flag"],
            "capability_desc": r["capability_desc"],
            "typical_reward": r["typical_reward"],
        }
        for r in rows
    }


def _load_zmx_counterpart_index(conn: sqlite3.Connection, uids: set[str]) -> dict[str, dict]:
    """source_uid -> 一条具体的 Zoomex 对照示例（标题/链接/摘要/奖励），供 Detail
    面板的两栏对比展示用。只查竞品分析实际引用过的 uid（zmx_counterpart_uids），
    不是整张表。"""
    if not uids:
        return {}
    placeholders = ",".join("?" * len(uids))
    rows = _dict_rows(conn.execute(
        f"""SELECT s.source_uid, s.core_summary, s.reward_form, s.reward_amount, s.reward_token,
                   a.title, a.url
            FROM zmx_summary s JOIN announcements a ON a.uid = s.source_uid
            WHERE s.source_uid IN ({placeholders})""",
        list(uids),
    ))
    return {
        r["source_uid"]: {
            "title": _clean_title(r["title"]),
            "url": r["url"],
            "core_summary": r["core_summary"],
            "reward_range": _format_zmx_reward(r["reward_amount"], r["reward_token"], r["reward_form"]),
        }
        for r in rows
    }


def _merge_localized_rows(rows: list[dict]) -> list[dict]:
    """One business event per row; locales/URLs remain available as parallel variants."""
    buckets: list[list[dict]] = []
    for row in rows:
        normalized_title = re.sub(r"\s+", " ", (row.get("title") or "").strip()).casefold()
        matched = None
        for bucket in buckets:
            if bucket[0]["source"] != row["source"]:
                continue
            same_group = row.get("group_id") and any(r.get("group_id") == row["group_id"] for r in bucket)
            same_title_cross_locale = normalized_title and any(
                re.sub(r"\s+", " ", (r.get("title") or "").strip()).casefold() == normalized_title
                and r.get("locale") != row.get("locale")
                for r in bucket
            )
            if same_group or same_title_cross_locale:
                matched = bucket
                break
        if matched is None:
            buckets.append([row])
        else:
            matched.append(row)

    merged = []
    locale_order = {"EN": 0, "FR": 1, "VN": 2, "ID": 3, "EN-Asia": 4}
    for variants in buckets:
        variants.sort(key=lambda r: (locale_order.get(r.get("locale"), 9), -(len(r.get("description") or ""))))
        representative = dict(variants[0])
        representative["markets"] = sorted({r["locale"] for r in variants}, key=lambda x: locale_order.get(x, 9))
        representative["localized_variants"] = [
            {"locale": r["locale"], "title": r["title"], "url": r["url"]}
            for r in variants
        ]
        representative["locale"] = representative["markets"][0]
        merged.append(representative)
    return merged


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


def _derive_follow_up(rows: list[dict]) -> None:
    """Phase⑤：Follow-up 是确定性规则派生，不是 AI 产出——Phase②起 run.py 不再让
    LLM 输出 action_type/owner/follow_up，这里用同一批公告内的 diff_type +
    mechanism_type 出现频率做规则判断（原地修改 rows，跟 _push_candidate 的用法
    一致）：
    - diff_type=ZMX缺失 且该 mechanism_type 本批被 ≥2 个不同竞品触及（行业共性
      趋势，不是单一竞品的孤例）→ 建议评估跟进
    - diff_type=ZMX玩法不同 → 建议观察差异
    - diff_type=ZMX已有 → 无需关注
    - 其余（不适用/混合，或已有值——理论上 Phase②后不会有，防御性保留）不产出
    """
    sources_by_mechanism: dict[str, set[str]] = {}
    for r in rows:
        mt = r.get("mechanism_type")
        if mt:
            sources_by_mechanism.setdefault(mt, set()).add(r["source"])
    for r in rows:
        if r.get("follow_up"):
            continue
        diff_type = r.get("diff_type")
        mt = r.get("mechanism_type")
        if diff_type == "ZMX缺失" and mt and len(sources_by_mechanism.get(mt, ())) >= 2:
            r["follow_up"] = "建议评估跟进"
        elif diff_type == "ZMX玩法不同":
            r["follow_up"] = "建议观察差异"
        elif diff_type == "ZMX已有":
            r["follow_up"] = "无需关注"


def build_category_section(
    conn: sqlite3.Connection, category: str, as_of_date: str, article_index: dict[str, dict],
    zmx_catalog_index: Optional[dict[tuple[str, str], dict]] = None,
    zmx_counterpart_index: Optional[dict[str, dict]] = None,
    latest_only: bool = True,
) -> list[dict]:
    """最新一批（as_of_date 当天、status IN new/changed）某个 category 的逐条公告，
    一行一篇公告，按 uid join 到 Phase 4 的逐条分析结果（找不到就是中性默认）。

    这是 campaign/product/listing/delisting 四个 category 共用的构建函数——listing
    对外展示的 section 会把 delisting 的结果也拼进去（调用方负责拼接，这里只管单个
    category），"other" 也可以传进来单独查（只用于 Overview 的 Announcement chip
    计数，不作为独立的顶层导出 section）。

    Phase③：campaign/product 都会带上 Zoomex 能力目录对照字段（zmx_exists/
    zmx_mechanism_type/zmx_capability_desc/zmx_counterpart/comparison_status），
    不再只有 product 才有——这是 Phase②起 campaign 也真正走 Stage1/Stage3 分析
    的直接结果。旧版基于全量 Zoomex 历史做确定性词项重叠匹配的
    `_load_product_baseline`/`_product_baseline_candidates`（`zmx_candidates`/
    `comparison_status in (candidate_found, baseline_unmatched)`）已整体退休：
    那套机制是在真实 Stage1/Stage3 管线尚未覆盖 campaign、且 product 的
    Zoomex 基线还是有 90 天窗口限制时的权宜之计，继续跟新管线的真实
    diff_type/zmx_counterpart 同屏显示只会造成两套结论互相矛盾。
    """
    latest_clause = "AND date(fetched_at) = ? AND status IN ('new', 'changed')" if latest_only else ""
    params = (category, as_of_date) if latest_only else (category,)
    rows = _dict_rows(conn.execute(
        f"""SELECT uid, group_id, source, locale, title, post_time, update_time, status,
                   url, is_region_exclusive
            FROM announcements
            WHERE source != '{BASELINE_SOURCE}' AND category = ? {latest_clause}
            ORDER BY post_time DESC""",
        params,
    ))
    zmx_catalog_index = zmx_catalog_index or {}
    zmx_counterpart_index = zmx_counterpart_index or {}
    out = []
    for r in rows:
        # Listing/Delisting 自本版起不做任何 LLM 分析或 ZMX 比较；即使数据库里
        # 留有旧版本 insight，也不得继续展示过期的 priority/diff/follow_up。
        art = {} if category in ("listing", "delisting") else article_index.get(r["uid"], {})
        start_date, end_date = _split_time_window(art.get("time_window"))
        description = art.get("description")
        item = {
            "uid": r["uid"],
            "group_id": r["group_id"],
            "source": r["source"],
            "locale": r["locale"],
            "category": category,
            "title": _clean_title(r["title"]),
            "post_time": r["post_time"],
            "update_time": r["update_time"],
            "status": r["status"],
            "url": r["url"],
            "is_region_exclusive": bool(r["is_region_exclusive"]),
            "description": description,
            "mechanism_type": art.get("mechanism_type") or (
                _campaign_type(r["title"], description) if category == "campaign" else None
            ),
            "reward_range": art.get("reward_range") or (
                _reward_summary(description) if category == "campaign" else None
            ),
            "target_users": art.get("target_users"),
            "start_date": art.get("start_date") or start_date,
            "end_date": art.get("end_date") or end_date,
            "feature": art.get("feature"),
            "token_symbol": art.get("token_symbol"),
            "launch_time": art.get("launch_time"),
            "diff_type": art.get("diff_type"),
            "diff_tag": DIFF_TYPE_TAG.get(art.get("diff_type") or "", "na"),
            "diff_detail": art.get("diff_detail"),
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
        }
        if category in ("campaign", "product"):
            mechanism_type = art.get("mechanism_type")
            catalog_entry = zmx_catalog_index.get((category, mechanism_type)) if mechanism_type else None
            counterpart_uid = (art.get("zmx_counterpart_uids") or [None])[0]
            item["zmx_mechanism_type"] = mechanism_type
            item["zmx_exists"] = catalog_entry["exists_flag"] if catalog_entry else None
            item["zmx_capability_desc"] = catalog_entry["capability_desc"] if catalog_entry else None
            item["zmx_counterpart"] = zmx_counterpart_index.get(counterpart_uid) if counterpart_uid else None
            # analyzed = 这条公告有真实的 Stage1/Stage3 逐条分析结果（不管结论是不是
            # "不适用"）；pending = 还没被任何一次分析运行覆盖过，不是"确认没有差异"。
            item["comparison_status"] = "analyzed" if art else "pending"
        if category == "product":
            item["product_category"] = _product_category(r["title"], art.get("feature") or description)
            item["update_kind"] = _product_update_kind(r["title"], r["status"])
        out.append(item)
    return out


def build_overview(
    as_of_date: str,
    campaign_rows: list[dict],
    product_rows: list[dict],
    listing_only_rows: list[dict],
    delisting_rows: list[dict],
    other_rows: list[dict],
) -> dict:
    """构建去重后的 Daily Summary、按业务价值排序的 Highlights 和 Daily Insight。"""

    def chip_from_rows(rows: list[dict]) -> dict:
        values = _dedupe_business_rows(rows)
        count_new = sum(1 for r in values if r["status"] == "new")
        count_changed = sum(1 for r in values if r["status"] == "changed")
        diff_breakdown = {"missing": 0, "diff": 0, "same": 0, "mixed": 0, "na": 0}
        for r in values:
            diff_breakdown[r["diff_tag"]] = diff_breakdown.get(r["diff_tag"], 0) + 1
        return {
            "today": len(values),
            "count_new": count_new,
            "count_changed": count_changed,
            "diff_breakdown": diff_breakdown,
        }

    chips = {
        "campaign": chip_from_rows(campaign_rows),
        "product": chip_from_rows(product_rows),
        "listing": chip_from_rows(listing_only_rows),
        "announcement": chip_from_rows(delisting_rows + other_rows),
    }

    def business_priority(r: dict) -> str:
        # 首次抓到的历史补录仍保持 status=new，但不能伪装成“今天新活动”进入 P1。
        # 30 天只用于展示层降噪，不回写事实层状态。
        try:
            published = datetime.fromisoformat((r.get("post_time") or "").replace("Z", "+00:00"))
            as_of = datetime.fromisoformat(f"{as_of_date}T00:00:00+00:00")
            is_stale_backfill = published < as_of - timedelta(days=30)
        except ValueError:
            is_stale_backfill = False
        if r["status"] == "new" and is_stale_backfill:
            return "P7"
        if r["category"] == "campaign":
            if r["status"] == "new":
                return "P1"
            if r.get("change_kind") == "rule":
                return "P2"
            if r.get("change_kind") == "reward":
                return "P3"
        if r["category"] == "product":
            title = (r.get("title") or "").casefold()
            update_markers = (
                "update", "upgrade", "adjust", "remove", "reminder", "maintenance",
                "更新", "升级", "调整", "移除", "维护",
            )
            # status=new 只表示首次抓到；标题明确为调整/升级时仍应归 P5，而不是误称新产品。
            return "P5" if r["status"] == "changed" or any(x in title for x in update_markers) else "P4"
        if r["category"] in ("listing", "delisting"):
            return "P6"
        return "P7"

    all_rows = campaign_rows + product_rows + listing_only_rows + delisting_rows + other_rows
    # 多语言去重后，严格按业务规则选全局 Top 5、每个竞品最多 2 条。
    # 同一规则层级才用发布时间倒序；不读取 LLM priority/diff_type 参与排序。
    highlights_pool = _dedupe_business_rows(all_rows)
    for r in highlights_pool:
        r["business_priority"] = business_priority(r)
    highlights_pool.sort(
        key=lambda r: (
            int(r["business_priority"][1:]),
            -(datetime.fromisoformat((r.get("post_time") or "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()),
        )
    )
    per_source: dict[str, int] = {}
    selected = []
    for r in highlights_pool:
        if per_source.get(r["source"], 0) >= 2:
            continue
        selected.append(r)
        per_source[r["source"]] = per_source.get(r["source"], 0) + 1
        if len(selected) >= 5:
            break
    highlights = [
        {
            "source": r["source"],
            "category": r["category"],
            "priority": r["business_priority"],
            "title": r["title"],
            "one_line_summary": (r["description"] or r["follow_up"] or "")[:160],
            "diff_type": r["diff_type"],
            "diff_tag": r["diff_tag"],
            "time": _format_time(r["post_time"]),
            "url": r["url"],
            "is_mock": r["is_mock"],
        }
        for r in selected
    ]
    campaign_new_by_source: dict[str, int] = {}
    product_new_by_source: dict[str, int] = {}
    reward_changes_by_source: dict[str, int] = {}
    for r in highlights_pool:
        if r["category"] == "campaign" and r["status"] == "new":
            campaign_new_by_source[r["source"]] = campaign_new_by_source.get(r["source"], 0) + 1
        if r["category"] == "product" and r["status"] == "new":
            product_new_by_source[r["source"]] = product_new_by_source.get(r["source"], 0) + 1
        if r.get("change_kind") == "reward":
            reward_changes_by_source[r["source"]] = reward_changes_by_source.get(r["source"], 0) + 1

    def leader(values: dict[str, int]) -> Optional[dict]:
        if not values:
            return None
        source, count = max(values.items(), key=lambda item: (item[1], item[0]))
        return {"source": source, "count": count}

    leaders = {
        "new_campaigns": leader(campaign_new_by_source),
        "new_products": leader(product_new_by_source),
        "reward_changes": leader(reward_changes_by_source),
    }
    total_changes = sum(c["today"] for c in chips.values())
    return {
        "batch_date": as_of_date,
        "chips": chips,
        "highlights": highlights,
        "insight": {
            "significant": total_changes > 0,
            "leaders": leaders,
        },
    }


def build_trend(conn: sqlite3.Connection) -> dict:
    """全部历史（不限 as_of_date）按天 x category 的公告计数，前端自己切
    7d/30d/全部——跟 search_index 一样"整段下发，交给前端筛"的思路，不为每种
    时间窗口单独查库。"""
    placeholders = ",".join("?" * len(CATEGORIES))
    rows = _dict_rows(
        conn.execute(
            f"""SELECT date(fetched_at) as d, category,
                       COUNT(DISTINCT COALESCE(group_id, uid)) as n
                FROM announcements
                WHERE source != '{BASELINE_SOURCE}' AND category IN ({placeholders})
                  AND status IN ('new', 'changed')
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
            f"""SELECT uid, group_id, source, locale, category, title, post_time, status, url
                FROM announcements WHERE source != '{BASELINE_SOURCE}'
                ORDER BY post_time DESC"""
        )
    )
    out = []
    for r in rows:
        art = article_index.get(r["uid"], {})
        out.append({
            "uid": r["uid"],
            "group_id": r["group_id"],
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
    out = _merge_localized_rows(out)
    dates = [r["post_time"][:10] for r in out if r["post_time"]]
    return {
        "rows": out,
        "total": len(out),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def build_daily_digest(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """Phase⑤：当日 AI Insight。只读 src/analysis/daily_digest.py 的
    peek_cached_digest()（从不触发真实 LLM 调用——导出是静态快照生成，不应该在
    渲染过程中发起网络请求，见该函数自己的 docstring），命中真实缓存则
    source='llm'；否则 source='fallback'，前端据此显示"LLM 生成"或"占位符"角标，
    不让占位文案冒充真实分析结论。

    Scoping：daily_digest.py 是按 locale 生成的（"这个 locale 今天整体发生了什么"），
    但新版看板 Overview 是跨 locale 的单一入口，不再有逐 locale 的今日 Summary
    位置——这里固定取 EN（每个竞品都覆盖的市场，最具代表性），不是遍历全部
    locale 各生成一份再拼接。
    """
    from src.analysis.daily_digest import peek_cached_digest

    result = peek_cached_digest(conn, "EN", as_of_date)
    if result and result.generated:
        return {"source": "llm", "summary": result.daily_summary, "priority_focus": result.priority_focus}
    return {"source": "fallback", "summary": None, "priority_focus": None}


def build_dashboard_data(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    as_of_date = _resolve_as_of_date(conn)
    generated_at = _resolve_generated_at(conn)
    article_index = _load_article_index(conn)
    zmx_catalog_index = _load_zmx_catalog_index(conn)
    counterpart_uids = {
        art["zmx_counterpart_uids"][0]
        for art in article_index.values()
        if art.get("zmx_counterpart_uids")
    }
    zmx_counterpart_index = _load_zmx_counterpart_index(conn, counterpart_uids)

    def _section(category: str, **kwargs) -> list[dict]:
        return build_category_section(
            conn, category, as_of_date, article_index,
            zmx_catalog_index=zmx_catalog_index, zmx_counterpart_index=zmx_counterpart_index,
            **kwargs,
        )

    campaign_rows = _merge_localized_rows(_section("campaign"))
    campaign_all_rows = _merge_localized_rows(_section("campaign", latest_only=False))
    product_rows = _merge_localized_rows(_section("product"))
    listing_only_rows = _merge_localized_rows(_section("listing"))
    delisting_rows = _merge_localized_rows(_section("delisting"))
    other_rows = _merge_localized_rows(_section("other"))
    listing_rows = listing_only_rows + delisting_rows

    # Phase⑤：Follow-up 是规则派生，不是 AI 产出（Phase②起 run.py 不再让 LLM
    # 输出这个字段）——campaign/product 各自在自己的批次范围内判断 mechanism_type
    # 出现频率，不跨类目混算。
    _derive_follow_up(campaign_rows)
    _derive_follow_up(product_rows)

    overview = build_overview(as_of_date, campaign_rows, product_rows, listing_only_rows, delisting_rows, other_rows)
    daily_digest = build_daily_digest(conn, as_of_date)

    # 推送候选预览：附加在每个 category section 行上，跟 Phase 6 引擎无关，纯预览。
    for rows in (campaign_rows, product_rows, listing_rows):
        for r in rows:
            r["push_candidate"] = _push_candidate({**r, "push_status": "pending"}, r)

    insights_total = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
    insights_mock = conn.execute("SELECT COUNT(*) FROM insights WHERE llm_tokens_used = -1").fetchone()[0]
    zoomex_total = conn.execute(
        f"SELECT COUNT(*) FROM announcements WHERE source = '{BASELINE_SOURCE}'"
    ).fetchone()[0]
    zmx_summary_product_total = conn.execute(
        "SELECT COUNT(*) FROM zmx_summary WHERE category = 'product'"
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

    # analyzed/pending 覆盖两个类目（Phase②起 campaign 也走真实 Stage1/Stage3 分析，
    # 不再只统计 product）；candidate_found/baseline_unmatched 两档随退休的旧版
    # 词项重叠匹配一起下线，见 build_category_section 顶部注释。
    comparison_coverage = {
        status: sum(1 for row in campaign_rows + product_rows if row.get("comparison_status") == status)
        for status in ("analyzed", "pending")
    }

    data = {
        "meta": {
            "generated_at": generated_at,
            "batch_date": as_of_date,
            "db_path": str(Path(db_path).name),
            "insights_total": insights_total,
            "insights_mock": insights_mock,
            "zoomex_baseline_total": zoomex_total,
            "zoomex_product_baseline_total": zmx_summary_product_total,
            "source_coverage": source_coverage,
        },
        "overview": overview,
        "daily_digest": daily_digest,
        "trend": build_trend(conn),
        "campaign": campaign_rows,
        "campaign_all": campaign_all_rows,
        "product": product_rows,
        "listing": listing_rows,
        "announcements": other_rows,
        "markets": build_markets(conn),
        "search_index": build_search_index(conn, article_index),
        "quality": {
            "product_comparison": {
                "total_events": len(campaign_rows) + len(product_rows),
                "zoomex_product_baseline_events": zmx_summary_product_total,
                **comparison_coverage,
                "definition": (
                    "analyzed=covered by a real Stage1/Stage3 analysis run (diff_type reflects the "
                    "LLM+catalog verdict, including 不适用 when evidence was inconclusive); "
                    "pending=not yet reached by an analysis run, not a confirmed absence of difference"
                ),
            },
            "known_gaps": [
                "Campaign lifecycle fields depend on extracted start/end dates and are incomplete when source text omits them.",
                "Product change history is announcement-based; no canonical product entity table exists yet.",
                "KR has no configured collector, so KR market coverage is empty rather than estimated.",
            ],
        },
    }
    conn.close()
    return data


def export(db_path: str, out_path: str) -> dict:
    data = build_dashboard_data(db_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
