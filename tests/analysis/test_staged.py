from __future__ import annotations

from src.analysis.staged import (
    calculate_priority,
    comparison_cache_key,
    extraction_cache_key,
    preprocess_article,
    recall_candidates,
)
from src.analysis.zmx_catalog import ZmxCatalogEntry


def _entry(uid: str, mechanism: str, mechanics: str) -> ZmxCatalogEntry:
    return ZmxCatalogEntry(
        uid=uid, title=mechanism, mechanism_type=mechanism, key_mechanics=mechanics,
        reward_range=None, target_users=None, start_date=None, end_date=None, post_time=None,
    )


def test_preprocess_keeps_high_value_sentences_and_structured_diff():
    result = preprocess_article(
        title="Trading competition",
        content="Welcome. Prize pool increased to 50,000 USDT. Campaign ends July 31.",
        old_content="Welcome. Prize pool is 20,000 USDT. Campaign ends July 24.",
    )
    assert "50,000 USDT" in " ".join(result["content"]["money_sentences"])
    assert any("50,000" in line for line in result["diff"]["added"])
    assert any("20,000" in line for line in result["diff"]["removed"])
    assert "50,000 USDT" in result["candidates"]["amounts"]


def test_extraction_cache_is_independent_from_baseline_but_model_and_provider_sensitive():
    first = extraction_cache_key("hash", model="small", provider="openai_http")
    assert first == extraction_cache_key("hash", model="small", provider="openai_http")
    assert first != extraction_cache_key("hash", model="large", provider="openai_http")
    assert first != extraction_cache_key("hash", model="small", provider="cursor_agent")


def test_comparison_cache_changes_when_candidate_baseline_changes():
    facts = [{"i": 1, "mechanism": "合约交易量排名"}]
    a = comparison_cache_key(
        facts, {1: [_entry("z1", "合约交易赛", "按交易量排名")]},
        prompt_version="v1", model="m", provider="p",
    )
    b = comparison_cache_key(
        facts, {1: [_entry("z2", "合约交易赛", "按交易量排名")]},
        prompt_version="v1", model="m", provider="p",
    )
    assert a != b


def test_recall_returns_only_relevant_top_four_and_no_forced_fallback():
    entries = [
        _entry("z1", "合约交易赛", "合约交易量排名"),
        _entry("z2", "入金活动", "充值送体验金"),
    ]
    assert [e.uid for e in recall_candidates({"mechanism": "合约交易量排名"}, entries)] == ["z1"]
    assert recall_candidates({"mechanism": "NFT mint"}, entries) == []


def test_recall_ignores_generic_english_stopwords_and_industry_filler_words():
    """2026-07-22 真实数据发现：英文语料下 recall_candidates 几乎对任何两篇文章都
    能碰出重叠词（"users"/"trading"/"account"这类高频虚词或行业通用词长度都够
    _TOKEN_RE 的门槛），导致 Bitunix product/EN 一整批毫不相关的文章（tick size
    调整、AUSTRAC 注册等）全部被强行召回同一两个 Zoomex 目录条目。停用词过滤后，
    只共享通用虚词/行业套话的两段文本不应再被判定为"相关"。"""
    entries = [_entry("z1", "wallet", "Users can view their account balance and use the platform.")]
    facts = {"mechanism": "Regulatory registration for a new remittance entity", "feature": "AUSTRAC registration"}
    assert recall_candidates(facts, entries) == []


def test_recall_still_matches_on_genuine_shared_domain_terms():
    """停用词过滤不能矫枉过正——真正共享的机制词（如 tick/deposit/wallet）必须
    继续被识别为相关，不能被停用词表误杀。"""
    entries = [_entry("z1", "wallet", "Supports instant deposit and withdrawal via OnlinePay.")]
    facts = {"mechanism": "Instant deposit and withdrawal for local currency"}
    assert [e.uid for e in recall_candidates(facts, entries)] == ["z1"]


def test_priority_is_programmatic_and_weight_changes_need_no_llm():
    score, priority = calculate_priority(
        event_type="reward_changed", gap_type="different_mechanism",
        business_impact="medium", confidence=0.95, novelty=2, urgency=2,
    )
    assert score == 82
    assert priority == "高"
