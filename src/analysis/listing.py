"""Listing/Delisting 的轻量 LLM 分类。

LLM 只判断币种赛道，不参与 Listing Type、Status、Token、交易对、上线时间、
ZMX 差异或业务优先级判断。其它字段均由公告事实和确定性规则派生。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from src.analysis.llm import strip_code_fences
from src.analysis.staged import namespaced_cache_key

LISTING_CLASSIFICATION_VERSION = "listing-category-v1"
LISTING_BATCH_SIZE = 50
TOKEN_CATEGORIES = (
    "AI", "Meme", "Layer2", "DeFi", "GameFi", "RWA", "DePIN", "Other",
)

_PAIR_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{2,15})\s*[/_\-]?\s*(USDT|USDC|USD|BTC|ETH)(?![A-Z0-9])",
    re.IGNORECASE,
)
_PERP_RE = re.compile(r"\b(perpetual|perp|futures?)\b|\bcontract\s+trading\b|合约|永续", re.I)
_SPOT_RE = re.compile(r"\bspot\b|现货", re.I)
_DATE_TIME_RE = re.compile(
    r"\b(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:UTC|GMT))?)?)\b",
    re.I,
)


@dataclass
class ListingClassification:
    valid: bool
    categories: dict[int, str] = field(default_factory=dict)
    confidences: dict[int, float] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def listing_cache_key(rows, *, model: str, provider: str, prompt_version: str) -> str:
    payload = [{"uid": r["uid"], "content_hash": r["content_hash"]} for r in rows]
    return namespaced_cache_key(
        "listing-category", payload, version=prompt_version, model=model, provider=provider,
    )


def build_listing_classification_prompt(rows) -> tuple[str, str]:
    labels = ", ".join(TOKEN_CATEGORIES)
    system = (
        "You classify crypto tokens mentioned in exchange listing or delisting announcements. "
        "Only classify the token/project sector. Do not assess Zoomex, priority, opportunity, "
        "listing type, status, or business impact. Use exactly one allowed category per item. "
        "Return JSON only."
    )
    articles = []
    for i, row in enumerate(rows, start=1):
        articles.append({
            "i": i,
            "title": row["title"] or "",
            "content": (row["content"] or "")[:1200],
        })
    user = (
        f"Allowed categories: {labels}.\n"
        "Definitions: AI=AI agents/models/compute/data; Meme=meme/community tokens; "
        "Layer2=blockchain scaling/L2; DeFi=DEX/lending/yield/derivatives protocols; "
        "GameFi=blockchain games/metaverse; RWA=tokenized real-world assets; "
        "DePIN=decentralized physical infrastructure; Other=other sectors or insufficient evidence.\n"
        "Return {\"items\":[{\"i\":1,\"category\":\"AI\",\"confidence\":0.9}]}.\n"
        f"Articles:\n{json.dumps(articles, ensure_ascii=False)}"
    )
    return system, user


def validate_listing_classification(raw_text: str, *, expected_indices: set[int]) -> ListingClassification:
    try:
        data = json.loads(strip_code_fences(raw_text))
    except (json.JSONDecodeError, TypeError) as exc:
        return ListingClassification(valid=False, issues=[f"json_parse_failed:{exc}"])
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return ListingClassification(valid=False, issues=["response_not_items_object"])

    categories: dict[int, str] = {}
    confidences: dict[int, float] = {}
    issues: list[str] = []
    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        idx = item.get("i")
        if not isinstance(idx, int) or idx not in expected_indices or idx in categories:
            issues.append(f"invalid_or_duplicate_index:{idx!r}")
            continue
        category = item.get("category")
        if category not in TOKEN_CATEGORIES:
            issues.append(f"item:{idx}:invalid_category:{category!r}")
            category = "Other"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        categories[idx] = category
        confidences[idx] = confidence

    missing = expected_indices - set(categories)
    for idx in missing:
        categories[idx] = "Other"
        confidences[idx] = 0.0
    if missing:
        issues.append("missing_indices:" + ",".join(map(str, sorted(missing))))
    return ListingClassification(valid=True, categories=categories, confidences=confidences, issues=issues)


_SPOT_SECTIONS = {
    ("Weex", "18540509241753"),      # New spot listings
    ("Bitunix", "13762037166105"),   # New Listings；合约标题会被显式 Perpetual 规则优先识别
    ("Lbank", "CO00000053"),         # New Listings 聚合；无合约证据时按现货处理
    ("Lbank", "CO00000057"),         # Delisting Information；无合约证据时按现货处理
}


def derive_listing_facts(
    title: Optional[str], content: Optional[str], category: str,
    *, source: Optional[str] = None, raw_category: Optional[str] = None,
) -> dict:
    text = f"{title or ''}\n{content or ''}"
    pair_match = _PAIR_RE.search(text)
    trading_pair = None
    token_symbol = None
    if pair_match:
        token_symbol = pair_match.group(1).upper()
        trading_pair = f"{token_symbol}/{pair_match.group(2).upper()}"

    has_perp = bool(_PERP_RE.search(text))
    has_spot = bool(_SPOT_RE.search(text))
    if has_perp and not has_spot:
        listing_type = "Perpetual"
    elif has_spot and not has_perp:
        listing_type = "Spot"
    elif has_perp and has_spot:
        listing_type = "Spot & Perpetual"
    elif not has_perp and not has_spot and (source, raw_category) in _SPOT_SECTIONS:
        listing_type = "Spot"
    else:
        listing_type = None
    launch_match = _DATE_TIME_RE.search(text)
    return {
        "token_symbol": token_symbol,
        "trading_pair": trading_pair,
        "listing_type": listing_type,
        "listing_status": "Delisted" if category == "delisting" else "New Listing",
        "launch_time": launch_match.group(1) if launch_match else None,
    }
