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

from src.analysis.zmx_catalog import ZmxCatalogEntry

EXTRACTION_SCHEMA_VERSION = "article-facts-v1"
COMPARISON_SCHEMA_VERSION = "business-judgment-v2"  # v2：移除 action_type/owner/action/
                                                     # deliverable/deadline/needs_human_review
                                                     # ——这些不再由 LLM 产出，Follow-up
                                                     # 改为 Phase⑤ 的确定性规则派生

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
# recall_candidates() \u9760\u8bcd\u9879\u91cd\u53e0\u5224\u65ad\u5019\u9009\u662f\u5426\u76f8\u5173\uff0c\u53ea\u8981\u6c42 >=1 \u4e2a\u5171\u540c\u8bcd\u2014\u20142026-07-22
# \u771f\u5b9e\u6570\u636e\u53d1\u73b0\u8fd9\u4e2a\u9608\u503c\u5728\u82f1\u6587\u8bed\u6599\u4e0b\u5f62\u540c\u865a\u8bbe\uff1a\u4e2d\u6587\u56e0\u4e3a _TOKEN_RE \u8981\u6c42\u81f3\u5c11 2 \u4e2a\u5b57\u7b26\uff0c
# \u5929\u7136\u8fc7\u6ee4\u6389\u4e86"\u7684"/"\u4e86"\u8fd9\u7c7b\u5355\u5b57\u865a\u8bcd\uff0c\u4f46\u82f1\u6587\u7684"is"/"to"/"users"/"trading"/
# "account"\u8fd9\u7c7b\u9ad8\u9891\u865a\u8bcd\u6216\u884c\u4e1a\u901a\u7528\u8bcd\u957f\u5ea6\u90fd >=2\uff0c\u4f1a\u88ab\u5f53\u6210\u6709\u6548\u8bcd\u9879\uff0c\u5bfc\u81f4\u51e0\u4e4e\u4efb\u4f55\u4e24\u6bb5
# \u82f1\u6587\u4e1a\u52a1\u6587\u672c\u90fd\u80fd\u78b0\u51fa\u81f3\u5c11\u4e00\u4e2a\u91cd\u53e0\u8bcd\u2014\u2014\u5b9e\u6d4b Bitunix product/EN \u4e00\u6574\u6279 12 \u7bc7\u51e0\u4e4e\u5b8c\u5168
# \u4e0d\u540c\u4e3b\u9898\u7684\u6587\u7ae0\uff08tick size \u8c03\u6574\u3001AUSTRAC \u6ce8\u518c\u3001SEPA \u4e3b\u4f53\u53d8\u66f4\u3001CRWD \u62c6\u80a1\u5408\u7ea6\u8c03\u6574\u7b49\uff09
# \u53ec\u56de\u7ed3\u679c\u5168\u90e8\u8d8b\u540c\u6307\u5411\u540c\u4e00\u4e24\u4e2a Zoomex \u76ee\u5f55\u6761\u76ee\uff08wallet/card_payment\uff09\uff0c\u4e0d\u662f\u56e0\u4e3a
# \u771f\u7684\u76f8\u5173\uff0c\u53ea\u662f\u56e0\u4e3a\u90fd\u542b"users""trading"\u8fd9\u7c7b\u8bcd\u3002\u505c\u7528\u8bcd\u8868\u53ea\u505a\u4fdd\u5b88\u7684\u8bed\u6cd5\u865a\u8bcd +
# \u9ad8\u9891\u65e0\u533a\u5206\u5ea6\u884c\u4e1a\u60ef\u7528\u8bcd\uff0c\u4e0d\u52a8\u4efb\u4f55\u53ef\u80fd\u627f\u8f7d\u5b9e\u9645\u8bed\u4e49\u7684\u8bcd\uff08\u5982 fee/deposit/tick/
# wallet/copy/risk \u7b49\u5177\u4f53\u673a\u5236\u8bcd\u90fd\u4fdd\u7559\uff09\u3002
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
    candidates: dict[int, list[ZmxCatalogEntry]],
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
    return {
        token.lower() for token in _TOKEN_RE.findall(str(value))
        if token.lower() not in _STOPWORDS
    }


def recall_candidates(
    facts: dict[str, Any],
    entries: list[ZmxCatalogEntry],
    *,
    top_k: int = 4,
) -> list[ZmxCatalogEntry]:
    """结构化字段词项重叠召回。零重叠不返回候选，避免把无关基线硬塞给模型。"""
    query_terms = _terms({
        "mechanism": facts.get("mechanism"),
        "eligibility": facts.get("eligibility"),
        "reward": facts.get("reward"),
        "target_users": facts.get("target_users"),
        "feature": facts.get("feature"),
    })
    scored: list[tuple[int, int, ZmxCatalogEntry]] = []
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
