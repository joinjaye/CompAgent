"""Zoomex（我方基线）能力目录：结构化提取 + 无 LLM 的目录 rollup + 查询。

取代原来的 src/analysis/zmx_baseline.py，修复该模块的两个根本缺陷：

1. **mechanism_type 不再是 LLM 自由生成的中文标签**，而是
   config/zmx_mechanism_taxonomy.yaml 定义的封闭/半封闭枚举——这是修复标签碎片化
   （同一玩法被起了十几个近义名字）的根本手段，不是靠事后合并。
2. **提取覆盖 Zoomex 全量历史，没有任何 lookback 窗口**——旧版 90 天窗口导致系统
   永远无法断言"Zoomex 真的没有这个能力"，只能说"最近没看到"。既然是我方自己的
   平台能力盘点，没有理由只看最近 90 天。

本模块分两部分：

1. **提取**（`run_extraction()`，CLI `python -m src.analysis.zmx_catalog extract`）：
   把 Zoomex 公告（仅 campaign/product——只有这两个类目会真正过 LLM 分析，见
   run.py 的 ANALYZED_CATEGORIES）结构化成能力目录条目，写入 `zmx_summary` 表。
   EN→其它 locale 的复用是**逐条**判断（同 group_id、EN 侧已经真提取过），不是
   旧版那种整批次的复用逻辑——多语言版本描述的是同一个事件，机制字段本身不因
   语言而变化，直接复制没有额外风险。
2. **rollup**（`run_rollup()`，CLI `... rollup`，**不调用 LLM**）：按枚举里定义的
   每一个 key（不只是观察到的）聚合 zmx_summary，写入 `zmx_catalog_entry`。覆盖
   全部 key 是关键——只有这样 `exists_flag='no'` 才真正代表"没有"，不是"没搜到"。
3. **查询**（`get_catalog_digest()`，供 `run.py` 竞品批次分析注入 prompt）：跟旧版
   `get_baseline_digest()` 完全一致的"类型覆盖优先于单类型深度"算法，只是数据源
   从 zmx_baseline 换成 zmx_summary，且没有 lookback 过滤。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from src.analysis.config import PROJECT_ROOT, load_analysis_config, load_cursor_credentials, load_llm_credentials
from src.analysis.cursor_agent import call_llm_cursor_agent
from src.analysis.llm import call_llm, compute_cache_key, get_cached_response, set_cached_response, strip_code_fences
from src.analysis.prompts import TaxonomyCategory, TaxonomySpec, build_catalog_extraction_prompt
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import utcnow_iso

logger = logging.getLogger(__name__)

# 只有 campaign/product 真正过 LLM 分析（见 run.py 的 ANALYZED_CATEGORIES），listing/
# delisting 不建目录——跟旧版 zmx_baseline 覆盖 listing 不同，这是范围收窄（旧版
# 覆盖 listing 但从未真正被 listing 的 LLM 分析用到，因为 listing 从 Phase 4 v3
# 政策收紧起就已经是零 LLM 调用的确定性汇总）。
CATALOG_CATEGORIES = ("campaign", "product")
TAXONOMY_PATH = PROJECT_ROOT / "config" / "zmx_mechanism_taxonomy.yaml"
_TERM_RE = re.compile(r"[a-z0-9]{3,}|[一-鿿]{2,}", re.IGNORECASE)
# 跟 src/analysis/staged.py 的 _STOPWORDS 同一份清单、同样的理由（见该文件顶部
# 注释）：select_relevant_catalog() 是词项重叠召回，不过滤虚词/行业通用词会导致
# 几乎任何两段英文文本都碰出重叠，2026-07-22 真实数据证实过。两边各自维护一份
# （zmx_catalog.py 反过来会被 staged.py import，放一起会循环 import），改动时
# 两处一起改。
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from",
    "has", "have", "in", "into", "is", "it", "its", "may", "more", "no",
    "not", "of", "on", "or", "per", "than", "that", "the", "their", "then",
    "there", "this", "to", "up", "use", "used", "using", "via", "was",
    "were", "will", "with", "without", "you", "your",
    "users", "user", "trading", "trade", "traders", "trader",
    "feature", "features", "platform", "service", "services",
    "account", "accounts", "available", "support", "supports", "supported",
    "new", "now", "official", "officially", "launch", "launches", "live",
    "update", "updates", "please", "click", "also",
})


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _terms(text: str) -> set[str]:
    return {
        m.group(0).lower() for m in _TERM_RE.finditer(text or "")
        if m.group(0).lower() not in _STOPWORDS
    }


# ============================================================
# 枚举加载
# ============================================================


def load_mechanism_taxonomy(category: str, path: Path | str = TAXONOMY_PATH) -> TaxonomySpec:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cat_data = data.get(category)
    if cat_data is None:
        raise ValueError(f"config/zmx_mechanism_taxonomy.yaml 里没有 {category!r} 这个类目")
    entries = [
        TaxonomyCategory(
            key=c["key"],
            name=c.get("name_zh") or c.get("name_en") or c["key"],
            definition=c.get("definition", ""),
            examples=c.get("examples") or [],
        )
        for c in cat_data.get("categories", [])
    ]
    return TaxonomySpec(category=category, method=cat_data.get("method", "semi_closed"), entries=entries)


def taxonomy_keys(taxonomy: TaxonomySpec) -> set[str]:
    return {e.key for e in taxonomy.entries}


# ============================================================
# 提取
# ============================================================


def list_zoomex_locale_categories(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Zoomex 实际存在数据的 (locale, category) 组合，category 限定 campaign/product。"""
    placeholders = ",".join("?" * len(CATALOG_CATEGORIES))
    rows = conn.execute(
        f"""
        SELECT DISTINCT locale, category FROM announcements
        WHERE source = 'Zoomex' AND category IN ({placeholders})
        """,
        CATALOG_CATEGORIES,
    ).fetchall()
    return sorted((r["locale"], r["category"]) for r in rows)


