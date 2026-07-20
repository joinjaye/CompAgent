"""src/analysis/llm.py 单测：JSON 解析失败处理、evidence_indices 空数组强制改
「不适用」、uid 不在 related_uids 内时丢弃并记日志、diff_type 非法枚举强制修正、
cache key 计算的顺序无关性、call_llm 的 HTTP 调用（mock http_fetch，不发真实请求）。

-v2（2026-07-20）新增：articles[] 逐条字段（diff_type/priority/follow_up/
change_kind/listing_kind）的校验测试，见 test_validate_and_normalize_article_*。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.analysis.config import LlmCredentials
from src.analysis.llm import (
    call_llm,
    compute_cache_key,
    get_cached_response,
    set_cached_response,
    validate_and_normalize,
)
from src.analysis.zmx_baseline import ZmxBaselineEntry


def test_validate_and_normalize_json_parse_failure_returns_invalid():
    result = validate_and_normalize("not a json {{{", category="campaign", related_uids={"u1"})
    assert result.valid is False
    assert result.summary is None
    assert result.articles_analysis == []
    assert result.zmx_diff is None
    assert any("json_parse_failed" in i for i in result.issues)


def test_validate_and_normalize_strips_markdown_code_fences():
    raw = '```json\n{"batch_summary": "s", "articles": [], "zmx_comparison": {"diff_type": "不适用", "analysis": null, "evidence_indices": [], "priority": "低", "priority_reason": "r"}}\n```'
    result = validate_and_normalize(raw, category="campaign", related_uids=set())
    assert result.valid is True
    assert result.summary == "s"


def test_validate_and_normalize_drops_article_uid_not_in_batch():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [
            {"uid": "u1", "title": "in batch"},
            {"uid": "u_ghost", "title": "not in batch"},
        ],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert len(result.articles_analysis) == 1
    assert result.articles_analysis[0]["uid"] == "u1"
    assert any("dropped_article_uid_not_in_batch" in i for i in result.issues)


def test_validate_and_normalize_forces_not_applicable_when_evidence_indices_empty():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [],
        "zmx_comparison": {"diff_type": "ZMX缺失", "analysis": "some analysis", "evidence_indices": [], "priority": "高", "priority_reason": "r"},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids=set())
    assert result.diff_type == "不适用"
    assert any("empty_evidence_indices" in i for i in result.issues)


def test_validate_and_normalize_forces_not_applicable_on_invalid_enum():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [],
        "zmx_comparison": {"diff_type": "某种奇怪的值", "analysis": "x", "evidence_indices": [1], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids=set())
    assert result.diff_type == "不适用"
    assert any("invalid_diff_type" in i for i in result.issues)


def test_validate_and_normalize_listing_rejects_zmx_variant_type():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [],
        "zmx_comparison": {"diff_type": "ZMX玩法不同", "analysis": "x", "evidence_indices": [1], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="listing", related_uids=set())
    # listing 的合法枚举不含 "ZMX玩法不同"
    assert result.diff_type == "不适用"


def test_validate_and_normalize_delisting_always_not_applicable_even_with_evidence():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [],
        "zmx_comparison": {"diff_type": "ZMX缺失", "analysis": "x", "evidence_indices": [1], "priority": "高", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="delisting", related_uids=set())
    assert result.diff_type == "不适用"


def test_validate_and_normalize_maps_evidence_indices_to_zmx_uids():
    hits = [
        ZmxBaselineEntry(uid="z1", title="t1", mechanism_type="入金活动", key_mechanics=None,
                          reward_range=None, target_users=None, start_date=None, end_date=None,
                          post_time="2026-01-01T00:00:00Z"),
        ZmxBaselineEntry(uid="z2", title="t2", mechanism_type="交易赛", key_mechanics=None,
                          reward_range=None, target_users=None, start_date=None, end_date=None,
                          post_time="2026-01-02T00:00:00Z"),
    ]
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [],
        "zmx_comparison": {"diff_type": "ZMX已有", "analysis": "见 [Z2]", "evidence_indices": [2], "priority": "低", "priority_reason": "因为..."},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids=set(), zmx_hits=hits)
    assert result.zmx_evidence_uids == ["z2"]
    assert "优先级依据：因为..." in result.zmx_diff


def test_validate_and_normalize_ignores_out_of_range_evidence_index():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [],
        "zmx_comparison": {"diff_type": "ZMX已有", "analysis": "x", "evidence_indices": [99], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids=set(), zmx_hits=[])
    assert result.zmx_evidence_uids == []


def _article(**overrides):
    base = {
        "uid": "u1", "title": "t", "diff_type": "ZMX缺失", "evidence_indices": [1],
        "priority": "高", "follow_up": "跟进一下",
    }
    base.update(overrides)
    return base


def test_validate_and_normalize_article_listing_rejects_zmx_variant_type():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="ZMX玩法不同")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="listing", related_uids={"u1"})
    assert result.articles_analysis[0]["diff_type"] == "不适用"
    assert any("invalid_diff_type" in i for i in result.issues)


def test_validate_and_normalize_article_empty_evidence_forces_not_applicable():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="ZMX缺失", evidence_indices=[])],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert result.articles_analysis[0]["diff_type"] == "不适用"
    assert any("empty_evidence_indices_forced_not_applicable" in i for i in result.issues)


def test_validate_and_normalize_article_delisting_diff_type_always_not_applicable():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="ZMX缺失", evidence_indices=[1])],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="delisting", related_uids={"u1"})
    assert result.articles_analysis[0]["diff_type"] == "不适用"


def test_validate_and_normalize_article_invalid_priority_becomes_null_not_fabricated():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(priority="超级高")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert result.articles_analysis[0]["priority"] is None
    assert any("invalid_priority" in i for i in result.issues)


def test_validate_and_normalize_article_change_kind_kept_only_for_campaign_and_changed_status():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="不适用", evidence_indices=[], change_kind="reward")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(
        raw, category="campaign", related_uids={"u1"}, article_status={"u1": "changed"}
    )
    assert result.articles_analysis[0]["change_kind"] == "reward"


def test_validate_and_normalize_article_change_kind_null_when_status_not_changed():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="不适用", evidence_indices=[], change_kind="reward")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(
        raw, category="campaign", related_uids={"u1"}, article_status={"u1": "new"}
    )
    assert result.articles_analysis[0]["change_kind"] is None


def test_validate_and_normalize_article_change_kind_null_without_article_status_param():
    # 不传 article_status（默认 None）等价于"每条状态都不是 changed"，change_kind 恒 null，
    # 这保证了所有既有调用方/测试（不知道这个新参数）行为不受影响。
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="不适用", evidence_indices=[], change_kind="reward")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert result.articles_analysis[0]["change_kind"] is None


def test_validate_and_normalize_article_change_kind_null_for_non_campaign():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="不适用", evidence_indices=[], change_kind="reward")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(
        raw, category="product", related_uids={"u1"}, article_status={"u1": "changed"}
    )
    assert result.articles_analysis[0]["change_kind"] is None


def test_validate_and_normalize_article_listing_kind_only_kept_for_listing():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="不适用", evidence_indices=[], listing_kind="spot")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    listing_result = validate_and_normalize(raw, category="listing", related_uids={"u1"})
    assert listing_result.articles_analysis[0]["listing_kind"] == "spot"

    campaign_result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert campaign_result.articles_analysis[0]["listing_kind"] is None


def test_validate_and_normalize_article_listing_kind_invalid_value_becomes_null():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="不适用", evidence_indices=[], listing_kind="两者均有")],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="listing", related_uids={"u1"})
    assert result.articles_analysis[0]["listing_kind"] is None


def test_validate_and_normalize_invalid_article_field_does_not_drop_whole_article():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(diff_type="某种奇怪的值", priority="乱填", follow_up=123)],
        "zmx_comparison": {"diff_type": "不适用", "analysis": None, "evidence_indices": [], "priority": "低", "priority_reason": None},
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert len(result.articles_analysis) == 1
    article = result.articles_analysis[0]
    assert article["uid"] == "u1"
    assert article["diff_type"] == "不适用"
    assert article["priority"] is None
    assert article["follow_up"] is None


def test_validate_and_normalize_business_action_fields():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(
            priority="高", priority_reason="奖池由 10,000 增至 50,000 USDT",
            action_type="campaign_design", owner="campaign_ops",
            follow_up="两日内输出 SEA 合约交易赛奖池对比表",
        )],
        "zmx_comparison": {
            "diff_type": "不适用", "analysis": None, "evidence_indices": [],
            "priority": "低", "priority_reason": None,
        },
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    article = result.articles_analysis[0]
    assert article["priority_reason"].startswith("奖池")
    assert article["action_type"] == "campaign_design"
    assert article["owner"] == "campaign_ops"
    assert article["follow_up"].startswith("两日内")


def test_validate_and_normalize_removes_generic_action_and_invalid_enums():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(
            priority="高", priority_reason=None, action_type="do_something",
            owner="anyone", follow_up="建议关注",
        )],
        "zmx_comparison": {
            "diff_type": "不适用", "analysis": None, "evidence_indices": [],
            "priority": "低", "priority_reason": None,
        },
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    article = result.articles_analysis[0]
    assert article["priority_reason"] is None
    assert article["action_type"] is None
    assert article["owner"] is None
    assert article["follow_up"] is None
    assert any("generic_follow_up_removed" in i for i in result.issues)
    assert any("missing_priority_reason" in i for i in result.issues)


def test_validate_and_normalize_reports_missing_batch_articles():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(uid="u1")],
        "zmx_comparison": {
            "diff_type": "不适用", "analysis": None, "evidence_indices": [],
            "priority": "低", "priority_reason": None,
        },
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1", "u2"})
    assert result.valid is True
    assert any(issue == "missing_article_uids:u2" for issue in result.issues)


def test_validate_and_normalize_drops_duplicate_article_uid():
    raw = json.dumps({
        "batch_summary": "s",
        "articles": [_article(uid="u1"), _article(uid="u1")],
        "zmx_comparison": {
            "diff_type": "不适用", "analysis": None, "evidence_indices": [],
            "priority": "低", "priority_reason": None,
        },
    })
    result = validate_and_normalize(raw, category="campaign", related_uids={"u1"})
    assert len(result.articles_analysis) == 1
    assert any(issue == "dropped_duplicate_article_uid:u1" for issue in result.issues)


def test_compute_cache_key_is_order_independent():
    k1 = compute_cache_key(["hashA", "hashB"], "campaign-v1")
    k2 = compute_cache_key(["hashB", "hashA"], "campaign-v1")
    assert k1 == k2


def test_compute_cache_key_changes_with_content_or_prompt_version():
    base = compute_cache_key(["hashA", "hashB"], "campaign-v1")
    assert base != compute_cache_key(["hashA", "hashC"], "campaign-v1")
    assert base != compute_cache_key(["hashA", "hashB"], "campaign-v2")


def test_cache_roundtrip():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE llm_cache (cache_key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL)")
    key = compute_cache_key(["h1"], "campaign-v1")
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
