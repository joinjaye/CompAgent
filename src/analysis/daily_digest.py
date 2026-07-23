"""当日跨类目 Summary（daily digest）——把某个 locale 当天已经产出的全部批次分析
结果（insights 表已有的 summary/zmx_diff）综合成一段简报。是 insights 批次分析的
下一层：insights 是"这个类目今天发生了什么"，daily digest 是"这个 locale 今天
整体发生了什么"。

跟 Phase 4 的四套 category prompt 不是同一个 LLM 调用粒度（那是公告原文 -> 批次
分析，这是批次分析结果 -> 当日综述），所以单独成模块，不并入 run.py 的批次循环，
也不改动 run.py 已经跑通、有 253 个测试兜底的批次逻辑。

本模块只实现"能不能生成"的机制（prompt 构建、缓存 key、校验、调用），dry_run
默认 True，只构建 prompt 和缓存 key、不发任何网络请求——Phase 7 看板集成时明确
要求本 session 不做真实 LLM 调用，见 CLAUDE.md「Phase 7」。缓存复用 Phase 4 已有的
llm_cache 表（不新增 schema），cache_key 只跟"当天这些批次的 id 集合"有关，跟
四套 category prompt 的 compute_cache_key() 是同一个设计思路。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from src.analysis.config import (
    LlmCredentials,
    load_analysis_config,
    load_cursor_credentials,
    load_llm_credentials,
)
from src.analysis.cursor_agent import call_llm_cursor_agent
from src.analysis.llm import call_llm, get_cached_response, set_cached_response
from src.analysis.prompts import BuiltPrompt, build_daily_digest_prompt

logger = logging.getLogger(__name__)

PROMPT_VERSION = "daily-digest-v4"

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def load_locale_batches(conn: sqlite3.Connection, locale: str, batch_date: str) -> list[dict]:
    """取某个 locale 当天已经产出的全部批次（category != other，跟 insights 表本身
    的约束一致——other 从不产出 insights），按 category/source 排列，顺序稳定。"""
    conn.row_factory = sqlite3.Row
    locale_clause = "" if locale == "ALL" else "AND locale = ?"
    params = (batch_date,) if locale == "ALL" else (batch_date, locale)
    rows = conn.execute(
        f"""SELECT id, source, category, locale, article_count, diff_type, priority,
                   summary, zmx_diff, articles_analysis
            FROM insights WHERE batch_date = ? {locale_clause}
            ORDER BY category, source, locale""",
        params,
    ).fetchall()
    batches = []
    for row in rows:
        item = dict(row)
        try:
            articles = json.loads(item.pop("articles_analysis") or "[]")
        except (json.JSONDecodeError, TypeError):
            articles = []
        mechanisms = []
        reward_changes = 0
        for article in articles:
            signal = (
                article.get("mechanism") or article.get("feature")
                or article.get("token_category") or article.get("listing_type")
            )
            if signal and signal not in mechanisms:
                mechanisms.append(str(signal)[:80])
            if article.get("change_kind") == "reward":
                reward_changes += 1
        parts = []
        if mechanisms:
            parts.append("主要信号：" + "；".join(mechanisms[:4]))
        if reward_changes:
            parts.append(f"奖励变化：{reward_changes} 条")
        item["signals"] = "；".join(parts) or "（无额外结构化信号）"
        batches.append(item)
    return batches


def compute_digest_cache_key(batches: list[dict], prompt_version: str = PROMPT_VERSION) -> str:
    """缓存键覆盖批次 id、摘要、结构化信号、数量和差异结论，且与查询顺序无关。
    因此同一批次被重新分析但 id 不变时，只要内容变化也会失效，避免复用旧 Insight。"""
    normalized = [
        {
            "id": b["id"], "summary": b.get("summary"), "signals": b.get("signals"),
            "article_count": b.get("article_count"), "diff_type": b.get("diff_type"),
        }
        for b in sorted(batches, key=lambda item: item["id"])
    ]
    fingerprint = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    inner = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{inner}|{prompt_version}".encode("utf-8")).hexdigest()


@dataclass
class DailyDigestResult:
    generated: bool  # False = dry_run（有批次），或响应校验失败
    from_cache: bool = False
    daily_summary: Optional[str] = None
    campaign_summary: Optional[str] = None
    product_summary: Optional[str] = None
    priority_focus: Optional[str] = None
    tokens_used: Optional[int] = None
    cache_key: Optional[str] = None
    prompt: Optional[BuiltPrompt] = None
    issues: list[str] = field(default_factory=list)


def _empty_day_digest() -> DailyDigestResult:
    """没有可分析批次是正常业务状态，不应让生产日报失败或调用 LLM。"""
    return DailyDigestResult(
        generated=True,
        from_cache=False,
        daily_summary=(
            "今日未发现可纳入分析的竞品 Campaign、Product 或 Listing & Delisting 更新。"
            "看板继续展示最近已入库的历史数据，本批次不生成新的趋势结论。"
        ),
        campaign_summary=(
            "今日未发现可分析的 Campaign 更新。"
            "现有活动记录继续保留在看板和多维表格中，等待下一批有效变化。"
        ),
        product_summary=(
            "今日未发现可分析的 Product 更新。"
            "现有产品记录继续保留在看板和多维表格中，等待下一批有效变化。"
        ),
        tokens_used=0,
        issues=["no_batches_for_locale_date"],
    )


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def _validate_digest_response(raw_text: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], list[str]]:
    """比四套 category prompt 的校验简单得多：只要求 daily_summary 是非空字符串，
    priority_focus 可选。JSON 解析失败或 daily_summary 缺失都返回 (None, None, issues)
    ——上层据此判断"这次调用没有产出可用摘要"，不拿半成品充数。"""
    try:
        data = json.loads(_strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        return None, None, None, None, [f"json_parse_failed: {e}"]
    if not isinstance(data, dict):
        return None, None, None, None, ["response_not_object"]
    summary = data.get("daily_summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, None, None, None, ["missing_daily_summary"]
    issues = []
    summaries = {
        "daily_summary": summary,
        "campaign_summary": data.get("campaign_summary"),
        "product_summary": data.get("product_summary"),
    }
    for field_name, value in summaries.items():
        if value is None and field_name != "daily_summary":
            continue
        if not isinstance(value, str) or not value.strip():
            issues.append(f"invalid_{field_name}")
            continue
        sentences = [part for part in re.split(r"[。！？!?]+", value.strip()) if part.strip()]
        if not 2 <= len(sentences) <= 4:
            issues.append(f"{field_name}_sentence_count:{len(sentences)}")
    if issues:
        return None, None, None, None, issues
    focus = data.get("priority_focus")
    campaign = summaries["campaign_summary"]
    product = summaries["product_summary"]
    return summary, campaign, product, (focus if isinstance(focus, str) else None), []


def peek_cached_digest(conn: sqlite3.Connection, locale: str, batch_date: str) -> Optional[DailyDigestResult]:
    """只读缓存，从不调用 LLM——给 src/dashboard 导出层用：看板导出是一次性的静态
    快照生成，不应该在渲染数据的过程中触发网络请求；如果 daily digest 还没有被
    真正跑过（真实生产环境里应该是 `python -m src.analysis daily-digest` 之类的
    独立步骤，见模块顶部说明，本次 session 未实现调度触发），返回 None，
    调用方据此展示"待生成"占位而不是报错。无批次时直接返回确定性的空数据日摘要。"""
    batches = load_locale_batches(conn, locale, batch_date)
    if not batches:
        return _empty_day_digest()
    cache_key = compute_digest_cache_key(batches)
    cached = get_cached_response(conn, cache_key)
    if cached is None:
        return None
    summary, campaign, product, focus, issues = _validate_digest_response(cached)
    return DailyDigestResult(
        generated=summary is not None, from_cache=True, daily_summary=summary,
        campaign_summary=campaign, product_summary=product, priority_focus=focus,
        tokens_used=0, cache_key=cache_key, issues=issues,
    )


def generate_daily_digest(
    conn: sqlite3.Connection,
    locale: str,
    batch_date: str,
    *,
    credentials: Optional[LlmCredentials] = None,
    provider: str = "openai_http",
    model: str = "",
    temperature: float = 0.3,
    max_tokens: int = 800,
    timeout_s: float = 60.0,
    max_retries: int = 3,
    dry_run: bool = True,
) -> DailyDigestResult:
    """dry_run=True（默认）：只构建 prompt + cache_key，不查缓存、不调用 LLM，
    有批次时 generated 恒为 False——用于"看逻辑对不对/看 prompt 长什么样"，不产出内容；
    无批次时返回确定性的空数据日摘要，不构建 prompt、也不调用 LLM。
    dry_run=False：先查 llm_cache，命中则复用（tokens_used=0，跟 Phase 4 批次分析
    对 EN->FR/IDcache 复用的语义一致）；未命中才真正调用，需要传入 credentials。
    provider="cursor_agent" 时 credentials 应为 CursorCredentials（走
    call_llm_cursor_agent，不是 OpenAI 兼容协议），跟 run.py/zmx_catalog.py 的
    provider 切换是同一个模式。
    """
    batches = load_locale_batches(conn, locale, batch_date)
    if not batches:
        return _empty_day_digest()

    prompt = build_daily_digest_prompt(locale, batch_date, batches)
    cache_key = compute_digest_cache_key(batches)

    if dry_run:
        return DailyDigestResult(generated=False, cache_key=cache_key, prompt=prompt)

    cached = get_cached_response(conn, cache_key)
    if cached is not None:
        summary, campaign, product, focus, issues = _validate_digest_response(cached)
        return DailyDigestResult(
            generated=summary is not None, from_cache=True, daily_summary=summary,
            campaign_summary=campaign, product_summary=product, priority_focus=focus,
            tokens_used=0, cache_key=cache_key, prompt=prompt, issues=issues,
        )

    if credentials is None:
        raise ValueError("dry_run=False 且缓存未命中时必须传入 credentials")

    if provider == "cursor_agent":
        raw_text, tokens_used = call_llm_cursor_agent(
            prompt.system, prompt.user, api_key=credentials.api_key, model=credentials.model,
        )
    else:
        raw_text, tokens_used = call_llm(
            prompt.system, prompt.user, credentials=credentials, model=model,
            temperature=temperature, max_tokens=max_tokens, timeout_s=timeout_s, max_retries=max_retries,
        )
    set_cached_response(conn, cache_key, raw_text)
    summary, campaign, product, focus, issues = _validate_digest_response(raw_text)
    return DailyDigestResult(
        generated=summary is not None, from_cache=False, daily_summary=summary,
        campaign_summary=campaign, product_summary=product, priority_focus=focus,
        tokens_used=tokens_used, cache_key=cache_key, prompt=prompt, issues=issues,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成跨市场竞品情报 Insight，并写入 LLM 缓存")
    parser.add_argument("--db", default="data/competitor_intel.db")
    parser.add_argument("--date", required=True)
    parser.add_argument("--provider", choices=["openai_http", "cursor_agent"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--require-generated", action="store_true",
        help="未产出完整的 Overview/Campaign/Product 摘要时以非零状态退出（生产流水线使用）",
    )
    args = parser.parse_args()

    config = load_analysis_config().get("llm", {})
    provider = args.provider or config.get("provider", "openai_http")
    credentials = load_cursor_credentials() if provider == "cursor_agent" else load_llm_credentials()
    if not args.dry_run:
        credentials.validate()
    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        result = generate_daily_digest(
            conn, "ALL", args.date, credentials=credentials, provider=provider,
            temperature=config.get("temperature", 0),
            max_tokens=config.get("max_tokens_per_call", {}).get("daily_digest", 800),
            timeout_s=config.get("timeout_s", 60), max_retries=config.get("max_retries", 3),
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            conn.commit()
        print(json.dumps({
            "generated": result.generated, "from_cache": result.from_cache,
            "tokens_used": result.tokens_used, "issues": result.issues,
            "daily_summary": result.daily_summary,
            "campaign_summary": result.campaign_summary,
            "product_summary": result.product_summary,
        }, ensure_ascii=False))
        if args.require_generated and not result.generated:
            raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