def list_pending_zoomex_rows(conn: sqlite3.Connection, *, category: str, locale: str) -> list[sqlite3.Row]:
    """尚未提取或 content_hash 已变化的 Zoomex 公告——**没有 lookback_days 参数**，
    覆盖全量历史。这是修复"无法断言缺失"问题的关键，不是遗漏。
    """
    return conn.execute(
        """
        SELECT a.* FROM announcements a
        LEFT JOIN zmx_summary s ON s.source_uid = a.uid
        WHERE a.source = 'Zoomex' AND a.category = ? AND a.locale = ?
              AND a.content IS NOT NULL AND a.content != ''
              AND a.duplicate_of IS NULL
              AND (s.source_uid IS NULL OR s.content_hash != a.content_hash)
        ORDER BY a.post_time DESC
        """,
        (category, locale),
    ).fetchall()


def find_derivable_en_summary(conn: sqlite3.Connection, *, group_id: Optional[str], category: str) -> Optional[sqlite3.Row]:
    """同一 group_id、已经真实提取过（非派生）的 EN（缺失则退到 EN-Asia）zmx_summary
    行。多语言版本描述的是同一个事件，机制字段本身不因语言而变化——这是逐条判断，
    不需要旧版 batch.can_derive_from_en() 那种"整批 group_id 集合完全相同"的保守
    校验（那是为了保护批次级 summary 文本的一致性，这里没有批次级文本）。
    """
    if not group_id:
        return None
    return conn.execute(
        """
        SELECT s.* FROM zmx_summary s
        JOIN announcements a ON a.uid = s.source_uid
        WHERE a.group_id = ? AND s.category = ? AND a.locale IN ('EN', 'EN-Asia')
              AND s.is_locale_derived = 0
        ORDER BY CASE a.locale WHEN 'EN' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (group_id, category),
    ).fetchone()


def _json_list_or_none(value) -> Optional[list]:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def upsert_summary_row(
    conn: sqlite3.Connection,
    *,
    source_uid: str,
    group_id: Optional[str],
    category: str,
    locale: str,
    content_hash: str,
    prompt_version: str,
    mechanism_type: str,
    raw_mechanism_label: Optional[str] = None,
    core_summary: Optional[str] = None,
    key_mechanics: Optional[str] = None,
    reward_form: Optional[str] = None,
    reward_amount: Optional[str] = None,
    reward_token: Optional[str] = None,
    target_users: Optional[str] = None,
    entry_threshold: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    main_feature: Optional[str] = None,
    supported_market: Optional[list] = None,
    supported_token: Optional[list] = None,
    supported_platform: Optional[list] = None,
    supported_user_tier: Optional[list] = None,
    is_locale_derived: bool = False,
    derived_from_uid: Optional[str] = None,
    llm_tokens_used: Optional[int] = None,
) -> None:
    """按 source_uid 幂等 upsert。"""
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO zmx_summary (
            source_uid, group_id, category, locale, mechanism_type, raw_mechanism_label,
            core_summary, key_mechanics, reward_form, reward_amount, reward_token,
            target_users, entry_threshold, start_date, end_date, main_feature,
            supported_market, supported_token, supported_platform, supported_user_tier,
            content_hash, is_locale_derived, derived_from_uid, prompt_version,
            llm_tokens_used, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (source_uid) DO UPDATE SET
            group_id = excluded.group_id,
            mechanism_type = excluded.mechanism_type,
            raw_mechanism_label = excluded.raw_mechanism_label,
            core_summary = excluded.core_summary,
            key_mechanics = excluded.key_mechanics,
            reward_form = excluded.reward_form,
            reward_amount = excluded.reward_amount,
            reward_token = excluded.reward_token,
            target_users = excluded.target_users,
            entry_threshold = excluded.entry_threshold,
            start_date = excluded.start_date,
            end_date = excluded.end_date,
            main_feature = excluded.main_feature,
            supported_market = excluded.supported_market,
            supported_token = excluded.supported_token,
            supported_platform = excluded.supported_platform,
            supported_user_tier = excluded.supported_user_tier,
            content_hash = excluded.content_hash,
            is_locale_derived = excluded.is_locale_derived,
            derived_from_uid = excluded.derived_from_uid,
            prompt_version = excluded.prompt_version,
            llm_tokens_used = excluded.llm_tokens_used,
            updated_at = excluded.updated_at
        """,
        (
            source_uid, group_id, category, locale, mechanism_type, raw_mechanism_label,
            core_summary, key_mechanics, reward_form, reward_amount, reward_token,
            target_users, entry_threshold, start_date, end_date, main_feature,
            json.dumps(supported_market) if supported_market is not None else None,
            json.dumps(supported_token) if supported_token is not None else None,
            json.dumps(supported_platform) if supported_platform is not None else None,
            json.dumps(supported_user_tier) if supported_user_tier is not None else None,
            content_hash, is_locale_derived, derived_from_uid, prompt_version,
            llm_tokens_used, now, now,
        ),
    )


