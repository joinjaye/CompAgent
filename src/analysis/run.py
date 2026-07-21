"""Phase 4 批次编排：按 (source, category, locale) 分组当日 status IN (new, changed)
的公告，每组一次分析（要么复用同日 EN 批次、要么走 staged-v1 三段流程），写一行
insights。

Phase②（staged.py 接入）把原来"一次 LLM 调用产出整批分析"的单体流程，改成
Stage1（每篇公告独立事实抽取，per-article 缓存）→ Stage2（无 LLM，确定性候选召回）→
Stage3（一次批量业务判断调用，只读 Stage1 产出的事实 + Stage2 召回的候选，不重新读
公告原文）。priority 完全由 calculate_priority() 程序计算，AI 不直接产出；
action_type/owner/follow_up 也不再由 AI 产出（改为 Phase⑤ 的确定性规则）。

CLI：
    python -m src.analysis [--date YYYY-MM-DD] [--source Bitunix,Weex]
                            [--category campaign] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.analysis.batch import (
    BatchKey,
    can_derive_from_en,
    compute_batch_id,
    get_batch_rows,
    get_insight,
    list_batch_keys,
)
from src.analysis.config import load_analysis_config, load_cursor_credentials, load_llm_credentials
from src.analysis.cursor_agent import call_llm_cursor_agent
from src.analysis.llm import (
    aggregate_batch_diff_type,
    call_llm,
    get_cached_response,
    set_cached_response,
    validate_business_judgment,
    validate_fact_extraction,
)
from src.analysis.prompts import build_business_judgment_prompt, build_fact_extraction_prompt
from src.analysis.staged import calculate_priority, comparison_cache_key, extraction_cache_key, preprocess_article, recall_candidates
from src.analysis.zmx_catalog import get_catalog_digest, select_relevant_catalog
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import get_content_history, utcnow_iso

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = ("Bitunix", "Weex")  # 竞品源；Zoomex 是基线，不作为被分析对象
ANALYZED_CATEGORIES = frozenset({"campaign", "product"})

# event_type -> change_kind（campaign 独有字段，跟旧版 v2 prompt 的语义一致：
# reward=奖励规模/形式变化，rule=规则或门槛变化，other=其它变化）。只有
# status=='changed' 时才会用到，new/unchanged 恒为 None。
_EVENT_TYPE_TO_CHANGE_KIND = {"reward_changed": "reward", "rule_changed": "rule"}
_PRIORITY_RANK = {"高": 3, "中": 2, "低": 1}


def today_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _old_content_for_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Optional[str]:
    """changed 条目的"变更前正文"：content_history 里最近一次归档的版本
    （get_content_history 按 id 升序返回，最后一条就是这次变更前的直接上一版本）。
    """
    if row["status"] != "changed":
        return None
    history = get_content_history(conn, row["uid"])
    return history[-1]["content"] if history else None


def _build_old_content_map(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, Optional[str]]:
    return {row["uid"]: _old_content_for_row(conn, row) for row in rows if row["status"] == "changed"}


def _fact_payload(index: int, fact) -> dict[str, Any]:
    """FactExtractionResult -> 普通 dict，同时供 comparison_cache_key/
    build_business_judgment_prompt 的 facts 参数、以及 recall_candidates 的
    facts 参数使用（recall_candidates 只读 mechanism/eligibility/reward/
    target_users/feature 这几个 key，多出来的字段无害）。
    """
    return {
        "i": index,
        "event_type": fact.event_type,
        "mechanism": fact.mechanism,
        "feature": fact.feature,
        "start_at": fact.start_at,
        "end_at": fact.end_at,
        "reward": fact.reward,
        "eligibility": fact.eligibility,
        "target_users": fact.target_users,
        "changes": fact.changes,
        "confidence": fact.confidence,
    }


def _synthesize_batch_summary(articles: list[dict[str, Any]]) -> str:
    """批次级 summary 不再有 LLM 直接产出（Stage1/Stage3 都是逐篇/逐条粒度，没有
    "整批叙述"这个概念），改成确定性模板拼接——诚实反映"这是统计口径，不是 AI 综述"，
    不假装有一句 AI 生成的总结文案。"""
    total = len(articles)
    if total == 0:
        return "本批次无公告"
    counts: dict[str, int] = {}
    for a in articles:
        counts[a["diff_type"]] = counts.get(a["diff_type"], 0) + 1
    parts = [f"{k} {v} 条" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return f"本批次共 {total} 条：" + "、".join(parts) + "。"


def _synthesize_batch_zmx_diff(articles: list[dict[str, Any]]) -> Optional[str]:
    """zmx_diff 是 daily_digest.py 的真实输入（见 Phase⑤），不能因为 Stage3 变成
    逐条判断就留空——拼接每条非"不适用"判断的 diff_detail（reason）。"""
    reasons = [a["diff_detail"] for a in articles if a["diff_type"] != "不适用" and a.get("diff_detail")]
    return "\n".join(reasons) if reasons else None


def _aggregate_batch_priority(priorities: list[Optional[str]]) -> Optional[str]:
    valid = [p for p in priorities if p in _PRIORITY_RANK]
    if not valid:
        return None
    return max(valid, key=lambda p: _PRIORITY_RANK[p])


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def upsert_insight(
    conn: sqlite3.Connection,
    *,
    insight_id: str,
    batch_date: str,
    source: str,
    category: str,
    locale: str,
    related_uids: list[str],
    is_locale_derived: bool,
    derived_from_id: Optional[str],
    summary: Optional[str],
    articles_analysis: Optional[list[dict[str, Any]]],
    zmx_diff: Optional[str],
    diff_type: Optional[str],
    priority: Optional[str],
    zmx_evidence_uids: list[str],
    prompt_version: str,
    llm_tokens_used: Optional[int],
) -> None:
    """同一天同一批次重跑：追加新公告到 related_uids，全量重新写入并覆盖原记录
    （updated_at 刷新，created_at 保留首次写入时间）。"""
    now = utcnow_iso()
    existing = conn.execute("SELECT created_at FROM insights WHERE id = ?", (insight_id,)).fetchone()
    created_at = existing["created_at"] if existing else now

    conn.execute(
        """
        INSERT INTO insights (
            id, batch_date, source, category, locale, article_count, related_uids,
            is_locale_derived, derived_from_id, summary, articles_analysis, zmx_diff,
            diff_type, priority, zmx_evidence_uids, prompt_version, llm_tokens_used,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            article_count = excluded.article_count,
            related_uids = excluded.related_uids,
            is_locale_derived = excluded.is_locale_derived,
            derived_from_id = excluded.derived_from_id,
            summary = excluded.summary,
            articles_analysis = excluded.articles_analysis,
            zmx_diff = excluded.zmx_diff,
            diff_type = excluded.diff_type,
            priority = excluded.priority,
            zmx_evidence_uids = excluded.zmx_evidence_uids,
            prompt_version = excluded.prompt_version,
            llm_tokens_used = excluded.llm_tokens_used,
            updated_at = excluded.updated_at
        """,
        (
            insight_id, batch_date, source, category, locale, len(related_uids), json.dumps(related_uids),
            is_locale_derived, derived_from_id, summary,
            json.dumps(articles_analysis) if articles_analysis is not None else None,
            zmx_diff, diff_type, priority, json.dumps(zmx_evidence_uids), prompt_version, llm_tokens_used,
            created_at, now,
        ),
    )


def _remap_articles_to_locale(
    conn: sqlite3.Connection, en_articles: list[dict[str, Any]], locale_rows: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    """EN 批次 articles_analysis 里每条的 uid 是 EN 的 uid，复用到其它 locale 时要换成
    本 locale 对应的 uid（通过 group_id 匹配）。字段无关（不管 Stage1/Stage3 产出了
    哪些 key，这里只换 uid、原样透传其余字段），EN 侧找不到 group_id、或本 locale
    批次没有对应 group_id 的条目会被跳过（理论上不该发生——can_derive_from_en 已经
    保证子集关系，这里只是防御）。
    """
    en_uids = [a.get("uid") for a in en_articles if isinstance(a, dict)]
    if not en_uids:
        return []
    placeholders = ",".join("?" * len(en_uids))
    en_group_rows = conn.execute(
        f"SELECT uid, group_id FROM announcements WHERE uid IN ({placeholders})", en_uids
    ).fetchall()
    en_uid_to_group = {r["uid"]: r["group_id"] for r in en_group_rows}
    group_to_locale_uid = {row["group_id"]: row["uid"] for row in locale_rows if row["group_id"]}

    remapped = []
    for article in en_articles:
        if not isinstance(article, dict):
            continue
        group_id = en_uid_to_group.get(article.get("uid"))
        locale_uid = group_to_locale_uid.get(group_id) if group_id else None
        if not locale_uid:
            continue
        new_article = dict(article)
        new_article["uid"] = locale_uid
        remapped.append(new_article)
    return remapped


@dataclass
class RunReport:
    analyzed: int = 0
    derived: int = 0
    cache_hits: int = 0
    llm_calls: int = 0
    validation_failed: int = 0
    total_tokens: int = 0
    skipped_budget_cap: int = 0
    batches: list[str] = field(default_factory=list)


def run(
    conn: sqlite3.Connection,
    batch_date: Optional[str] = None,
    sources: Optional[tuple[str, ...]] = None,
    categories: Optional[tuple[str, ...]] = None,
    dry_run: bool = False,
    provider: Optional[str] = None,
    max_calls: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> RunReport:
    """provider/max_calls/max_tokens 显式传参时覆盖 config/analysis.yaml 的
    llm.provider / llm.max_calls_per_run / llm.max_tokens_per_run（CLI
    --provider/--max-calls/--max-tokens 用这个覆盖来做一次性试跑，不用每次改
    yaml）。熔断按 LLM 调用次数/累计 token 数计——Stage1 每篇公告一次调用、Stage3
    每批一次调用，都计入同一套运行级熔断器。
    """
    batch_date = batch_date or today_utc_date()
    sources = sources or DEFAULT_SOURCES
    cfg = load_analysis_config()
    llm_cfg = cfg.get("llm", {})
    provider = provider or llm_cfg.get("provider", "openai_http")
    max_calls_per_run = max_calls if max_calls is not None else llm_cfg.get("max_calls_per_run")
    max_tokens_per_run = max_tokens if max_tokens is not None else llm_cfg.get("max_tokens_per_run")
    max_tokens_per_call = llm_cfg.get("max_tokens_per_call", {})

    zmx_cfg = cfg.get("zmx_catalog", {})
    prompt_versions = cfg.get("prompt_versions", {})
    article_facts_version = prompt_versions.get("article_facts", "article-facts-v1")
    business_judgment_version = prompt_versions.get("business_judgment", "business-judgment-v1")
    combined_prompt_version = f"{article_facts_version}+{business_judgment_version}"

    report = RunReport()
    keys = list_batch_keys(conn, sources, batch_date)
    if categories:
        keys = [k for k in keys if k.category in categories]
    # Listing/Delisting 只做确定性汇总和详情展示，不产出 insight，也不加载 LLM
    # 凭证。这样仅有上/下币数据的一天可以零 LLM 调用完成全流程。
    keys = [k for k in keys if k.category in ANALYZED_CATEGORIES]

    credentials = None
    if keys and not dry_run:
        credentials = load_cursor_credentials() if provider == "cursor_agent" else load_llm_credentials()
        credentials.validate()

    def _budget_exhausted() -> bool:
        return (
            (max_calls_per_run is not None and report.llm_calls >= max_calls_per_run)
            or (max_tokens_per_run is not None and report.total_tokens >= max_tokens_per_run)
        )

    def _call(system: str, user: str, *, max_tokens_this_call: int) -> tuple[str, int]:
        if provider == "cursor_agent":
            raw, tokens_used = call_llm_cursor_agent(system, user, api_key=credentials.api_key, model=credentials.model)
        else:
            raw, tokens_used = call_llm(
                system, user, credentials=credentials, model=credentials.model,
                temperature=llm_cfg.get("temperature", 0), max_tokens=max_tokens_this_call,
                timeout_s=llm_cfg.get("timeout_s", 60), max_retries=llm_cfg.get("max_retries", 3),
            )
        report.llm_calls += 1
        tokens_used = tokens_used or 0
        report.total_tokens += tokens_used
        return raw, tokens_used

    for key in keys:
        rows = get_batch_rows(conn, key.source, key.category, key.locale, batch_date)
        if not rows:
            continue
        related_uids = [r["uid"] for r in rows]
        insight_id = key.id

        derived_from_id = can_derive_from_en(conn, key.source, key.category, key.locale, batch_date)
        if derived_from_id:
            en_insight = get_insight(conn, derived_from_id)
            en_articles = json.loads(en_insight["articles_analysis"] or "[]")
            remapped_articles = _remap_articles_to_locale(conn, en_articles, rows)
            report.derived += 1
            report.batches.append(f"{key.source}/{key.category}/{key.locale} (derived from EN)")
            if not dry_run:
                upsert_insight(
                    conn,
                    insight_id=insight_id, batch_date=batch_date, source=key.source,
                    category=key.category, locale=key.locale, related_uids=related_uids,
                    is_locale_derived=True, derived_from_id=derived_from_id,
                    summary=en_insight["summary"], articles_analysis=remapped_articles,
                    zmx_diff=en_insight["zmx_diff"], diff_type=en_insight["diff_type"],
                    priority=en_insight["priority"],
                    zmx_evidence_uids=json.loads(en_insight["zmx_evidence_uids"] or "[]"),
                    prompt_version=en_insight["prompt_version"], llm_tokens_used=0,
                )
            continue

        old_content_map = _build_old_content_map(conn, rows)

        catalog_pool = get_catalog_digest(
            conn, category=key.category, locale=key.locale,
            max_entries=zmx_cfg.get("max_entries_per_batch", 20),
            max_examples_per_type=zmx_cfg.get("max_examples_per_type", 2),
        )
        catalog_pool = select_relevant_catalog(
            rows, catalog_pool, max_entries=zmx_cfg.get("candidate_entries_per_batch", 8),
        )

        if dry_run:
            print(f"=== {key.source}/{key.category}/{key.locale} batch={batch_date} ===")
            print(f"articles={len(rows)} catalog_pool={len(catalog_pool)} prompt_version={combined_prompt_version}")
            report.batches.append(f"{key.source}/{key.category}/{key.locale} (dry-run)")
            continue

        # ---------------- Stage 1：逐篇事实抽取（per-article 缓存） ----------------
        batch_tokens = 0  # 这一批真实消耗的 token（缓存命中不计），写入 insights.llm_tokens_used
        facts_by_index: dict[int, Any] = {}
        stage1_incomplete = False
        for i, row in enumerate(rows, start=1):
            pre = preprocess_article(
                title=row["title"] or "", content=row["content"] or "",
                old_content=old_content_map.get(row["uid"]),
            )
            cache_key1 = extraction_cache_key(row["content_hash"], model=credentials.model, provider=provider)
            cached1 = get_cached_response(conn, cache_key1)
            if cached1 is not None:
                report.cache_hits += 1
                facts_by_index[i] = validate_fact_extraction(cached1, expected_index=i)
                continue
            if _budget_exhausted():
                stage1_incomplete = True
                break
            prompt1 = build_fact_extraction_prompt(
                index=i, category=key.category, status=row["status"], title=row["title"] or "", preprocessed=pre,
            )
            raw1, tokens1 = _call(prompt1.system, prompt1.user, max_tokens_this_call=max_tokens_per_call.get("article_facts", 400))
            batch_tokens += tokens1
            set_cached_response(conn, cache_key1, raw1)
            facts_by_index[i] = validate_fact_extraction(raw1, expected_index=i)

        if stage1_incomplete:
            # 熔断触发时这一批 Stage1 尚未跑完：已缓存的单篇提取不浪费（llm_cache 里
            # 已经有了），但不写不完整的 insight，整批留到下次重跑（同「跳过，不算
            # 失败」的既有哲学）。
            logger.warning(
                "达到熔断上限（calls=%s tokens=%s），Stage1 未跑完，跳过 %s/%s/%s（下次重跑会重新尝试）",
                report.llm_calls, report.total_tokens, key.source, key.category, key.locale,
            )
            report.skipped_budget_cap += 1
            report.batches.append(f"{key.source}/{key.category}/{key.locale} (skipped: budget cap reached, stage1 incomplete)")
            continue

        # ---------------- Stage 2：确定性候选召回（无 LLM） ----------------
        facts_payload_by_index = {i: _fact_payload(i, fact) for i, fact in facts_by_index.items()}
        candidates_by_index = {
            i: recall_candidates(facts_payload_by_index[i], catalog_pool, top_k=4)
            for i in facts_by_index
        }

        # ---------------- Stage 3：批量业务判断（一次调用） ----------------
        facts_payload = [facts_payload_by_index[i] for i in sorted(facts_payload_by_index)]
        cache_key3 = comparison_cache_key(
            facts_payload, candidates_by_index, prompt_version=business_judgment_version,
            model=credentials.model, provider=provider,
        )
        cached3 = get_cached_response(conn, cache_key3)
        if cached3 is not None:
            raw3 = cached3
            report.cache_hits += 1
        else:
            if _budget_exhausted():
                logger.warning(
                    "达到熔断上限（calls=%s tokens=%s），Stage3 未执行，跳过 %s/%s/%s（下次重跑会重新尝试）",
                    report.llm_calls, report.total_tokens, key.source, key.category, key.locale,
                )
                report.skipped_budget_cap += 1
                report.batches.append(f"{key.source}/{key.category}/{key.locale} (skipped: budget cap reached, stage3 not run)")
                continue
            prompt3 = build_business_judgment_prompt(
                batch_date=batch_date, locale=key.locale, source=key.source, category=key.category,
                facts=facts_payload, candidates_by_index=candidates_by_index,
            )
            raw3, tokens3 = _call(prompt3.system, prompt3.user, max_tokens_this_call=max_tokens_per_call.get("business_judgment", 3000))
            batch_tokens += tokens3
            set_cached_response(conn, cache_key3, raw3)

        judgment = validate_business_judgment(
            raw3, expected_indices=set(facts_by_index.keys()), candidates_by_index=candidates_by_index,
        )
        if not judgment.valid:
            report.validation_failed += 1
        judgment_by_index = {item.index: item for item in judgment.items}

        # ---------------- 合并：逐条组装 + 批次级聚合 ----------------
        articles_analysis: list[dict[str, Any]] = []
        for i, row in enumerate(rows, start=1):
            fact = facts_by_index.get(i)
            item = judgment_by_index.get(i)
            event_type = fact.event_type if fact else "unknown"
            gap_type = item.gap_type if item else "not_applicable"
            diff_type = item.diff_type if item else "不适用"
            business_impact = item.business_impact if item else "low"
            novelty = item.novelty if item else 0
            urgency = item.urgency if item else 0
            confidence = fact.confidence if fact else 0.0

            _, priority = calculate_priority(
                event_type=event_type, gap_type=gap_type, business_impact=business_impact,
                confidence=confidence, novelty=novelty, urgency=urgency,
            )

            top_candidates = candidates_by_index.get(i, [])
            zmx_mechanism_type = top_candidates[0].mechanism_type if top_candidates else None

            change_kind = None
            if key.category == "campaign" and row["status"] == "changed":
                change_kind = _EVENT_TYPE_TO_CHANGE_KIND.get(event_type, "other")

            articles_analysis.append({
                "uid": row["uid"],
                "event_type": event_type,
                "mechanism": fact.mechanism if fact else None,
                "feature": fact.feature if fact else None,
                "start_at": fact.start_at if fact else None,
                "end_at": fact.end_at if fact else None,
                "reward": fact.reward if fact else {},
                "eligibility": fact.eligibility if fact else None,
                "target_users": fact.target_users if fact else [],
                "changes": fact.changes if fact else [],
                "confidence": confidence,
                "mechanism_type": zmx_mechanism_type,
                "diff_type": diff_type,
                "diff_detail": item.reason if item else None,
                "zmx_counterpart_uids": item.zmx_evidence_uids if item else [],
                "priority": priority,
                "change_kind": change_kind,
            })

        batch_diff_type = aggregate_batch_diff_type([a["diff_type"] for a in articles_analysis])
        batch_priority = _aggregate_batch_priority([a["priority"] for a in articles_analysis])
        summary = _synthesize_batch_summary(articles_analysis)
        zmx_diff = _synthesize_batch_zmx_diff(articles_analysis)
        zmx_evidence_uids = _dedupe_preserve_order(
            [uid for a in articles_analysis for uid in a["zmx_counterpart_uids"]]
        )

        upsert_insight(
            conn,
            insight_id=insight_id, batch_date=batch_date, source=key.source,
            category=key.category, locale=key.locale, related_uids=related_uids,
            is_locale_derived=False, derived_from_id=None,
            summary=summary, articles_analysis=articles_analysis,
            zmx_diff=zmx_diff, diff_type=batch_diff_type, priority=batch_priority,
            zmx_evidence_uids=zmx_evidence_uids, prompt_version=combined_prompt_version,
            llm_tokens_used=batch_tokens,
        )
        report.analyzed += 1
        report.batches.append(f"{key.source}/{key.category}/{key.locale}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--date", help="YYYY-MM-DD，默认今日 UTC 日期")
    parser.add_argument("--source", help="逗号分隔，默认 Bitunix,Weex")
    parser.add_argument("--category", help="逗号分隔，默认全部（campaign/product/listing/delisting）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", choices=["openai_http", "cursor_agent"],
                         help="覆盖 config/analysis.yaml 的 llm.provider，一次性试跑用")
    parser.add_argument("--max-calls", type=int,
                         help="覆盖 config/analysis.yaml 的 llm.max_calls_per_run，"
                              "达到这个调用次数后跳过剩余批次（不算失败，留到下次重跑）")
    parser.add_argument("--max-tokens", type=int,
                        help="本进程累计 token 熔断；达到后不再发起新调用")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect(args.db)
    try:
        sources = tuple(args.source.split(",")) if args.source else None
        categories = tuple(args.category.split(",")) if args.category else None
        report = run(
            conn, batch_date=args.date, sources=sources, categories=categories, dry_run=args.dry_run,
            provider=args.provider, max_calls=args.max_calls, max_tokens=args.max_tokens,
        )
        if not args.dry_run:
            conn.commit()
        print(f"分析批次数：analyzed={report.analyzed} derived={report.derived} "
              f"cache_hits={report.cache_hits} llm_calls={report.llm_calls} "
              f"skipped_budget_cap={report.skipped_budget_cap} "
              f"validation_failed={report.validation_failed} total_tokens={report.total_tokens}")
        for b in report.batches:
            print(f"  - {b}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
