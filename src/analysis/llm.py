"""LLM 调用（OpenAI 兼容 /chat/completions）+ 入库前校验 + 批次级响应缓存。

网络请求复用 src/collectors/http.py 的 fetch()（同一套指数退避重试 + certifi CA
方案，不再重新实现一遍）。LLM_API_BASE 约定是一个 OpenAI 兼容 base url
（形如 https://api.example.com/v1），本模块固定拼接 /chat/completions——这是
"任何 OpenAI 兼容接口"这一约束下最大公约数的调用形态，Anthropic 需经由官方的
OpenAI-compatible endpoint 接入，不在这里做按厂商分支的双协议实现。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from src.analysis.config import LlmCredentials
from src.analysis.zmx_index import ZmxArticle
from src.collectors.http import fetch as http_fetch
from src.db.operations import utcnow_iso

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# listing 的 diff_type 不含"ZMX玩法不同"（见 prompts.py listing-v1 强制规则 4）；
# delisting 恒为"不适用"，不在这里走通用枚举校验（validate_and_normalize 里单独强制）。
_VALID_DIFF_TYPES: dict[str, set[str]] = {
    "campaign": {"ZMX已有", "ZMX缺失", "ZMX玩法不同", "混合", "不适用"},
    "product": {"ZMX已有", "ZMX缺失", "ZMX玩法不同", "混合", "不适用"},
    "listing": {"ZMX已有", "ZMX缺失", "混合", "不适用"},
    "delisting": {"不适用"},
}


def compute_cache_key(content_hashes: list[str], prompt_version: str) -> str:
    """key = SHA256(SHA256(排序后的 content_hash 拼接) || prompt_version)。

    content_hashes 排序后再拼接，保证同一批次不管 SQL 返回顺序如何都能算出同一个
    key；任何一条内容变化（changed）或批次新增文章都会改变这个 hash，天然满足
    "同批次内容没变、prompt 版本没变时才复用缓存"的要求。
    """
    joined = "".join(sorted(content_hashes))
    inner = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{inner}|{prompt_version}".encode("utf-8")).hexdigest()


def get_cached_response(conn: sqlite3.Connection, cache_key: str) -> Optional[str]:
    row = conn.execute("SELECT response FROM llm_cache WHERE cache_key = ?", (cache_key,)).fetchone()
    return row["response"] if row else None


def set_cached_response(conn: sqlite3.Connection, cache_key: str, response: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (cache_key, response, created_at) VALUES (?, ?, ?)",
        (cache_key, response, utcnow_iso()),
    )


def call_llm(
    system: str,
    user: str,
    *,
    credentials: LlmCredentials,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    max_retries: int,
) -> tuple[str, Optional[int]]:
    """返回 (原始响应文本, 本次调用消耗的 token 数或 None)。"""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {credentials.api_key}",
    }
    url = f"{credentials.api_base.rstrip('/')}/chat/completions"
    raw = http_fetch(url, method="POST", headers=headers, body=body, timeout=timeout_s, max_retries=max_retries)
    data = json.loads(raw)
    content = data["choices"][0]["message"]["content"]
    tokens_used = (data.get("usage") or {}).get("total_tokens")
    return content, tokens_used


@dataclass
class AnalysisResult:
    valid: bool
    summary: Optional[str] = None
    articles_analysis: list[dict[str, Any]] = field(default_factory=list)
    zmx_diff: Optional[str] = None
    diff_type: Optional[str] = None
    priority: Optional[str] = None
    evidence_indices: list[int] = field(default_factory=list)
    zmx_evidence_uids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def validate_and_normalize(
    raw_text: str,
    *,
    category: str,
    related_uids: set[str],
    zmx_hits: Optional[list[ZmxArticle]] = None,
) -> AnalysisResult:
    """入库前校验，规则见 CLAUDE.md Phase 4 / phasePrompts.md 第四步：

    - JSON 解析失败 -> 该批次 summary/articles_analysis/zmx_diff 全部 NULL，不重试
    - diff_type 不在枚举值内 -> 强制改「不适用」
    - diff_type != 不适用 但 evidence_indices 为空 -> 强制改「不适用」（防幻觉）
    - articles 里 uid 不在 related_uids 内 -> 丢弃该条目
    """
    zmx_hits = zmx_hits or []
    issues: list[str] = []

    try:
        data = json.loads(_strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("LLM 响应 JSON 解析失败，本批次分析字段全部置 NULL：%s", e)
        return AnalysisResult(valid=False, issues=[f"json_parse_failed: {e}"])

    if not isinstance(data, dict):
        logger.error("LLM 响应不是 JSON object，本批次分析字段全部置 NULL")
        return AnalysisResult(valid=False, issues=["response_not_object"])

    summary = data.get("batch_summary")

    articles_raw = data.get("articles")
    filtered_articles: list[dict[str, Any]] = []
    if isinstance(articles_raw, list):
        for item in articles_raw:
            if not isinstance(item, dict):
                continue
            uid = item.get("uid")
            if uid not in related_uids:
                logger.warning("丢弃 articles 条目：uid=%r 不在本批次 related_uids 内", uid)
                issues.append(f"dropped_article_uid_not_in_batch:{uid}")
                continue
            filtered_articles.append(item)

    zmx = data.get("zmx_comparison") or {}
    if not isinstance(zmx, dict):
        zmx = {}

    valid_types = _VALID_DIFF_TYPES.get(category, {"不适用"})
    diff_type = zmx.get("diff_type")

    if category == "delisting":
        diff_type = "不适用"
    elif diff_type not in valid_types:
        logger.warning("diff_type=%r 不在 %s 的合法枚举内，强制改为「不适用」", diff_type, category)
        issues.append(f"invalid_diff_type:{diff_type}")
        diff_type = "不适用"

    evidence_indices_raw = zmx.get("evidence_indices")
    evidence_indices = [i for i in evidence_indices_raw if isinstance(i, int)] if isinstance(evidence_indices_raw, list) else []

    if diff_type != "不适用" and not evidence_indices:
        logger.warning("diff_type=%r 但 evidence_indices 为空，强制改为「不适用」（防幻觉校验）", diff_type)
        issues.append("empty_evidence_indices_forced_not_applicable")
        diff_type = "不适用"

    zmx_evidence_uids = [
        zmx_hits[i - 1].uid for i in evidence_indices if 1 <= i <= len(zmx_hits)
    ]

    analysis_text = zmx.get("analysis")
    priority = zmx.get("priority")
    priority_reason = zmx.get("priority_reason")
    zmx_diff = None
    if isinstance(analysis_text, str) and analysis_text:
        zmx_diff = analysis_text
        if isinstance(priority_reason, str) and priority_reason:
            zmx_diff = f"{zmx_diff}\n优先级依据：{priority_reason}"

    return AnalysisResult(
        valid=True,
        summary=summary if isinstance(summary, str) else None,
        articles_analysis=filtered_articles,
        zmx_diff=zmx_diff,
        diff_type=diff_type,
        priority=priority if isinstance(priority, str) else None,
        evidence_indices=evidence_indices,
        zmx_evidence_uids=zmx_evidence_uids,
        issues=issues,
    )
