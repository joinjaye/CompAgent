from __future__ import annotations

from src.analysis.staged import (
    calculate_priority,
    comparison_cache_key,
    extraction_cache_key,
    preprocess_article,
    recall_candidates,
    render_action,
)
from src.analysis.zmx_baseline import ZmxBaselineEntry


def _entry(uid: str, mechanism: str, mechanics: str) -> ZmxBaselineEntry:
    return ZmxBaselineEntry(
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


def test_priority_is_programmatic_and_weight_changes_need_no_llm():
    score, priority = calculate_priority(
        event_type="reward_changed", gap_type="different_mechanism",
        business_impact="medium", confidence=0.95, novelty=2, urgency=2,
    )
    assert score == 82
    assert priority == "高"


def test_render_action_requires_real_action():
    assert render_action({"action_type": "no_action", "action": "关注"}) is None
    assert render_action({
        "action_type": "benchmark", "owner": "campaign_ops",
        "deadline": "within_2_business_days", "action": "对比奖池机制",
        "deliverable": "竞品机制表",
    }) == "campaign_ops｜within_2_business_days｜对比奖池机制，交付：竞品机制表"
