"""src/analysis/daily_digest.py 单测：dry_run 不查缓存/不调 LLM、无批次时的行为、
缓存命中路径的校验，全部离线（真实 schema.sql 建的临时库 + 直接 INSERT insights，
不经过完整的 Phase 4 批次分析流程，因为这里只关心"读批次 -> 出 prompt/摘要"这一段）。
"""

from __future__ import annotations

import json

import pytest

from src.analysis.daily_digest import (
    compute_digest_cache_key,
    generate_daily_digest,
    load_locale_batches,
)
from src.analysis.llm import set_cached_response
from src.db.connection import SCHEMA_PATH, get_connection

BATCH_DATE = "2026-07-15"


@pytest.fixture()
def conn(tmp_path):
    with get_connection(tmp_path / "test.db") as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield conn


def _insert_insight(conn, *, id_, source, category, locale, summary, zmx_diff=None,
                     diff_type="不适用", priority="中", article_count=1):
    conn.execute(
        """INSERT INTO insights
           (id, batch_date, source, category, locale, article_count, related_uids,
            is_locale_derived, summary, articles_analysis, zmx_diff, diff_type, priority,
            zmx_evidence_uids, prompt_version, llm_tokens_used, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, '[]', 0, ?, '[]', ?, ?, ?, '[]', 'test-v1', 10, ?, ?)""",
        (id_, BATCH_DATE, source, category, locale, article_count, summary, zmx_diff,
         diff_type, priority, f"{BATCH_DATE}T00:00:00Z", f"{BATCH_DATE}T00:00:00Z"),
    )
    conn.commit()


def test_no_batches_returns_not_generated(conn):
    result = generate_daily_digest(conn, "EN", BATCH_DATE, dry_run=True)
    assert result.generated is False
    assert "no_batches_for_locale_date" in result.issues


def test_dry_run_builds_prompt_but_does_not_generate(conn):
    _insert_insight(conn, id_="b1", source="Bitunix", category="campaign", locale="EN",
                     summary="今日 Bitunix 发布一场交易大赛")
    result = generate_daily_digest(conn, "EN", BATCH_DATE, dry_run=True)
    assert result.generated is False
    assert result.from_cache is False
    assert result.daily_summary is None
    assert result.prompt is not None
    assert "Bitunix" in result.prompt.user
    assert "今日 Bitunix 发布一场交易大赛" in result.prompt.user
    assert result.cache_key is not None


def test_cache_key_stable_regardless_of_row_order(conn):
    _insert_insight(conn, id_="b1", source="Bitunix", category="campaign", locale="EN", summary="s1")
    _insert_insight(conn, id_="b2", source="BingX", category="listing", locale="EN", summary="s2")
    batches = load_locale_batches(conn, "EN", BATCH_DATE)
    key_a = compute_digest_cache_key(batches)
    key_b = compute_digest_cache_key(list(reversed(batches)))
    assert key_a == key_b


def test_cache_key_changes_when_batch_set_changes(conn):
    _insert_insight(conn, id_="b1", source="Bitunix", category="campaign", locale="EN", summary="s1")
    batches_before = load_locale_batches(conn, "EN", BATCH_DATE)
    key_before = compute_digest_cache_key(batches_before)

    _insert_insight(conn, id_="b2", source="BingX", category="listing", locale="EN", summary="s2")
    batches_after = load_locale_batches(conn, "EN", BATCH_DATE)
    key_after = compute_digest_cache_key(batches_after)

    assert key_before != key_after


def test_cache_hit_returns_parsed_summary_without_calling_llm(conn, monkeypatch):
    _insert_insight(conn, id_="b1", source="Bitunix", category="campaign", locale="EN", summary="s1")
    batches = load_locale_batches(conn, "EN", BATCH_DATE)
    cache_key = compute_digest_cache_key(batches)
    set_cached_response(
        conn, cache_key,
        json.dumps({"daily_summary": "今日综述内容", "priority_focus": "关注 Bitunix 活动"}),
    )
    conn.commit()

    def _boom(*args, **kwargs):
        raise AssertionError("不应该调用 call_llm：缓存应该命中")

    monkeypatch.setattr("src.analysis.daily_digest.call_llm", _boom)

    result = generate_daily_digest(conn, "EN", BATCH_DATE, dry_run=False)
    assert result.generated is True
    assert result.from_cache is True
    assert result.daily_summary == "今日综述内容"
    assert result.priority_focus == "关注 Bitunix 活动"
    assert result.tokens_used == 0


def test_cache_hit_with_invalid_json_returns_not_generated(conn):
    _insert_insight(conn, id_="b1", source="Bitunix", category="campaign", locale="EN", summary="s1")
    batches = load_locale_batches(conn, "EN", BATCH_DATE)
    cache_key = compute_digest_cache_key(batches)
    set_cached_response(conn, cache_key, "not valid json")
    conn.commit()

    result = generate_daily_digest(conn, "EN", BATCH_DATE, dry_run=False)
    assert result.generated is False
    assert result.from_cache is True
    assert any("json_parse_failed" in i for i in result.issues)


def test_missing_credentials_raises_when_not_dry_run_and_cache_miss(conn):
    _insert_insight(conn, id_="b1", source="Bitunix", category="campaign", locale="EN", summary="s1")
    with pytest.raises(ValueError):
        generate_daily_digest(conn, "EN", BATCH_DATE, dry_run=False, credentials=None)
