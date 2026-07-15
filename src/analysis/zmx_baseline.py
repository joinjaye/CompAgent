"""Zoomex（我方基线）结构化提取 + 查询。

取代原来的 src/analysis/zmx_index.py（纯 TF-IDF 原文检索）：那套设计把一个批次里
混杂的多种玩法（如入金活动/交易赛/新手任务/邀请返佣）合并成一个 query 检索 Top 5，
检索结果未必能覆盖批次内全部玩法类型，LLM 容易把"没检索到"误判成"Zoomex 没有"。

本模块分两部分：

1. **提取**（`run_extraction()`，对应 CLI `python -m src.analysis.zmx_baseline`）：
   把 Zoomex 公告（campaign/product/listing，delisting 不建基线）结构化成
   mechanism_type/key_mechanics/reward_range/target_users/start_date/end_date
   六个字段，写入 `zmx_baseline` 表。跟 `python -m src.analysis`（竞品批次分析）
   完全解耦，是独立维护步骤，不在竞品分析运行时现场触发。

   **只处理近 lookback_days（默认 90）天窗口内的 Zoomex 公告，这是结构性约束，不是
   默认参数**——`list_pending_zoomex_rows()` 的 SQL 里 `post_time >= cutoff` 过滤
   没有绕过开关，任何一次运行都不可能把窗口外的历史记录发给 LLM。

   真实执行受 CLI `--max-cost-usd`/`--max-tokens`/`--max-calls` 三个熔断上限控制
   （任一触发即停止，已产出的结果保留、每处理完一个批次就 commit，不会因为熔断或
   意外中断丢失已完成的部分）。

2. **查询**（`get_baseline_digest()`，供 `run.py` 竞品批次分析注入 prompt）：
   按 category×locale 拉取近 lookback_days 天的结构化基线，按 mechanism_type 分组，
   **类型覆盖优先于单类型深度**——先保证每个已知类型都有代表条目，预算
   （max_entries）还有余量时才按类型轮询补充同类型的第二个例子。这是直接解决
   "多种玩法混在一个批次、检索结果覆盖不全"这个核心问题的机制。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.analysis.config import load_analysis_config, load_cursor_credentials, load_llm_credentials
from src.analysis.cursor_agent import call_llm_cursor_agent
from src.analysis.llm import call_llm, compute_cache_key, get_cached_response, set_cached_response, strip_code_fences
from src.analysis.prompts import build_extraction_prompt
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import utcnow_iso

logger = logging.getLogger(__name__)

# delisting 不建基线：跟 prompts.py 的 delisting 模板一致（没有 zmx_comparison 部分），
# run.py 里 `if key.category != "delisting"` 这个分支现在守护的就是这张表。
BASELINE_CATEGORIES = ("campaign", "product", "listing")


def _cutoff_iso(lookback_days: int, reference_date: Optional[datetime] = None) -> str:
    reference_date = reference_date or datetime.now(timezone.utc)
    return (reference_date - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ============================================================
# 提取
# ============================================================


def list_zoomex_locale_categories(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Zoomex 实际存在数据的 (locale, category) 组合，category 限定
    campaign/product/listing。"""
    placeholders = ",".join("?" * len(BASELINE_CATEGORIES))
    rows = conn.execute(
        f"""
        SELECT DISTINCT locale, category FROM announcements
        WHERE source = 'Zoomex' AND category IN ({placeholders})
        """,
        BASELINE_CATEGORIES,
    ).fetchall()
    return sorted((r["locale"], r["category"]) for r in rows)


def list_pending_zoomex_rows(
    conn: sqlite3.Connection,
    *,
    category: str,
    locale: str,
    lookback_days: int,
    reference_date: Optional[datetime] = None,
) -> list[sqlite3.Row]:
    """近 lookback_days 天窗口内、尚未提取或 content_hash 已变化的 Zoomex 公告。

    `post_time >= cutoff` 这个过滤没有绕过开关——不提供"不限时间窗口"的调用方式，
    结构性保证任何一次提取都只处理窗口内数据。
    """
    cutoff = _cutoff_iso(lookback_days, reference_date)
    return conn.execute(
        """
        SELECT a.* FROM announcements a
        LEFT JOIN zmx_baseline b ON b.source_uid = a.uid
        WHERE a.source = 'Zoomex' AND a.category = ? AND a.locale = ?
              AND a.post_time IS NOT NULL AND a.post_time >= ?
              AND a.content IS NOT NULL AND a.content != ''
              AND (b.source_uid IS NULL OR b.content_hash != a.content_hash)
        ORDER BY a.post_time DESC
        """,
        (category, locale, cutoff),
    ).fetchall()


