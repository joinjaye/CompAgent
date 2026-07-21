"""src/analysis/llm.py 单测：staged-v1 两段响应校验
（validate_fact_extraction/validate_business_judgment，Phase②新增，取代旧的单体
validate_and_normalize）、批次级 diff_type 的纯程序化聚合
（aggregate_batch_diff_type）、缓存读写、call_llm 的 HTTP 调用（mock http_fetch，
不发真实请求）。
"""

from __future__ import annotations

import json
import sqlite3

from src.analysis.config import LlmCredentials
from src.analysis.llm import (
    aggregate_batch_diff_type,
    call_llm,
    get_cached_response,
    set_cached_response,
    validate_business_judgment,
    validate_fact_extraction,
)
from src.analysis.zmx_catalog import ZmxCatalogEntry


# ---------------------------------------------------------------- validate_fact_extraction ----


def test_validate_fact_extraction_json_parse_failure_returns_invalid():
    result = validate_fact_extraction("not a json {{{", expected_index=1)
    assert result.valid is False
    assert any("json_parse_failed" in i for i in result.issues)


def test_validate_fact_extraction_strips_markdown_code_fences():
    raw = '```json\n{"i": 1, "event_type": "created", "confidence": 0.9}\n```'
    result = validate_fact_extraction(raw, expected_index=1)
    assert result.valid is True
    assert result.event_type == "created"


def test_validate_fact_extraction_corrects_index_mismatch_without_invalidating():
    raw = json.dumps({"i": 99, "event_type": "created"})
    result = validate_fact_extraction(raw, expected_index=1)
    assert result.valid is True
    assert result.index == 1
    assert any("index_mismatch" in i for i in result.issues)


def test_validate_fact_extraction_invalid_event_type_becomes_unknown():
    raw = json.dumps({"i": 1, "event_type": "something_made_up"})
    result = validate_fact_extraction(raw, expected_index=1)
    assert result.event_type == "unknown"
    assert any("invalid_event_type" in i for i in result.issues)


def test_validate_fact_extraction_confidence_clamped_to_0_1():
    raw = json.dumps({"i": 1, "event_type": "created", "confidence": 5})
    result = validate_fact_extraction(raw, expected_index=1)
    assert result.confidence == 1.0


def test_validate_fact_extraction_reads_evidence_target_users_and_changes():
    raw = json.dumps({
        "i": 1, "event_type": "reward_changed", "confidence": 0.8,
        "target_users": ["新用户", 123, "VIP"],
        "changes": [{"field": "reward", "before": 100, "after": 200}, "not-a-dict"],
        "evidence": ["a", "b", "c", "d", "e", "f"],
    })
    result = validate_fact_extraction(raw, expected_index=1)
    assert result.target_users == ["新用户", "VIP"]
    assert result.changes == [{"field": "reward", "before": 100, "after": 200}]
    assert len(result.evidence) == 5  # 最多 5 条


# ---------------------------------------------------------------- validate_business_judgment ----


def _entry(uid: str, mechanism_type: str = "deposit_reward") -> ZmxCatalogEntry:
    return ZmxCatalogEntry(
        uid=uid, title="t", mechanism_type=mechanism_type, key_mechanics=None,
        reward_range=None, target_users=None, start_date=None, end_date=None, post_time=None,
    )


def test_validate_business_judgment_json_parse_failure_returns_invalid():
    result = validate_business_judgment("not json", expected_indices={1}, candidates_by_index={})
    assert result.valid is False


def test_validate_business_judgment_maps_gap_type_to_diff_type():
    # confirmed_gap 也需要至少有候选可看（哪怕候选最终都被判定为"不是同一回事"），
    # 完全没有候选时无法真正"确认"缺失，见下面 test_..._no_candidates_forces_...
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "confirmed_gap", "business_impact": "high", "novelty": 2, "urgency": 1,
         "zmx_evidence": [], "reason": "x"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={1: [_entry("z1")]})
    assert result.items[0].gap_type == "confirmed_gap"
    assert result.items[0].diff_type == "ZMX缺失"


def test_validate_business_judgment_baseline_not_found_maps_to_not_applicable_diff_type():
    """关键区分：baseline_not_found（没召回到候选）不等于 confirmed_gap（确认缺失），
    映射到的 diff_type 必须是「不适用」，不是「ZMX缺失」——这是防止误报缺失的核心。
    """
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "baseline_not_found", "business_impact": "low", "novelty": 0, "urgency": 0,
         "zmx_evidence": [], "reason": "无候选"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={1: []})
    assert result.items[0].diff_type == "不适用"


def test_validate_business_judgment_drops_item_not_in_batch():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "not_applicable", "business_impact": "low", "reason": "in batch"},
        {"i": 99, "gap_type": "not_applicable", "business_impact": "low", "reason": "not in batch"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={})
    assert len(result.items) == 1
    assert result.items[0].index == 1
    assert any("dropped_item_index_not_in_batch" in i for i in result.issues)


