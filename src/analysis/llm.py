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
from typing import TYPE_CHECKING, Any, Optional

from src.analysis.config import LlmCredentials
from src.collectors.http import fetch as http_fetch
from src.db.operations import utcnow_iso

if TYPE_CHECKING:
    # zmx_baseline.py 反过来要 import 这个模块的 call_llm/compute_cache_key 等
    # （提取逻辑复用同一套 LLM 调用 + 缓存基础设施），运行时互相 import 会循环，
    # 这里只在类型检查时导入（配合文件顶部 `from __future__ import annotations`，
    # 注解本身在运行时是字符串，不需要真的把类拿到）。
    from src.analysis.zmx_baseline import ZmxBaselineEntry

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

# 逐条 articles[] 校验用（-v2 新增，见 prompts.py 顶部注释）。diff_type 复用同一张
# 表——每条自己的差异判断跟批次级 zmx_comparison.diff_type 遵守同一套枚举约束。
_VALID_ARTICLE_DIFF_TYPES = _VALID_DIFF_TYPES
_VALID_ARTICLE_PRIORITY: set[str] = {"高", "中", "低"}
_VALID_CHANGE_KIND: set[str] = {"reward", "rule", "other"}
_VALID_LISTING_KIND: set[str] = {"spot", "perp"}


def _normalize_article_fields(
    item: dict[str, Any], *, category: str, article_status: dict[str, str], issues: list[str]
) -> dict[str, Any]:
    """逐条 articles[] 的字段校验/强制，只修正/置空单个字段，绝不因为某个字段不合法
    就丢弃整条（丢弃整条的唯一触发条件是 uid 不在 related_uids 内，那个检查在调用方
    的循环里已经做过，走到这里的 item 已经确认 uid 合法）。

    - diff_type：跟批次级 zmx_comparison 完全相同的三分支逻辑（delisting 恒不适用 /
      不在该 category 合法枚举内则强制不适用 / evidence_indices 为空则强制不适用），
      只是判断依据是这一条自己的 evidence_indices，不是批次级的。
    - priority：不在 高/中/低 内则置 null（不像 diff_type 那样有"不适用"这个安全
      默认值可以强制填，无法判断时如实留空，不编造）。
    - follow_up：不是字符串则置 null。
    - change_kind：只有 category=campaign 且这一条自己的 status=changed 时才可能
      保留合法值，其余情况一律 null——即使 LLM 给了值也不采信。
    - listing_kind：只有 category=listing 且值在 spot/perp 内才保留，其余置 null。
    """
    uid = item.get("uid")

    evidence_raw = item.get("evidence_indices")
    evidence_list = [i for i in evidence_raw if isinstance(i, int)] if isinstance(evidence_raw, list) else []
    item["evidence_indices"] = evidence_list

    valid_diff = _VALID_ARTICLE_DIFF_TYPES.get(category, {"不适用"})
    diff_type = item.get("diff_type")
    if category == "delisting":
        diff_type = "不适用"
    elif diff_type not in valid_diff:
        issues.append(f"article:{uid}:invalid_diff_type:{diff_type}")
        diff_type = "不适用"
    elif diff_type != "不适用" and not evidence_list:
        issues.append(f"article:{uid}:empty_evidence_indices_forced_not_applicable")
        diff_type = "不适用"
    item["diff_type"] = diff_type

    priority = item.get("priority")
    if priority not in _VALID_ARTICLE_PRIORITY:
        if priority is not None:
            issues.append(f"article:{uid}:invalid_priority:{priority!r}")
        priority = None
    item["priority"] = priority

    follow_up = item.get("follow_up")
    item["follow_up"] = follow_up if isinstance(follow_up, str) else None

    change_kind = item.get("change_kind")
    if category == "campaign" and article_status.get(uid) == "changed" and change_kind in _VALID_CHANGE_KIND:
        item["change_kind"] = change_kind
    else:
        item["change_kind"] = None

    listing_kind = item.get("listing_kind")
    item["listing_kind"] = listing_kind if (category == "listing" and listing_kind in _VALID_LISTING_KIND) else None

    return item


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


def strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def validate_and_normalize(
    raw_text: str,
    *,
    category: str,
    related_uids: set[str],
    zmx_hits: Optional[list[ZmxBaselineEntry]] = None,
    article_status: Optional[dict[str, str]] = None,
) -> AnalysisResult:
    """入库前校验，规则见 CLAUDE.md Phase 4 / phasePrompts.md 第四步：

    - JSON 解析失败 -> 该批次 summary/articles_analysis/zmx_diff 全部 NULL，不重试
    - diff_type 不在枚举值内 -> 强制改「不适用」
    - diff_type != 不适用 但 evidence_indices 为空 -> 强制改「不适用」（防幻觉）
    - articles 里 uid 不在 related_uids 内 -> 丢弃该条目

    -v2（2026-07-20）新增：articles[] 逐条的 diff_type/priority/follow_up/
    change_kind/listing_kind 校验（见 _normalize_article_fields）。这些字段只做
    单字段级别的强制/置空，不会导致整条被丢弃——丢弃整条的唯一触发条件仍然只有
    上面第四条（uid 不在 related_uids 内）。

    article_status 是 {uid: status}（如 "new"/"changed"），用于程序性强制
    change_kind「只有该条自己 status=changed 时才可能有值」这条规则——不传时
    （默认 None，等价于空字典）等同于"每条状态都不是 changed"，change_kind 恒为
    null，不影响其余字段的校验，也不影响任何既有调用方/测试的行为。
    """
    zmx_hits = zmx_hits or []
    article_status = article_status or {}
    issues: list[str] = []

    try:
        data = json.loads(strip_code_fences(raw_text))
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
            item = _normalize_article_fields(
                item, category=category, article_status=article_status, issues=issues
            )
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
