"""LLM 调用（OpenAI 兼容 /chat/completions）+ staged-v1 两段响应的入库前校验 +
响应缓存。

网络请求复用 src/collectors/http.py 的 fetch()（同一套指数退避重试 + certifi CA
方案，不再重新实现一遍）。LLM_API_BASE 约定是一个 OpenAI 兼容 base url
（形如 https://api.example.com/v1），本模块固定拼接 /chat/completions——这是
"任何 OpenAI 兼容接口"这一约束下最大公约数的调用形态，Anthropic 需经由官方的
OpenAI-compatible endpoint 接入，不在这里做按厂商分支的双协议实现。

Phase②（staged.py 接入）替换了原来的单体 validate_and_normalize()：现在分两段
校验——validate_fact_extraction()（Stage1，逐篇事实抽取）和
validate_business_judgment()（Stage3，批量业务判断）。旧的批次级
compute_cache_key() 也一并下线，缓存 key 改用 staged.py 的
extraction_cache_key()/comparison_cache_key()。
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
    # zmx_catalog.py 反过来要 import 这个模块的 call_llm/get_cached_response 等
    # （提取逻辑复用同一套 LLM 调用 + 缓存基础设施），运行时互相 import 会循环，
    # 这里只在类型检查时导入（配合文件顶部 `from __future__ import annotations`，
    # 注解本身在运行时是字符串，不需要真的把类拿到）。
    from src.analysis.zmx_catalog import ZmxCatalogEntry

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_VALID_EVENT_TYPES: set[str] = {
    "created", "reward_changed", "rule_changed", "extended", "ended",
    "cancelled", "other_updated", "unknown",
}
_VALID_GAP_TYPES: set[str] = {
    "confirmed_gap", "baseline_not_found", "different_mechanism", "covered", "not_applicable",
}
_VALID_BUSINESS_IMPACT: set[str] = {"high", "medium", "low"}

# gap_type（英文，staged.py/prompts.py 的内部wire格式）→ 项目历史上一直使用的中文
# diff_type 枚举，只在这里、写库前这一个边界做一次翻译（见 CLAUDE.md「Phase②」的
# 决定：gap_type 的值不是 diff_type 的简单 1:1 改名——baseline_not_found 特指"没
# 召回到候选，不能断言缺失"，必须映射到「不适用」而不是「ZMX缺失」，这是防止误报
# 缺失的关键，不是随意的措辞选择）。
GAP_TYPE_TO_DIFF_TYPE: dict[str, str] = {
    "confirmed_gap": "ZMX缺失",
    "different_mechanism": "ZMX玩法不同",
    "covered": "ZMX已有",
    "baseline_not_found": "不适用",
    "not_applicable": "不适用",
}


def strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def compute_cache_key(
    content_hashes: list[str],
    prompt_version: str,
    *,
    model: str = "",
    context_hash: str = "",
) -> str:
    """批次级缓存 key，目前只被 zmx_catalog.py 的 Zoomex 提取用（Stage1/Stage3 的
    per-article/per-batch 竞品分析缓存已改用 staged.py 的
    extraction_cache_key()/comparison_cache_key()，语义更贴合"只按单篇/单次比较
    调用生成 key"，不需要这个更通用的批次级实现——但 Zoomex 提取仍然是"一批公告一次
    调用"的老模型，继续复用这个函数，不必为了同一件事重写第二遍）。

    content_hashes 排序后再拼接，保证同一批次不管 SQL 返回顺序如何都能算出同一个
    key。model 防止切换模型后复用旧结果；context_hash 用于把跟批次内容无关但影响
    输出的上下文（如 prompt 的 system/user 全文）一并纳入 key。
    """
    joined = "".join(sorted(content_hashes))
    inner = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return hashlib.sha256(
        f"{inner}|{prompt_version}|{model}|{context_hash}".encode("utf-8")
    ).hexdigest()


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


# ============================================================
# Stage1：事实抽取校验
# ============================================================


@dataclass
class FactExtractionResult:
    valid: bool
    index: Optional[int] = None
    event_type: str = "unknown"
    mechanism: Optional[str] = None
    feature: Optional[str] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    reward: dict[str, Any] = field(default_factory=dict)
    eligibility: Optional[str] = None
    target_users: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def validate_fact_extraction(raw_text: str, *, expected_index: int) -> FactExtractionResult:
    """Stage1 单篇响应校验。JSON 解析失败或不是 object → invalid（该篇事实全部
    留空，不重试，跟旧版"批次解析失败则整批 NULL"是同一个哲学，只是单位从批次
    缩小到单篇——提取失败不该让同批次其它篇也白费）。i 跟期望值不符只记 issue、
    强制纠正，不整篇作废（LLM 偶尔照抄错位不该让这一篇的抽取结果全部丢弃）。
    """
    try:
        data = json.loads(strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Stage1 事实抽取响应 JSON 解析失败：%s", e)
        return FactExtractionResult(valid=False, index=expected_index, issues=[f"json_parse_failed: {e}"])
    if not isinstance(data, dict):
        logger.error("Stage1 事实抽取响应不是 JSON object")
        return FactExtractionResult(valid=False, index=expected_index, issues=["response_not_object"])

    issues: list[str] = []

    idx = data.get("i")
    if idx != expected_index:
        issues.append(f"index_mismatch:expected={expected_index}:got={idx!r}")
        idx = expected_index

    event_type = data.get("event_type")
    if event_type not in _VALID_EVENT_TYPES:
        if event_type is not None:
            issues.append(f"invalid_event_type:{event_type!r}")
        event_type = "unknown"

    reward_raw = data.get("reward")
    reward = reward_raw if isinstance(reward_raw, dict) else {}

    target_users_raw = data.get("target_users")
    target_users = [u for u in target_users_raw if isinstance(u, str)] if isinstance(target_users_raw, list) else []

    changes_raw = data.get("changes")
    changes = [c for c in changes_raw if isinstance(c, dict)] if isinstance(changes_raw, list) else []

    evidence_raw = data.get("evidence")
    evidence = [e for e in evidence_raw if isinstance(e, str)][:5] if isinstance(evidence_raw, list) else []

    confidence_raw = data.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        if confidence_raw is not None:
            issues.append(f"invalid_confidence:{confidence_raw!r}")
        confidence = 0.0

    return FactExtractionResult(
        valid=True,
        index=idx,
        event_type=event_type,
        mechanism=_str_or_none(data.get("mechanism")),
        feature=_str_or_none(data.get("feature")),
        start_at=_str_or_none(data.get("start_at")),
        end_at=_str_or_none(data.get("end_at")),
        reward=reward,
        eligibility=_str_or_none(data.get("eligibility")),
        target_users=target_users,
        changes=changes,
        evidence=evidence,
        confidence=confidence,
        issues=issues,
    )


# ============================================================
# Stage3：业务判断校验
# ============================================================


@dataclass
class BusinessJudgmentItem:
    index: int
    gap_type: str
    diff_type: str
    business_impact: str
    novelty: int
    urgency: int
    zmx_evidence_uids: list[str]
    reason: Optional[str]


@dataclass
class BusinessJudgmentResult:
    valid: bool
    items: list[BusinessJudgmentItem] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _clamp_0_3(value: Any) -> int:
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return 0


def validate_business_judgment(
    raw_text: str,
    *,
    expected_indices: set[int],
    candidates_by_index: dict[int, list["ZmxCatalogEntry"]],
) -> BusinessJudgmentResult:
    """Stage3 批量响应校验，逐条独立处理（不因为某一条不合法就整批作废）：

    - i 不在 expected_indices 内 / 重复 i → 丢弃该条（唯一会丢整条的规则，跟旧版
      "uid 不在 related_uids 内丢弃"是同一个防幻觉精神）。
    - gap_type 不在合法枚举内 → 强制「not_applicable」。
    - 该条对应的候选列表为空，但 gap_type 断言了 confirmed_gap/different_mechanism/
      covered → 强制降级为「baseline_not_found」（程序性兜底，不能仅凭 prompt 里
      写的规则就信任 LLM 一定遵守）。
    - zmx_evidence 引用越界的候选序号 → 丢弃该条引用，不映射越界 uid。
    - gap_type 断言了 different_mechanism/covered 但引用不到任何真实候选证据 →
      同样降级为「baseline_not_found」（防幻觉，跟旧版 evidence_indices 为空强制
      「不适用」是同一条规则在新 schema 下的等价物）。
    - business_impact 不合法 → 置「low」（安全默认，不是「不产出」，因为
      calculate_priority() 需要这个字段才能算分，留空会破坏下游确定性算分）。
    """
    try:
        data = json.loads(strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Stage3 业务判断响应 JSON 解析失败：%s", e)
        return BusinessJudgmentResult(valid=False, issues=[f"json_parse_failed: {e}"])
    if not isinstance(data, dict):
        logger.error("Stage3 业务判断响应不是 JSON object")
        return BusinessJudgmentResult(valid=False, issues=["response_not_object"])

    items_raw = data.get("items")
    issues: list[str] = []
    items: list[BusinessJudgmentItem] = []
    seen: set[int] = set()

    if isinstance(items_raw, list):
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            idx = item.get("i")
            if not isinstance(idx, int) or idx not in expected_indices:
                issues.append(f"dropped_item_index_not_in_batch:{idx!r}")
                continue
            if idx in seen:
                issues.append(f"dropped_duplicate_index:{idx}")
                continue
            seen.add(idx)

            candidates = candidates_by_index.get(idx, [])

            gap_type = item.get("gap_type")
            if gap_type not in _VALID_GAP_TYPES:
                issues.append(f"item:{idx}:invalid_gap_type:{gap_type!r}")
                gap_type = "not_applicable"

            if not candidates and gap_type not in ("baseline_not_found", "not_applicable"):
                issues.append(f"item:{idx}:no_candidates_forced_baseline_not_found")
                gap_type = "baseline_not_found"

            evidence_raw = item.get("zmx_evidence")
            evidence_positions = [e for e in evidence_raw if isinstance(e, int)] if isinstance(evidence_raw, list) else []
            zmx_evidence_uids = [candidates[p - 1].uid for p in evidence_positions if 1 <= p <= len(candidates)]

            if gap_type in ("different_mechanism", "covered") and not zmx_evidence_uids:
                issues.append(f"item:{idx}:empty_evidence_forced_baseline_not_found")
                gap_type = "baseline_not_found"

            business_impact = item.get("business_impact")
            if business_impact not in _VALID_BUSINESS_IMPACT:
                if business_impact is not None:
                    issues.append(f"item:{idx}:invalid_business_impact:{business_impact!r}")
                business_impact = "low"

            reason = item.get("reason")
            reason = reason.strip() if isinstance(reason, str) and reason.strip() else None

            items.append(BusinessJudgmentItem(
                index=idx,
                gap_type=gap_type,
                diff_type=GAP_TYPE_TO_DIFF_TYPE[gap_type],
                business_impact=business_impact,
                novelty=_clamp_0_3(item.get("novelty")),
                urgency=_clamp_0_3(item.get("urgency")),
                zmx_evidence_uids=zmx_evidence_uids,
                reason=reason,
            ))

    missing = expected_indices - seen
    if missing:
        issues.append(f"missing_item_indices:{','.join(str(m) for m in sorted(missing))}")

    return BusinessJudgmentResult(valid=True, items=items, issues=issues)


def aggregate_batch_diff_type(article_diff_types: list[str]) -> str:
    """批次级 diff_type（insights.diff_type 列）完全程序化推导，不再向 LLM 询问

    ——「混合」不再是 AI 需要理解和判断的概念，纯粹是"这批文章的 diff_type 集合
    是否包含一种以上非「不适用」的取值"这个可枚举事实。全部「不适用」（含空批次）
    → 「不适用」；只有一种非「不适用」取值 → 直接用该值；出现两种及以上 →「混合」。
    """
    non_na = {d for d in article_diff_types if d and d != "不适用"}
    if not non_na:
        return "不适用"
    if len(non_na) > 1:
        return "混合"
    return next(iter(non_na))
