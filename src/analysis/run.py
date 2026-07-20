"""Phase 4 批次编排：按 (source, category, locale) 分组当日 status IN (new, changed)
的公告，每组一次分析（要么复用同日 EN 批次、要么调 LLM），写一行 insights。

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
    call_llm,
    compute_cache_key,
    get_cached_response,
    set_cached_response,
    validate_and_normalize,
)
from src.analysis.prompts import build_prompt
from src.analysis.zmx_baseline import get_baseline_digest
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import get_content_history, utcnow_iso

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = ("Bitunix", "Weex")  # 竞品源；Zoomex 是基线，不作为被分析对象


def today_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    """粗略估算：英文/多数拉丁语系约 4 字符一个 token。dry-run 摸底用，不追求精确。"""
    return max(1, len(text) // 4)


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
    本 locale 对应的 uid（通过 group_id 匹配）。EN 侧找不到 group_id、或本 locale 批次
    没有对应 group_id 的条目会被跳过（理论上不该发生——can_derive_from_en 已经保证
    子集关系，这里只是防御）。
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
    skipped_call_cap: int = 0
    batches: list[str] = field(default_factory=list)


def run(
    conn: sqlite3.Connection,
    batch_date: Optional[str] = None,
    sources: Optional[tuple[str, ...]] = None,
    categories: Optional[tuple[str, ...]] = None,
    dry_run: bool = False,
    provider: Optional[str] = None,
    max_calls: Optional[int] = None,
) -> RunReport:
    """provider/max_calls 显式传参时覆盖 config/analysis.yaml 的 llm.provider /
    llm.max_calls_per_run（CLI --provider/--max-calls 用这个覆盖来做一次性试跑，不用
    每次改 yaml）。
    """
    batch_date = batch_date or today_utc_date()
    sources = sources or DEFAULT_SOURCES
    cfg = load_analysis_config()
    llm_cfg = cfg.get("llm", {})
    provider = provider or llm_cfg.get("provider", "openai_http")
    max_calls_per_run = max_calls if max_calls is not None else llm_cfg.get("max_calls_per_run")

    credentials = None
    if not dry_run:
        credentials = load_cursor_credentials() if provider == "cursor_agent" else load_llm_credentials()
        credentials.validate()

    zmx_cfg = cfg.get("zmx_baseline", {})
    trunc_cfg = cfg.get("content_truncation", {})
    prompt_versions = cfg.get("prompt_versions", {})
    max_tokens_by_category = llm_cfg.get("max_tokens_by_category", {})

    report = RunReport()
    keys = list_batch_keys(conn, sources, batch_date)
    if categories:
        keys = [k for k in keys if k.category in categories]

    for key in keys:
        rows = get_batch_rows(conn, key.source, key.category, key.locale, batch_date)
        if not rows:
            continue
        related_uids = [r["uid"] for r in rows]
        insight_id = key.id
        prompt_version = prompt_versions.get(key.category, f"{key.category}-v1")

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

        zmx_hits = []
        if key.category != "delisting":
            zmx_hits = get_baseline_digest(
                conn, category=key.category, locale=key.locale,
                lookback_days=zmx_cfg.get("lookback_days", 90),
                max_entries=zmx_cfg.get("max_entries_per_batch", 20),
                max_examples_per_type=zmx_cfg.get("max_examples_per_type", 2),
            )

        prompt = build_prompt(
            key.category, source=key.source, locale=key.locale, batch_date=batch_date,
            rows=rows, old_content_by_uid=old_content_map, zmx_hits=zmx_hits,
            article_content_chars=trunc_cfg.get("article_content_chars", 4000),
        )

        if dry_run:
            token_estimate = estimate_tokens(prompt.system) + estimate_tokens(prompt.user)
            print(f"=== {key.source}/{key.category}/{key.locale} batch={batch_date} ===")
            print(f"articles={len(rows)} zmx_hits={len(zmx_hits)} prompt_version={prompt_version}")
            print(f"预估 tokens ≈ {token_estimate}")
            print(prompt.user[:500])
            print("...\n")
            report.batches.append(f"{key.source}/{key.category}/{key.locale} (dry-run)")
            continue

        content_hashes = [r["content_hash"] for r in rows]
        cache_key = compute_cache_key(content_hashes, prompt_version)
        cached = get_cached_response(conn, cache_key)
        if cached is not None:
            raw_text = cached
            tokens_used = 0
            report.cache_hits += 1
        elif max_calls_per_run is not None and report.llm_calls >= max_calls_per_run:
            # 熔断：达到调用次数上限，跳过剩余批次（不写 insight，留到下次重跑；不算失败）
            logger.warning(
                "达到 max_calls_per_run=%s，跳过 %s/%s/%s（下次重跑会重新尝试）",
                max_calls_per_run, key.source, key.category, key.locale,
            )
            report.skipped_call_cap += 1
            report.batches.append(f"{key.source}/{key.category}/{key.locale} (skipped: call cap reached)")
            continue
        else:
            if provider == "cursor_agent":
                raw_text, tokens_used = call_llm_cursor_agent(
                    prompt.system, prompt.user,
                    api_key=credentials.api_key, model=credentials.model,
                )
            else:
                raw_text, tokens_used = call_llm(
                    prompt.system, prompt.user,
                    credentials=credentials, model=credentials.model,
                    temperature=llm_cfg.get("temperature", 0),
                    max_tokens=max_tokens_by_category.get(key.category, 1500),
                    timeout_s=llm_cfg.get("timeout_s", 60),
                    max_retries=llm_cfg.get("max_retries", 3),
                )
            set_cached_response(conn, cache_key, raw_text)
            report.llm_calls += 1
            report.total_tokens += tokens_used or 0

        article_status = {r["uid"]: r["status"] for r in rows}
        result = validate_and_normalize(
            raw_text, category=key.category, related_uids=set(related_uids), zmx_hits=zmx_hits,
            article_status=article_status,
        )
        if not result.valid:
            report.validation_failed += 1

        upsert_insight(
            conn,
            insight_id=insight_id, batch_date=batch_date, source=key.source,
            category=key.category, locale=key.locale, related_uids=related_uids,
            is_locale_derived=False, derived_from_id=None,
            summary=result.summary, articles_analysis=result.articles_analysis if result.valid else None,
            zmx_diff=result.zmx_diff, diff_type=result.diff_type, priority=result.priority,
            zmx_evidence_uids=result.zmx_evidence_uids, prompt_version=prompt_version,
            llm_tokens_used=tokens_used,
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect(args.db)
    try:
        sources = tuple(args.source.split(",")) if args.source else None
        categories = tuple(args.category.split(",")) if args.category else None
        report = run(
            conn, batch_date=args.date, sources=sources, categories=categories, dry_run=args.dry_run,
            provider=args.provider, max_calls=args.max_calls,
        )
        if not args.dry_run:
            conn.commit()
        print(f"分析批次数：analyzed={report.analyzed} derived={report.derived} "
              f"cache_hits={report.cache_hits} llm_calls={report.llm_calls} "
              f"skipped_call_cap={report.skipped_call_cap} "
              f"validation_failed={report.validation_failed} total_tokens={report.total_tokens}")
        for b in report.batches:
            print(f"  - {b}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