def list_existing_labels(conn: sqlite3.Connection, *, category: str, locale: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT mechanism_type FROM zmx_baseline
        WHERE category = ? AND locale = ? ORDER BY mechanism_type
        """,
        (category, locale),
    ).fetchall()
    return [r["mechanism_type"] for r in rows]


def parse_extraction_response(raw_text: str, related_uids: set[str]) -> list[dict]:
    """解析提取响应：JSON 解析失败或不是 object 时返回空列表（本批次跳过，下次重跑
    会重新尝试，因为 content_hash 没有被 upsert 过，仍然在 pending 候选集合里）；
    `uid` 不在 `related_uids` 内的条目丢弃；`mechanism_type` 缺失/空时兜底填「其他」
    （不整条丢弃——提取失败不该让这条 Zoomex 公告永远没有基线记录）。
    """
    try:
        data = json.loads(strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Zoomex 提取响应 JSON 解析失败，本批次跳过：%s", e)
        return []

    if not isinstance(data, dict):
        logger.error("Zoomex 提取响应不是 JSON object，本批次跳过")
        return []

    articles_raw = data.get("articles")
    if not isinstance(articles_raw, list):
        return []

    def _str_or_none(value) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    results: list[dict] = []
    for item in articles_raw:
        if not isinstance(item, dict):
            continue
        uid = item.get("uid")
        if uid not in related_uids:
            logger.warning("丢弃提取条目：uid=%r 不在本批次内", uid)
            continue
        mechanism_type = _str_or_none(item.get("mechanism_type")) or "其他"
        results.append(
            {
                "uid": uid,
                "mechanism_type": mechanism_type,
                "key_mechanics": _str_or_none(item.get("key_mechanics")),
                "reward_range": _str_or_none(item.get("reward_range")),
                "target_users": _str_or_none(item.get("target_users")),
                "start_date": _str_or_none(item.get("start_date")),
                "end_date": _str_or_none(item.get("end_date")),
            }
        )
    return results


def upsert_baseline_rows(
    conn: sqlite3.Connection,
    *,
    category: str,
    locale: str,
    rows: list[sqlite3.Row],
    parsed: list[dict],
    extraction_version: str,
) -> int:
    """按 source_uid 幂等 upsert。返回实际写入的行数。"""
    content_hash_by_uid = {r["uid"]: r["content_hash"] for r in rows}
    title_by_uid = {r["uid"]: r["title"] for r in rows}
    now = utcnow_iso()
    written = 0
    for item in parsed:
        uid = item["uid"]
        content_hash = content_hash_by_uid.get(uid)
        if content_hash is None:
            continue  # 防御：parse_extraction_response 已经过滤过不在批次内的 uid
        conn.execute(
            """
            INSERT INTO zmx_baseline (
                source_uid, locale, category, mechanism_type, title, key_mechanics,
                reward_range, target_users, start_date, end_date, content_hash,
                extraction_version, extracted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_uid) DO UPDATE SET
                mechanism_type = excluded.mechanism_type,
                title = excluded.title,
                key_mechanics = excluded.key_mechanics,
                reward_range = excluded.reward_range,
                target_users = excluded.target_users,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                content_hash = excluded.content_hash,
                extraction_version = excluded.extraction_version,
                extracted_at = excluded.extracted_at
            """,
            (
                uid, locale, category, item["mechanism_type"], title_by_uid.get(uid),
                item["key_mechanics"], item["reward_range"], item["target_users"],
                item["start_date"], item["end_date"], content_hash, extraction_version, now,
            ),
        )
        written += 1
    return written


@dataclass
class ExtractionReport:
    extracted: int = 0
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
    lookback_days: Optional[int] = None,
    batch_size: Optional[int] = None,
    provider: Optional[str] = None,
    max_calls: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    max_tokens: Optional[int] = None,
    dry_run: bool = False,
    reference_date: Optional[datetime] = None,
) -> ExtractionReport:
    """不传 locale/category 时遍历 Zoomex 全部已有数据的 locale × category 组合
    （跟 `python -m src.collectors --source zoomex` 不传 --locale 时遍历全部组合是
    同一个约定）。

    max_calls/max_cost_usd/max_tokens 三个熔断上限任一触发就停止对剩余批次发起新的
    LLM 调用（已产出的照常保留，跳过的批次留到下次重跑，不算失败）——跟 run.py 里
    `max_calls_per_run` 熔断是同一个模式，这里额外加了成本/token 两种判断口径，
    因为用户对本次提取有明确的美元预算上限。
    """
    cfg = load_analysis_config()
    baseline_cfg = cfg.get("zmx_baseline", {})
    extraction_cfg = baseline_cfg.get("extraction", {})
    llm_cfg = cfg.get("llm", {})

    lookback_days = lookback_days if lookback_days is not None else baseline_cfg.get("lookback_days", 90)
    batch_size = batch_size if batch_size is not None else extraction_cfg.get("batch_size", 15)
    provider = provider or llm_cfg.get("provider", "openai_http")
    max_calls_cap = max_calls if max_calls is not None else extraction_cfg.get("max_calls_per_run")
    max_cost_cap = max_cost_usd if max_cost_usd is not None else extraction_cfg.get("max_cost_usd_per_run")
    max_tokens_cap = max_tokens if max_tokens is not None else extraction_cfg.get("max_tokens_per_run")
    price_per_1k = extraction_cfg.get("price_usd_per_1k_tokens", 0.0)
    extraction_version = extraction_cfg.get("prompt_version", "zmx-extract-v1")
    response_max_tokens = extraction_cfg.get("response_max_tokens", 3000)
    article_content_chars = cfg.get("content_truncation", {}).get("article_content_chars", 4000)

    credentials = None
    if not dry_run:
        credentials = load_cursor_credentials() if provider == "cursor_agent" else load_llm_credentials()
        credentials.validate()

    if locale and category:
        combos = [(locale, category)]
    elif locale:
        combos = [(locale, c) for c in BASELINE_CATEGORIES]
    elif category:
        combos = [(l, c) for l, c in list_zoomex_locale_categories(conn) if c == category]
    else:
        combos = list_zoomex_locale_categories(conn)

    report = ExtractionReport()

    for loc, cat in combos:
        pending_rows = list_pending_zoomex_rows(
            conn, category=cat, locale=loc, lookback_days=lookback_days, reference_date=reference_date
        )
        if not pending_rows:
            continue
        existing_labels = list_existing_labels(conn, category=cat, locale=loc)

        for batch_rows in _chunk(pending_rows, batch_size):
            related_uids = {r["uid"] for r in batch_rows}
            prompt = build_extraction_prompt(
                category=cat, locale=loc, rows=batch_rows, existing_labels=existing_labels,
                article_content_chars=article_content_chars,
            )

            if dry_run:
                report.combos.append(f"{loc}/{cat} (dry-run, {len(batch_rows)} 条)")
                continue

            content_hashes = [r["content_hash"] for r in batch_rows]
            cache_key = compute_cache_key(content_hashes, extraction_version)
            cached = get_cached_response(conn, cache_key)
            if cached is not None:
                raw_text = cached
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
                        temperature=llm_cfg.get("temperature", 0),
                        max_tokens=response_max_tokens,
                        timeout_s=llm_cfg.get("timeout_s", 60),
                        max_retries=llm_cfg.get("max_retries", 3),
                    )
                set_cached_response(conn, cache_key, raw_text)
                report.llm_calls += 1
                tokens_used = tokens_used or 0
                report.total_tokens += tokens_used
                report.total_cost_usd += tokens_used / 1000 * price_per_1k

            parsed = parse_extraction_response(raw_text, related_uids)
            if not parsed:
                report.validation_failed += 1
            written = upsert_baseline_rows(
                conn, category=cat, locale=loc, rows=batch_rows, parsed=parsed,
                extraction_version=extraction_version,
            )
            report.extracted += written
            report.combos.append(f"{loc}/{cat} ({written}/{len(batch_rows)} 条)")
            # 每个批次落盘一次：熔断或任何意外中断都不会丢失已经产出的提取结果
            conn.commit()

    return report


# ============================================================
# 查询
# ============================================================


@dataclass
class ZmxBaselineEntry:
    uid: str
    title: Optional[str]
    mechanism_type: str
    key_mechanics: Optional[str]
    reward_range: Optional[str]
    target_users: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    post_time: Optional[str]


def get_baseline_digest(
    conn: sqlite3.Connection,
    *,
    category: str,
    locale: str,
    lookback_days: int = 90,
    max_entries: int = 20,
    max_examples_per_type: int = 2,
    reference_date: Optional[datetime] = None,
) -> list[ZmxBaselineEntry]:
    """按 category×locale 拉取近 lookback_days 天的结构化基线，类型覆盖优先于单
    类型深度：

    1. 按 mechanism_type 分组（按 post_time 降序排列后分组，组内天然按时间新旧排）。
    2. 第一轮：每个类型取最新 1 条，保证 max_entries 预算允许范围内类型全覆盖。
    3. 第二轮：预算还有余量时，按类型轮询补充同类型的下一条（最多到
       max_examples_per_type 条），让同一类型下也能看到一点变化范围（如奖池区间的
       浮动），但类型覆盖始终优先于单类型深度。

    JOIN announcements 拿 post_time 做时效过滤，不在 zmx_baseline 里冗余存一份
    ——遵守"SQLite 里 announcements 是唯一真相源"的既有原则。
    """
    cutoff = _cutoff_iso(lookback_days, reference_date)
    rows = conn.execute(
        """
        SELECT b.source_uid AS uid, b.title, b.mechanism_type, b.key_mechanics,
               b.reward_range, b.target_users, b.start_date, b.end_date, a.post_time
        FROM zmx_baseline b
        JOIN announcements a ON a.uid = b.source_uid
        WHERE b.category = ? AND b.locale = ?
              AND a.post_time IS NOT NULL AND a.post_time >= ?
        ORDER BY a.post_time DESC
        """,
        (category, locale, cutoff),
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
        ZmxBaselineEntry(
            uid=r["uid"], title=r["title"], mechanism_type=r["mechanism_type"],
            key_mechanics=r["key_mechanics"], reward_range=r["reward_range"],
            target_users=r["target_users"], start_date=r["start_date"], end_date=r["end_date"],
            post_time=r["post_time"],
        )
        for r in selected
    ]


# ============================================================
# CLI
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--locale", help="不传则遍历 Zoomex 全部已有数据的 locale")
    parser.add_argument("--category", choices=list(BASELINE_CATEGORIES), help="不传则遍历 campaign/product/listing")
    parser.add_argument("--lookback-days", type=int, help="默认读 config/analysis.yaml 的 zmx_baseline.lookback_days（90）")
    parser.add_argument("--batch-size", type=int, help="默认读 config/analysis.yaml 的 extraction.batch_size（15）")
    parser.add_argument("--provider", choices=["openai_http", "cursor_agent"],
                         help="覆盖 config/analysis.yaml 的 llm.provider")
    parser.add_argument("--max-calls", type=int, help="熔断上限：调用次数")
    parser.add_argument("--max-cost-usd", type=float, help="熔断上限：累计美元成本")
    parser.add_argument("--max-tokens", type=int, help="熔断上限：累计 token 数")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = connect(args.db)
    try:
        report = run_extraction(
            conn, locale=args.locale, category=args.category, lookback_days=args.lookback_days,
            batch_size=args.batch_size, provider=args.provider, max_calls=args.max_calls,
            max_cost_usd=args.max_cost_usd, max_tokens=args.max_tokens, dry_run=args.dry_run,
        )
        if not args.dry_run:
            conn.commit()
        print(
            f"提取结果：extracted={report.extracted} cache_hits={report.cache_hits} "
            f"llm_calls={report.llm_calls} validation_failed={report.validation_failed} "
            f"total_tokens={report.total_tokens} total_cost_usd={report.total_cost_usd:.4f} "
            f"skipped_budget_cap={report.skipped_budget_cap}"
        )
        for c in report.combos:
            print(f"  - {c}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
