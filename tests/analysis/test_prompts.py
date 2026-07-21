"""src/analysis/prompts.py 单测：占位符替换只认 ALL_CAPS 变量、不受信任的正文内容
不会破坏模板、staged-v1 的两段 prompt（build_fact_extraction_prompt/
build_business_judgment_prompt，Phase②新增，此前零测试覆盖）、Zoomex 能力目录提取
prompt（build_catalog_extraction_prompt）。旧的 build_prompt/build_zmx_note/
build_zmx_block（v1/v2/v3 单体 prompt）已随 Phase② 的 staged.py 改造整体删除，不
再测试。
"""

from __future__ import annotations

import json

from src.analysis.prompts import (
    TaxonomyCategory,
    TaxonomySpec,
    build_business_judgment_prompt,
    build_catalog_extraction_prompt,
    build_fact_extraction_prompt,
    render,
)
from src.analysis.zmx_catalog import ZmxCatalogEntry


def test_render_only_replaces_all_caps_placeholders():
    template = "Hello {NAME}, price is {100} and {lowercase} stays, json {\"key\": 1}"
    out = render(template, {"NAME": "World"})
    assert out == 'Hello World, price is {100} and {lowercase} stays, json {"key": 1}'


def test_render_does_not_reprocess_substituted_content():
    template = "{ARTICLES_BLOCK}"
    out = render(template, {"ARTICLES_BLOCK": "contains literal {SOURCE} text"})
    assert out == "contains literal {SOURCE} text"


def test_render_leaves_unknown_placeholder_untouched():
    out = render("{UNKNOWN_VAR}", {})
    assert out == "{UNKNOWN_VAR}"


# ---------------------------------------------------------------- staged-v1 ----


def test_build_fact_extraction_prompt_returns_index_not_uid_or_title():
    """反回声设计：payload 里传了 title 供 LLM 参考，但 required_schema 只要求
    返回批次内整数 i，不要求（也不应该）回传 uid/title。"""
    prompt = build_fact_extraction_prompt(
        index=3, category="campaign", status="new", title="Some Title",
        preprocessed={"content": {"lead": "L"}, "diff": {"added": [], "removed": []}, "candidates": {}},
    )
    assert prompt.system
    assert '"i":3' in prompt.user
    schema_block = prompt.user.split("required_schema:\n", 1)[1]
    assert '"uid"' not in schema_block
    assert '"title"' not in schema_block
    assert "event_type" in schema_block


def test_build_fact_extraction_prompt_includes_preprocessed_evidence():
    prompt = build_fact_extraction_prompt(
        index=1, category="product", status="changed", title="T",
        preprocessed={
            "content": {"lead": "Prize pool increased to 50,000 USDT."},
            "diff": {"added": ["+ 50,000 USDT"], "removed": ["- 20,000 USDT"]},
            "candidates": {"amounts": ["50,000 USDT"], "market_type": "perp"},
        },
    )
    assert "50,000 USDT" in prompt.user
    assert "perp" in prompt.user


def _catalog_entry(uid, mechanism_type, mechanics="m", reward="r", users="u", title="t"):
    return ZmxCatalogEntry(
        uid=uid, title=title, mechanism_type=mechanism_type, key_mechanics=mechanics,
        reward_range=reward, target_users=users, start_date=None, end_date=None, post_time=None,
    )


def test_build_business_judgment_prompt_only_reads_facts_and_candidates_not_original_content():
    """比较阶段不应该重新读取公告原文——只有 facts（Stage1 已抽取的结构化字段）和
    candidates_by_index（Zoomex 目录候选）进入 payload。"""
    facts = [{"i": 1, "mechanism": "充值送奖励", "feature": None}]
    candidates = {1: [_catalog_entry("z1", "deposit_reward")]}
    prompt = build_business_judgment_prompt(
        batch_date="2026-07-21", locale="EN", source="Bitunix", category="campaign",
        facts=facts, candidates_by_index=candidates,
    )
    payload = json.loads(prompt.user.split("input:\n", 1)[1].split("\noutput_schema:", 1)[0])
    assert payload["items"][0]["facts"] == facts[0]
    assert payload["items"][0]["zmx_candidates"][0]["mechanism_type"] == "deposit_reward"
    # 原始公告字段（uid/title/content）不应该出现在这一阶段的 payload 里
    assert "content" not in json.dumps(payload)


def test_build_business_judgment_prompt_schema_excludes_ai_produced_action_fields():
    """Phase②明确要求：AI 不产出 priority/action_type/owner/follow_up，这些改为
    Phase⑤ 的确定性规则派生。output_schema 里不应该再要求这些字段。
    """
    prompt = build_business_judgment_prompt(
        batch_date="2026-07-21", locale="EN", source="Bitunix", category="campaign",
        facts=[{"i": 1}], candidates_by_index={},
    )
    schema_block = prompt.user.split("output_schema:\n", 1)[1]
    for forbidden in ("action_type", "owner", "\"action\"", "deliverable", "deadline", "needs_human_review", "priority"):
        assert forbidden not in schema_block, f"{forbidden} 不应出现在 business judgment 的 output_schema 里"
    assert "gap_type" in schema_block
    assert "reason" in schema_block


def test_build_business_judgment_prompt_empty_candidates_produce_empty_list():
    prompt = build_business_judgment_prompt(
        batch_date="2026-07-21", locale="EN", source="Bitunix", category="product",
        facts=[{"i": 5}], candidates_by_index={},
    )
    payload = json.loads(prompt.user.split("input:\n", 1)[1].split("\noutput_schema:", 1)[0])
    assert payload["items"][0]["zmx_candidates"] == []


# ---------------------------------------------------------------- zmx-catalog-extract-v1 ----


def _taxonomy(category: str) -> TaxonomySpec:
    return TaxonomySpec(
        category=category,
        method="semi_closed",
        entries=[
            TaxonomyCategory(key="deposit_reward", name="入金奖励", definition="充值送奖励", examples=["Deposit $100 get $10"]),
            TaxonomyCategory(key="other", name="其他", definition="不属于以上任何类型", examples=[]),
        ],
    )


def test_build_catalog_extraction_prompt_includes_taxonomy_and_articles():
    rows = [{"uid": "u1", "title": "Deposit bonus", "content": "Deposit $100 to get $10"}]
    prompt = build_catalog_extraction_prompt(category="campaign", locale="EN", rows=rows, taxonomy=_taxonomy("campaign"))
    assert "deposit_reward" in prompt.user
    assert "u1" in prompt.user
    assert "Deposit bonus" in prompt.user
    assert "reward_form" in prompt.user  # campaign-only field
    assert "supported_market" not in prompt.user  # product-only field, must not leak into campaign


def test_build_catalog_extraction_prompt_product_fields_differ_from_campaign():
    rows = [{"uid": "u1", "title": "New API", "content": "Launched a new API"}]
    prompt = build_catalog_extraction_prompt(category="product", locale="EN", rows=rows, taxonomy=_taxonomy("product"))
    assert "supported_market" in prompt.user
    assert "reward_form" not in prompt.user


def test_build_catalog_extraction_prompt_rejects_unsupported_category():
    import pytest
    with pytest.raises(ValueError):
        build_catalog_extraction_prompt(category="listing", locale="EN", rows=[], taxonomy=_taxonomy("listing"))
