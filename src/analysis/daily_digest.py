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

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from src.analysis.config import LlmCredentials
from src.analysis.cursor_agent import call_llm_cursor_agent
from src.analysis.llm import call_llm, get_cached_response, set_cached_response
from src.analysis.prompts import BuiltPrompt, build_daily_digest_prompt

logger = logging.getLogger(__name__)

PROMPT_VERSION = "daily-digest-v1"

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def load_locale_batches(conn: sqlite3.Connection, locale: str, batch_date: str) -> list[dict]:
    """取某个 locale 当天已经产出的全部批次（category != other，跟 insights 表本身
    的约束一致——other 从不产出 insights），按 category/source 排列，顺序稳定。"""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, source, category, article_count, diff_type, priority, summary, zmx_diff
           FROM insights WHERE locale = ? AND batch_date = ?
           ORDER BY category, source""",
        (locale, batch_date),
    ).fetchall()
    return [dict(r) for r in rows]


def compute_digest_cache_key(batches: list[dict], prompt_version: str = PROMPT_VERSION) -> str:
    """跟 llm.compute_cache_key 同样的思路：key 只跟"当天这些批次的 id 集合"有关，
    不跟查询返回顺序有关——同一天批次集合没变时复用缓存，任何一个批次 id 变化
    （新增批次/批次被重跑产生新 id）都会让 key 变化，不会复用过期的当日综述。"""
    fingerprint = "".join(sorted(b["id"] for b in batches))
    inner = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{inner}|{prompt_version}".encode("utf-8")).hexdigest()


@dataclass
class DailyDigestResult:
    generated: bool  # False = dry_run，或本日没有任何批次，或响应校验失败
    from_cache: bool = False
    daily_summary: Optional[str] = None
    priority_focus: Optional[str] = None
    tokens_used: Optional[int] = None
    cache_key: Optional[str] = None
    prompt: Optional[BuiltPrompt] = None
    issues: list[str] = field(default_factory=list)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def _validate_digest_response(raw_text: str) -> tuple[Optional[str], Optional[str], list[str]]:
    """比四套 category prompt 的校验简单得多：只要求 daily_summary 是非空字符串，
    priority_focus 可选。JSON 解析失败或 daily_summary 缺失都返回 (None, None, issues)
    ——上层据此判断"这次调用没有产出可用摘要"，不拿半成品充数。"""
    try:
        data = json.loads(_strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        return None, None, [f"json_parse_failed: {e}"]
    if not isinstance(data, dict):
        return None, None, ["response_not_object"]
    summary = data.get("daily_summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, None, ["missing_daily_summary"]
    focus = data.get("priority_focus")
    return summary, (focus if isinstance(focus, str) else None), []


def peek_cached_digest(conn: sqlite3.Connection, locale: str, batch_date: str) -> Optional[DailyDigestResult]:
    """只读缓存，从不调用 LLM——给 src/dashboard 导出层用：看板导出是一次性的静态
    快照生成，不应该在渲染数据的过程中触发网络请求；如果 daily digest 还没有被
    真正跑过（真实生产环境里应该是 `python -m src.analysis daily-digest` 之类的
    独立步骤，见模块顶部说明，本次 session 未实现调度触发），返回 None，
    调用方据此展示"待生成"占位而不是报错。"""
    batches = load_locale_batches(conn, locale, batch_date)
    if not batches:
        return None
    cache_key = compute_digest_cache_key(batches)
    cached = get_cached_response(conn, cache_key)
    if cached is None:
        return None
    summary, focus, issues = _validate_digest_response(cached)
    return DailyDigestResult(
        generated=summary is not None, from_cache=True, daily_summary=summary,
        priority_focus=focus, tokens_used=0, cache_key=cache_key, issues=issues,
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
    generated 恒为 False——用于"看逻辑对不对/看 prompt 长什么样"，不产出内容。
    dry_run=False：先查 llm_cache，命中则复用（tokens_used=0，跟 Phase 4 批次分析
    对 EN->FR/IDcache 复用的语义一致）；未命中才真正调用，需要传入 credentials。
    provider="cursor_agent" 时 credentials 应为 CursorCredentials（走
    call_llm_cursor_agent，不是 OpenAI 兼容协议），跟 run.py/zmx_catalog.py 的
    provider 切换是同一个模式。
    """
    batches = load_locale_batches(conn, locale, batch_date)
    if not batches:
        return DailyDigestResult(generated=False, issues=["no_batches_for_locale_date"])

    prompt = build_daily_digest_prompt(locale, batch_date, batches)
    cache_key = compute_digest_cache_key(batches)

    if dry_run:
        return DailyDigestResult(generated=False, cache_key=cache_key, prompt=prompt)

    cached = get_cached_response(conn, cache_key)
    if cached is not None:
        summary, focus, issues = _validate_digest_response(cached)
        return DailyDigestResult(
            generated=summary is not None, from_cache=True, daily_summary=summary,
            priority_focus=focus, tokens_used=0, cache_key=cache_key, prompt=prompt, issues=issues,
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
    summary, focus, issues = _validate_digest_response(raw_text)
    return DailyDigestResult(
        generated=summary is not None, from_cache=False, daily_summary=summary,
        priority_focus=focus, tokens_used=tokens_used, cache_key=cache_key, prompt=prompt, issues=issues,
    )