def test_validate_business_judgment_no_candidates_forces_baseline_not_found():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "confirmed_gap", "business_impact": "high", "reason": "x"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={1: []})
    assert result.items[0].gap_type == "baseline_not_found"
    assert any("no_candidates_forced_baseline_not_found" in i for i in result.issues)


def test_validate_business_judgment_empty_evidence_forces_baseline_not_found():
    """有候选，但没引用任何一条作为证据时断言 covered/different_mechanism 一样是
    防幻觉降级对象——跟旧版 evidence_indices 空数组强制"不适用"是同一条规则。"""
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "covered", "business_impact": "low", "zmx_evidence": [], "reason": "x"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={1: [_entry("z1")]})
    assert result.items[0].gap_type == "baseline_not_found"
    assert any("empty_evidence_forced_baseline_not_found" in i for i in result.issues)


def test_validate_business_judgment_maps_evidence_position_to_real_zmx_uid():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "covered", "business_impact": "low", "zmx_evidence": [2], "reason": "见候选2"},
    ]})
    candidates = {1: [_entry("z1"), _entry("z2")]}
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index=candidates)
    assert result.items[0].zmx_evidence_uids == ["z2"]


def test_validate_business_judgment_out_of_range_evidence_ignored():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "covered", "business_impact": "low", "zmx_evidence": [99], "reason": "x"},
    ]})
    candidates = {1: [_entry("z1")]}
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index=candidates)
    # 越界证据被丢弃 -> 没有真实证据 -> 防幻觉降级
    assert result.items[0].zmx_evidence_uids == []
    assert result.items[0].gap_type == "baseline_not_found"


def test_validate_business_judgment_invalid_gap_type_becomes_not_applicable():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "something_weird", "business_impact": "low", "reason": "x"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={1: []})
    assert result.items[0].gap_type == "not_applicable"
    assert result.items[0].diff_type == "不适用"


def test_validate_business_judgment_invalid_business_impact_defaults_to_low():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "not_applicable", "business_impact": "super_high", "reason": "x"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={})
    assert result.items[0].business_impact == "low"


def test_validate_business_judgment_reports_missing_indices():
    raw = json.dumps({"items": [{"i": 1, "gap_type": "not_applicable", "business_impact": "low"}]})
    result = validate_business_judgment(raw, expected_indices={1, 2}, candidates_by_index={})
    assert any(issue == "missing_item_indices:2" for issue in result.issues)


def test_validate_business_judgment_drops_duplicate_index():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "not_applicable", "business_impact": "low"},
        {"i": 1, "gap_type": "not_applicable", "business_impact": "low"},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={})
    assert len(result.items) == 1
    assert any("dropped_duplicate_index" in i for i in result.issues)


def test_validate_business_judgment_novelty_urgency_clamped_0_3():
    raw = json.dumps({"items": [
        {"i": 1, "gap_type": "not_applicable", "business_impact": "low", "novelty": 99, "urgency": -5},
    ]})
    result = validate_business_judgment(raw, expected_indices={1}, candidates_by_index={})
    assert result.items[0].novelty == 3
    assert result.items[0].urgency == 0


# ---------------------------------------------------------------- aggregate_batch_diff_type ----


def test_aggregate_batch_diff_type_all_not_applicable():
    assert aggregate_batch_diff_type(["不适用", "不适用"]) == "不适用"


def test_aggregate_batch_diff_type_empty_batch():
    assert aggregate_batch_diff_type([]) == "不适用"


def test_aggregate_batch_diff_type_single_non_na_value_passthrough():
    assert aggregate_batch_diff_type(["不适用", "ZMX缺失", "不适用"]) == "ZMX缺失"


def test_aggregate_batch_diff_type_multiple_distinct_values_is_mixed():
    assert aggregate_batch_diff_type(["ZMX已有", "ZMX缺失"]) == "混合"


# ---------------------------------------------------------------- cache / call_llm ----


def test_cache_roundtrip():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE llm_cache (cache_key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL)")
    key = "some-key"
    assert get_cached_response(conn, key) is None
    set_cached_response(conn, key, '{"ok": true}')
    assert get_cached_response(conn, key) == '{"ok": true}'
    conn.close()


def test_call_llm_posts_openai_compatible_request(monkeypatch):
    captured = {}

    def fake_fetch(url, *, method, headers, body, timeout, max_retries):
        captured["url"] = url
        captured["method"] = method
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return json.dumps({
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"total_tokens": 123},
        })

    monkeypatch.setattr("src.analysis.llm.http_fetch", fake_fetch)

    creds = LlmCredentials(api_key="sk-test", api_base="https://api.example.com/v1", model="gpt-test")
    content, tokens = call_llm(
        "system prompt", "user prompt",
        credentials=creds, model="gpt-test", temperature=0, max_tokens=1000,
        timeout_s=30, max_retries=2,
    )

    assert content == '{"ok": true}'
    assert tokens == 123
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["messages"][0] == {"role": "system", "content": "system prompt"}
    assert captured["body"]["messages"][1] == {"role": "user", "content": "user prompt"}
    assert captured["body"]["temperature"] == 0
    assert captured["body"]["max_tokens"] == 1000
