"""Phase 4 staged-v1：确定性预处理、分阶段缓存键、候选召回与优先级计算。

本模块刻意不做网络调用。run.py 负责调用 provider；这里的函数都是纯函数，方便离线
测试，也让 priority 权重调整不需要重新调用 LLM。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from difflib import ndiff
from typing import Any, Iterable, Optional

from src.analysis.zmx_baseline import ZmxBaselineEntry

EXTRACTION_SCHEMA_VERSION = "article-facts-v1"
COMPARISON_SCHEMA_VERSION = "business-judgment-v1"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_MONEY_RE = re.compile(
    r"(?<!\w)(?:[$€¥]\s*)?\d[\d,]*(?:\.\d+)?\s*(?:USDT|USDC|USD|BTC|ETH|[A-Z]{2,10})?",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%")
_DATE_RE = re.compile(
    r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*20\d{2})?)\b",
    re.IGNORECASE,
)
_RULE_RE = re.compile(
    r"reward|prize|bonus|pool|eligible|eligibility|require|threshold|rule|"
    r"start|end|extend|fee|volume|deposit|trade|奖励|奖池|资格|门槛|规则|"
    r"开始|结束|延期|交易量|充值",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{2,}")


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def preprocess_article(
    *,
    title: str,
    content: str,
    old_content: Optional[str] = None,
    max_chars: int = 1500,
) -> dict[str, Any]:
    """把正文压成结构化证据；changed 只保留句子级 added/removed。"""
    sentences = _sentences(content)
    money = _dedupe(s for s in sentences if _MONEY_RE.search(s) or _PERCENT_RE.search(s))
    dates = _dedupe(s for s in sentences if _DATE_RE.search(s))
    rules = _dedupe(s for s in sentences if _RULE_RE.search(s))
    tables = _dedupe(s for s in sentences if "|" in s or "\t" in s)
    evidence = {
        "lead": sentences[0] if sentences else "",
        "money_sentences": money,
        "date_sentences": dates,
        "rule_sentences": rules,
        "table_rows": tables,
        "closing": "\n".join(sentences[-2:]) if sentences else "",
    }
    # 用序列化后的真实长度做硬上限，避免某一类句子特别多。
    while len(json.dumps(evidence, ensure_ascii=False)) > max_chars:
        longest = max(
            ("money_sentences", "date_sentences", "rule_sentences", "table_rows"),
            key=lambda key: len(evidence[key]),
        )
        if evidence[longest]:
            evidence[longest].pop()
        elif len(evidence["closing"]) > 120:
            evidence["closing"] = evidence["closing"][:120]
        else:
            break

    added: list[str] = []
    removed: list[str] = []
    if old_content is not None:
        for line in ndiff(_sentences(old_content), sentences):
            if line.startswith("+ "):
                added.append(line[2:])
            elif line.startswith("- "):
                removed.append(line[2:])

    candidates = {
        "dates": _dedupe(_DATE_RE.findall(content or "")),
        "amounts": _dedupe(match.group(0).strip() for match in _MONEY_RE.finditer(content or "") if match.group(0).strip()),
        "percentages": _dedupe(_PERCENT_RE.findall(content or "")),
        "market_type": (
            "perp" if re.search(r"\b(perpetual|perp|futures?|contract)\b", title, re.I)
            else "spot" if re.search(r"\bspot\b", title, re.I)
            else None
        ),
    }
    return {
        "content": evidence,
        "diff": {"added": added[:20], "removed": removed[:20]},
        "candidates": candidates,
    }


def namespaced_cache_key(
    namespace: str,
    payload: Any,
    *,
    version: str,
    model: str,
    provider: str,
) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{namespace}|{version}|{provider}|{model}|{canonical}".encode("utf-8")
    ).hexdigest()


def extraction_cache_key(content_hash: str, *, model: str, provider: str) -> str:
    return namespaced_cache_key(
        "extraction", {"content_hash": content_hash},
        version=EXTRACTION_SCHEMA_VERSION, model=model, provider=provider,
    )


def comparison_cache_key(
    facts: list[dict[str, Any]],
    candidates: dict[int, list[ZmxBaselineEntry]],
    *,
    prompt_version: str,
    model: str,
    provider: str,
) -> str:
    candidate_payload = {
        str(i): [
            {
                "uid": item.uid,
                "mechanism_type": item.mechanism_type,
                "key_mechanics": item.key_mechanics,
                "reward_range": item.reward_range,
                "target_users": item.target_users,
            }
            for item in entries
        ]
        for i, entries in candidates.items()
    }
    return namespaced_cache_key(
        "comparison", {"facts": facts, "candidates": candidate_payload},
        version=f"{COMPARISON_SCHEMA_VERSION}:{prompt_version}", model=model, provider=provider,
    )


def _terms(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_terms(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple, set)):
        return set().union(*(_terms(item) for item in value)) if value else set()
    if value is None:
        return set()
    return {token.lower() for token in _TOKEN_RE.findall(str(value))}


def recall_candidates(
    facts: dict[str, Any],
    entries: list[ZmxBaselineEntry],
    *,
    top_k: int = 4,
) -> list[ZmxBaselineEntry]:
    """结构化字段词项重叠召回。零重叠不返回候选，避免把无关基线硬塞给模型。"""
    query_terms = _terms({
        "mechanism": facts.get("mechanism"),
        "eligibility": facts.get("eligibility"),
        "reward": facts.get("reward"),
        "target_users": facts.get("target_users"),
        "feature": facts.get("feature"),
    })
    scored: list[tuple[int, int, ZmxBaselineEntry]] = []
    for pos, entry in enumerate(entries):
        entry_terms = _terms({
            "mechanism_type": entry.mechanism_type,
            "key_mechanics": entry.key_mechanics,
            "reward_range": entry.reward_range,
            "target_users": entry.target_users,
            "title": entry.title,
        })
        score = len(query_terms & entry_terms)
        if score:
            scored.append((score, -pos, entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in scored[:top_k]]


_EVENT_SCORE = {
    "reward_changed": 30, "rule_changed": 28, "extended": 20, "ended": 18,
    "created": 15, "cancelled": 15, "other_updated": 10, "unknown": 0,
}
_GAP_SCORE = {
    "confirmed_gap": 30, "different_mechanism": 22, "baseline_not_found": 12,
    "covered": 3, "not_applicable": 0,
}
_IMPACT_SCORE = {"high": 20, "medium": 12, "low": 4}


def calculate_priority(
    *,
    event_type: str,
    gap_type: str,
    business_impact: str,
    confidence: float,
    novelty: int = 0,
    urgency: int = 0,
) -> tuple[int, str]:
    """LLM 输出窄维度，程序计算稳定、可解释的分数和档位。"""
    confidence_score = 10 if confidence >= 0.9 else 5 if confidence >= 0.7 else -10
    score = (
        _EVENT_SCORE.get(event_type, 0)
        + _GAP_SCORE.get(gap_type, 0)
        + _IMPACT_SCORE.get(business_impact, 0)
        + confidence_score
        + max(0, min(3, int(novelty))) * 2
        + max(0, min(3, int(urgency))) * 2
    )
    return score, "高" if score >= 70 else "中" if score >= 40 else "低"


def render_action(item: dict[str, Any]) -> Optional[str]:
    action = item.get("action")
    if not isinstance(action, str) or not action.strip() or item.get("action_type") == "no_action":
        return None
    owner = item.get("owner") or "unassigned"
    deadline = item.get("deadline") or "unscheduled"
    deliverable = item.get("deliverable")
    suffix = f"，交付：{deliverable}" if deliverable else ""
    return f"{owner}｜{deadline}｜{action.strip()}{suffix}"