def parse_catalog_extraction_response(raw_text: str, related_uids: set[str], valid_keys: set[str]) -> list[dict]:
    """解析提取响应。mechanism_type 不在枚举内（含 LLM 自造的新值）一律强制落
    "other"，原值搬进 raw_mechanism_label——不允许 LLM 自造新类型悄悄溜进目录。
    """
    try:
        data = json.loads(strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Zoomex 能力目录提取响应 JSON 解析失败，本批次跳过：%s", e)
        return []
    if not isinstance(data, dict):
        logger.error("Zoomex 能力目录提取响应不是 JSON object，本批次跳过")
        return []

    articles_raw = data.get("articles")
    if not isinstance(articles_raw, list):
        return []

    def _str_or_none(value) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    def _list_or_empty(value) -> list:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    results: list[dict] = []
    for item in articles_raw:
        if not isinstance(item, dict):
            continue
        uid = item.get("uid")
        if uid not in related_uids:
            logger.warning("丢弃提取条目：uid=%r 不在本批次内", uid)
            continue

        mechanism_type = item.get("mechanism_type")
        raw_label = _str_or_none(item.get("raw_mechanism_label"))
        if mechanism_type not in valid_keys:
            if mechanism_type and mechanism_type != "other":
                raw_label = raw_label or str(mechanism_type)
            mechanism_type = "other"
        if mechanism_type == "other" and not raw_label:
            raw_label = "（未说明）"

        results.append({
            "uid": uid,
            "mechanism_type": mechanism_type,
            "raw_mechanism_label": raw_label if mechanism_type == "other" else None,
            "core_summary": _str_or_none(item.get("core_summary")),
            "key_mechanics": _str_or_none(item.get("key_mechanics")),
            "reward_form": _str_or_none(item.get("reward_form")),
            "reward_amount": _str_or_none(item.get("reward_amount")),
            "reward_token": _str_or_none(item.get("reward_token")),
            "target_users": _str_or_none(item.get("target_users")),
            "entry_threshold": _str_or_none(item.get("entry_threshold")),
            "start_date": _str_or_none(item.get("start_date")),
            "end_date": _str_or_none(item.get("end_date")),
            "main_feature": _str_or_none(item.get("main_feature")),
            "supported_market": _list_or_empty(item.get("supported_market")),
            "supported_token": _list_or_empty(item.get("supported_token")),
            "supported_platform": _list_or_empty(item.get("supported_platform")),
            "supported_user_tier": _list_or_empty(item.get("supported_user_tier")),
        })
    return results


@dataclass
class ExtractionReport:
    extracted: int = 0
    derived: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    validation_failed: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    skipped_budget_cap: int = 0
    combos: list[str] = field(default_factory=list)


def run_extraction(
    conn: sqlite3.Connection,
    *,
    locale: Optional[str] = None,
    category: Optional[str] = None,
    batch_size: Optional[int] = None,
    provider: Optional[str] = None,
    max_calls: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    max_tokens: Optional[int] = None,
    dry_run: bool = False,
) -> ExtractionReport:
    """不传 locale/category 时遍历 Zoomex 全部已有数据的 locale × category 组合。

    组合按 category 分组、组内 EN 优先排序——per-article 的 EN→locale 派生
    （find_derivable_en_summary）要求 EN 那条已经真实入库，同一次 run 内 EN 必须
    先于其它 locale 被处理才能命中派生、省下 LLM 调用。
    """
    cfg = load_analysis_config()
    catalog_cfg = cfg.get("zmx_catalog", {})
    extraction_cfg = catalog_cfg.get("extraction", {})
    llm_cfg = cfg.get("llm", {})

    batch_size = batch_size if batch_size is not None else extraction_cfg.get("batch_size", 15)
    provider = provider or llm_cfg.get("provider", "openai_http")
    max_calls_cap = max_calls if max_calls is not None else extraction_cfg.get("max_calls_per_run")
    max_cost_cap = max_cost_usd if max_cost_usd is not None else extraction_cfg.get("max_cost_usd_per_run")
    max_tokens_cap = max_tokens if max_tokens is not None else extraction_cfg.get("max_tokens_per_run")
    price_per_1k = extraction_cfg.get("price_usd_per_1k_tokens", 0.0)
    extraction_version = extraction_cfg.get("prompt_version", "zmx-catalog-extract-v1")
    response_max_tokens = extraction_cfg.get("response_max_tokens", 3000)
    article_content_chars = cfg.get("content_truncation", {}).get("article_content_chars", 4000)

    credentials = None
    if not dry_run:
        credentials = load_cursor_credentials() if provider == "cursor_agent" else load_llm_credentials()
        credentials.validate()

    if locale and category:
        combos = [(locale, category)]
    elif locale:
        combos = [(locale, c) for c in CATALOG_CATEGORIES]
    elif category:
        combos = [(l, c) for l, c in list_zoomex_locale_categories(conn) if c == category]
    else:
        combos = list_zoomex_locale_categories(conn)
    combos.sort(key=lambda lc: (lc[1], lc[0] != "EN", lc[0]))

    report = ExtractionReport()
    taxonomy_by_category: dict[str, TaxonomySpec] = {}

    for loc, cat in combos:
        if cat not in taxonomy_by_category:
            taxonomy_by_category[cat] = load_mechanism_taxonomy(cat)
        taxonomy = taxonomy_by_category[cat]
        valid_keys = taxonomy_keys(taxonomy) | {"other"}

        pending_rows = list_pending_zoomex_rows(conn, category=cat, locale=loc)
        if not pending_rows:
            continue

        rows_needing_llm = []
        for row in pending_rows:
            derived = None if loc == "EN" else find_derivable_en_summary(conn, group_id=row["group_id"], category=cat)
            if derived is None:
                rows_needing_llm.append(row)
                continue
            upsert_summary_row(
                conn, source_uid=row["uid"], group_id=row["group_id"], category=cat, locale=loc,
                content_hash=row["content_hash"], prompt_version=derived["prompt_version"],
                mechanism_type=derived["mechanism_type"], raw_mechanism_label=derived["raw_mechanism_label"],
                core_summary=derived["core_summary"], key_mechanics=derived["key_mechanics"],
                reward_form=derived["reward_form"], reward_amount=derived["reward_amount"],
                reward_token=derived["reward_token"], target_users=derived["target_users"],
                entry_threshold=derived["entry_threshold"], start_date=derived["start_date"],
                end_date=derived["end_date"], main_feature=derived["main_feature"],
                supported_market=_json_list_or_none(derived["supported_market"]),
                supported_token=_json_list_or_none(derived["supported_token"]),
                supported_platform=_json_list_or_none(derived["supported_platform"]),
                supported_user_tier=_json_list_or_none(derived["supported_user_tier"]),
                is_locale_derived=True, derived_from_uid=derived["source_uid"], llm_tokens_used=0,
            )
            report.derived += 1
            if not dry_run:
                conn.commit()

        if not rows_needing_llm:
            continue

        for batch_rows in _chunk(rows_needing_llm, batch_size):
            related_uids = {r["uid"] for r in batch_rows}
            prompt = build_catalog_extraction_prompt(
                category=cat, locale=loc, rows=batch_rows, taxonomy=taxonomy,
                article_content_chars=article_content_chars,
            )

            if dry_run:
                report.combos.append(f"{loc}/{cat} (dry-run, {len(batch_rows)} 条)")
                continue

            content_hashes = [r["content_hash"] for r in batch_rows]
            prompt_context_hash = hashlib.sha256(
                json.dumps({"system": prompt.system, "user": prompt.user}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            cache_key = compute_cache_key(
                content_hashes, extraction_version, model=credentials.model, context_hash=prompt_context_hash,
            )
            cached = get_cached_response(conn, cache_key)
            if cached is not None:
                raw_text = cached
                tokens_used = 0
                report.cache_hits += 1
            else:
                budget_exhausted = (
                    (max_calls_cap is not None and report.llm_calls >= max_calls_cap)
                    or (max_tokens_cap is not None and report.total_tokens >= max_tokens_cap)
                    or (max_cost_cap is not None and report.total_cost_usd >= max_cost_cap)
                )
                if budget_exhausted:
                    logger.warning(
                        "达到提取熔断上限（calls=%s/%s tokens=%s/%s cost=%.4f/%s），"
                        "跳过 %s/%s 剩余批次（下次重跑会重新尝试）",
                        report.llm_calls, max_calls_cap, report.total_tokens, max_tokens_cap,
                        report.total_cost_usd, max_cost_cap, loc, cat,
                    )
                    report.skipped_budget_cap += 1
                    report.combos.append(f"{loc}/{cat} (skipped: budget cap reached)")
                    continue

                if provider == "cursor_agent":
                    raw_text, tokens_used = call_llm_cursor_agent(
                        prompt.system, prompt.user, api_key=credentials.api_key, model=credentials.model,
                    )
                else:
                    raw_text, tokens_used = call_llm(
                        prompt.system, prompt.user, credentials=credentials, model=credentials.model,
                        temperature=llm_cfg.get("temperature", 0), max_tokens=response_max_tokens,
                        timeout_s=llm_cfg.get("timeout_s", 60), max_retries=llm_cfg.get("max_retries", 3),
                    )
                set_cached_response(conn, cache_key, raw_text)
                report.llm_calls += 1
                tokens_used = tokens_used or 0
                report.total_tokens += tokens_used
                report.total_cost_usd += tokens_used / 1000 * price_per_1k

            parsed = parse_catalog_extraction_response(raw_text, related_uids, valid_keys)
            if not parsed:
                report.validation_failed += 1

            content_hash_by_uid = {r["uid"]: r["content_hash"] for r in batch_rows}
            group_id_by_uid = {r["uid"]: r["group_id"] for r in batch_rows}
            written = 0
            for item in parsed:
                upsert_summary_row(
                    conn, source_uid=item["uid"], group_id=group_id_by_uid.get(item["uid"]),
                    category=cat, locale=loc, content_hash=content_hash_by_uid.get(item["uid"]),
                    prompt_version=extraction_version, mechanism_type=item["mechanism_type"],
                    raw_mechanism_label=item["raw_mechanism_label"], core_summary=item["core_summary"],
                    key_mechanics=item["key_mechanics"], reward_form=item["reward_form"],
                    reward_amount=item["reward_amount"], reward_token=item["reward_token"],
                    target_users=item["target_users"], entry_threshold=item["entry_threshold"],
                    start_date=item["start_date"], end_date=item["end_date"], main_feature=item["main_feature"],
                    supported_market=item["supported_market"], supported_token=item["supported_token"],
                    supported_platform=item["supported_platform"], supported_user_tier=item["supported_user_tier"],
                    is_locale_derived=False, derived_from_uid=None, llm_tokens_used=tokens_used,
                )
                written += 1
            report.extracted += written
            report.combos.append(f"{loc}/{cat} ({written}/{len(batch_rows)} 条)")
            # 每个批次落盘一次：熔断或任何意外中断都不会丢失已经产出的提取结果
            conn.commit()

    return report


# ============================================================
# rollup（无 LLM 调用，纯 SQL/Python 聚合）
# ============================================================


@dataclass
class RollupReport:
    entries_written: int = 0


def run_rollup(conn: sqlite3.Connection, *, category: Optional[str] = None) -> RollupReport:
    """覆盖枚举里定义的每一个 key（不只是观察到的）：≥1 条 zmx_summary 命中 →
    exists_flag='yes'；0 条命中但在 mechanism_type='other' 桶里有词项重叠的近似
    条目 → 'partial'（供人工核对，不是确认命中）；完全没有 → 'no'，这才真正代表
    "Zoomex 没有这个能力"，不是"没搜到"。
    """
    categories = (category,) if category else CATALOG_CATEGORIES
    written = 0
    for cat in categories:
        taxonomy = load_mechanism_taxonomy(cat)
        other_rows = conn.execute(
            """
            SELECT s.source_uid, s.raw_mechanism_label, s.core_summary, s.key_mechanics, a.post_time
            FROM zmx_summary s JOIN announcements a ON a.uid = s.source_uid
            WHERE s.category = ? AND s.mechanism_type = 'other'
            ORDER BY a.post_time DESC
            """,
            (cat,),
        ).fetchall()

        for entry in taxonomy.entries:
            if entry.key == "other":
                continue
            rows = conn.execute(
                """
                SELECT s.source_uid, s.core_summary, s.reward_form, s.reward_amount, s.reward_token, a.post_time
                FROM zmx_summary s JOIN announcements a ON a.uid = s.source_uid
                WHERE s.category = ? AND s.mechanism_type = ?
                ORDER BY a.post_time DESC
                """,
                (cat, entry.key),
            ).fetchall()

            if rows:
                exists_flag = "yes"
                example_uids = [r["source_uid"] for r in rows[:3]]
                capability_desc = rows[0]["core_summary"] or f"Zoomex 已有「{entry.name}」类型的活动/功能"
                typical_reward = None
                if cat == "campaign":
                    reward_row = next((r for r in rows if r["reward_amount"] or r["reward_form"]), None)
                    if reward_row:
                        typical_reward = " / ".join(
                            p for p in (reward_row["reward_amount"], reward_row["reward_token"], reward_row["reward_form"]) if p
                        ) or None
            else:
                query_terms = _terms(" ".join(filter(None, [entry.name, entry.definition, *entry.examples])))
                scored = []
                for r in other_rows:
                    cand_terms = _terms(" ".join(filter(None, [r["raw_mechanism_label"], r["core_summary"], r["key_mechanics"]])))
                    overlap = len(query_terms & cand_terms)
                    if overlap:
                        scored.append((overlap, r))
                scored.sort(key=lambda x: x[0], reverse=True)
                if scored:
                    exists_flag = "partial"
                    example_uids = [r["source_uid"] for _, r in scored[:3]]
                    top_label = scored[0][1]["raw_mechanism_label"] or scored[0][1]["core_summary"] or ""
                    capability_desc = f"近似匹配（原始描述：{top_label}），建议人工核对是否算「{entry.name}」"
                else:
                    exists_flag = "no"
                    example_uids = []
                    capability_desc = f"Zoomex 目前没有「{entry.name}」类型的活动/功能记录"
                typical_reward = None

            entry_id = hashlib.sha256(f"{cat}_{entry.key}".encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO zmx_catalog_entry (
                    id, category, mechanism_type, exists_flag, capability_desc,
                    example_uids, typical_reward, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    exists_flag = excluded.exists_flag,
                    capability_desc = excluded.capability_desc,
                    example_uids = excluded.example_uids,
                    typical_reward = excluded.typical_reward,
                    updated_at = excluded.updated_at
                """,
                (entry_id, cat, entry.key, exists_flag, capability_desc, json.dumps(example_uids), typical_reward, utcnow_iso()),
            )
            written += 1
    conn.commit()
    return RollupReport(entries_written=written)


# ============================================================
# 查询
# ============================================================


@dataclass
class ZmxCatalogEntry:
    uid: str
    title: Optional[str]
    mechanism_type: str
    key_mechanics: Optional[str]
    reward_range: Optional[str]
    target_users: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    post_time: Optional[str]


def _synthesize_reward_range(amount: Optional[str], token: Optional[str], form: Optional[str]) -> Optional[str]:
    joined = " ".join(p for p in (amount, token) if p) or None
    if joined and form:
        return f"{joined}（{form}）"
    return joined or form


def get_catalog_digest(
    conn: sqlite3.Connection,
    *,
    category: str,
    locale: str,
    max_entries: int = 20,
    max_examples_per_type: int = 2,
) -> list[ZmxCatalogEntry]:
    """按 category×locale 拉取 Zoomex 全量结构化目录（**无 lookback 窗口**），类型
    覆盖优先于单类型深度——算法跟旧版 get_baseline_digest 完全一致，只是数据源
    换成了 zmx_summary，且不再受 90 天窗口限制。
    """
    rows = conn.execute(
        """
        SELECT s.source_uid AS uid, a.title, s.mechanism_type, s.key_mechanics,
               s.reward_amount, s.reward_token, s.reward_form, s.target_users,
               s.start_date, s.end_date, a.post_time
        FROM zmx_summary s
        JOIN announcements a ON a.uid = s.source_uid
        WHERE s.category = ? AND s.locale = ?
        ORDER BY a.post_time DESC
        """,
        (category, locale),
    ).fetchall()

    by_type: dict[str, list[sqlite3.Row]] = {}
    type_order: list[str] = []
    for row in rows:
        mt = row["mechanism_type"]
        if mt not in by_type:
            by_type[mt] = []
            type_order.append(mt)
        by_type[mt].append(row)

    selected: list[sqlite3.Row] = []
    for mt in type_order:
        if len(selected) >= max_entries:
            break
        selected.append(by_type[mt][0])

    depth = 1
    while len(selected) < max_entries and depth < max_examples_per_type:
        added_any = False
        for mt in type_order:
            if len(selected) >= max_entries:
                break
            if len(by_type[mt]) > depth:
                selected.append(by_type[mt][depth])
                added_any = True
        depth += 1
        if not added_any:
            break

    return [
        ZmxCatalogEntry(
            uid=r["uid"], title=r["title"], mechanism_type=r["mechanism_type"],
            key_mechanics=r["key_mechanics"],
            reward_range=_synthesize_reward_range(r["reward_amount"], r["reward_token"], r["reward_form"]),
            target_users=r["target_users"], start_date=r["start_date"], end_date=r["end_date"],
            post_time=r["post_time"],
        )
        for r in selected
    ]


def select_relevant_catalog(
    rows: list[sqlite3.Row], entries: list[ZmxCatalogEntry], *, max_entries: int,
) -> list[ZmxCatalogEntry]:
    """从类型覆盖池中选择与当前批次更相关的少量目录条目——跟旧版
    select_relevant_baseline 完全相同的确定性词项重叠算法，没有变化（这一层价值
    跟数据源是不是全量历史无关，一直有效）。
    """
    if max_entries <= 0 or len(entries) <= max_entries:
        return entries

    query = _terms("\n".join(f"{r['title'] or ''}\n{(r['content'] or '')[:1200]}" for r in rows))
    seen_types: set[str] = set()
    scored: list[tuple[int, int, ZmxCatalogEntry]] = []
    for position, entry in enumerate(entries):
        candidate = _terms(
            " ".join(filter(None, [entry.title, entry.mechanism_type, entry.key_mechanics, entry.reward_range, entry.target_users]))
        )
        overlap = len(query & candidate)
        diversity = 1 if entry.mechanism_type not in seen_types else 0
        seen_types.add(entry.mechanism_type)
        scored.append((overlap * 10 + diversity, -position, entry))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [entry for _score, _position, entry in scored[:max_entries]]


# ============================================================
# CLI
# ============================================================


def main() -> None:
    # --db 只注册在每个子命令的 parser 上，不放在顶层 parser 上——argparse 的一个
    # 真实坑：如果顶层和子命令的 parser 都定义同名 dest（"db"），子命令 parser 的
    # default 会在解析子命令自己的 token 时无条件覆盖掉顶层已经解析出的值，`--db X
    # rollup` 这种"放在子命令前"的写法会静默丢失用户传的路径、退回默认库。只在
    # 子命令 parser 上定义，强制 --db 必须写在子命令名之后（`rollup --db X`），
    # 这也是 scripts/run_daily_pre_lark.sh 里已经在用的写法。
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_db_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", default=str(DEFAULT_DB_PATH))

    def _add_extract_args(p: argparse.ArgumentParser) -> None:
        _add_db_arg(p)
        p.add_argument("--locale", help="不传则遍历 Zoomex 全部已有数据的 locale")
        p.add_argument("--category", choices=list(CATALOG_CATEGORIES), help="不传则遍历 campaign/product")
        p.add_argument("--batch-size", type=int, help="默认读 config/analysis.yaml 的 extraction.batch_size（15）")
        p.add_argument("--provider", choices=["openai_http", "cursor_agent"], help="覆盖 config/analysis.yaml 的 llm.provider")
        p.add_argument("--max-calls", type=int, help="熔断上限：调用次数")
        p.add_argument("--max-cost-usd", type=float, help="熔断上限：累计美元成本")
        p.add_argument("--max-tokens", type=int, help="熔断上限：累计 token 数")
        p.add_argument("--dry-run", action="store_true")

    _add_extract_args(sub.add_parser("extract", help="提取 Zoomex 公告为结构化 zmx_summary（调用 LLM）"))

    p_rollup = sub.add_parser("rollup", help="按枚举 rollup zmx_catalog_entry（不调用 LLM）")
    _add_db_arg(p_rollup)
    p_rollup.add_argument("--category", choices=list(CATALOG_CATEGORIES))

    _add_extract_args(sub.add_parser("all", help="先 extract 再 rollup"))

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect(args.db)
    try:
        if args.command in ("extract", "all"):
            report = run_extraction(
                conn, locale=args.locale, category=args.category, batch_size=args.batch_size,
                provider=args.provider, max_calls=args.max_calls, max_cost_usd=args.max_cost_usd,
                max_tokens=args.max_tokens, dry_run=args.dry_run,
            )
            if not args.dry_run:
                conn.commit()
            print(
                f"提取结果：extracted={report.extracted} derived={report.derived} "
                f"cache_hits={report.cache_hits} llm_calls={report.llm_calls} "
                f"validation_failed={report.validation_failed} total_tokens={report.total_tokens} "
                f"total_cost_usd={report.total_cost_usd:.4f} skipped_budget_cap={report.skipped_budget_cap}"
            )
            for c in report.combos:
                print(f"  - {c}")

        if args.command == "rollup":
            rollup_report = run_rollup(conn, category=args.category)
            print(f"rollup 结果：entries_written={rollup_report.entries_written}")
        elif args.command == "all" and not args.dry_run:
            rollup_report = run_rollup(conn, category=args.category)
            print(f"rollup 结果：entries_written={rollup_report.entries_written}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
