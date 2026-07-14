"""src/analysis/llm.py 单测：JSON 解析失败处理、evidence_indices 空数组强制改
「不适用」、uid 不在 related_uids 内时丢弃并记日志、diff_type 非法枚举强制修正、
cache key 计算的顺序无关性、call_llm 的 HTTP 调用（mock http_fetch，不发真实请求）。
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
from src.analysis.zmx_index import ZmxArticle


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
        ZmxArticle(uid="z1", title="t1", content_preview="p1", post_time="2026-01-01T00:00:00Z", similarity_score=0.5),
        ZmxArticle(uid="z2", title="t2", content_preview="p2", post_time="2026-01-02T00:00:00Z", similarity_score=0.6),
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
