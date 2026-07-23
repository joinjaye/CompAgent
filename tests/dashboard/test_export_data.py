"""src/dashboard/export_data.py 单测：category-first schema 的 8 个顶层 key 都在、
search_index 字段窄且不含正文、老批次（缺 5 个新字段）优雅降级不报错、delisting 行
在 listing 段里正确带 category 标签、Listing/Delisting 不暴露旧 LLM 分析。

全部离线：真实 schema.sql 建的临时库 + upsert_announcement/upsert_insight 直接写行，
不发真实请求、不调用真实 LLM。
"""

from __future__ import annotations

import json

import pytest

from src.analysis.daily_digest import compute_digest_cache_key
from src.analysis.llm import set_cached_response
from src.analysis.run import upsert_insight
from src.dashboard.export_data import (
    _derive_follow_up,
    _format_zmx_reward,
    build_business_table_links,
    build_daily_digest,
    build_dashboard_data,
)
from src.db.connection import get_connection
from src.db.operations import upsert_announcement

BATCH_DATE = "2026-07-15"


def test_format_zmx_reward_accepts_numeric_amount():
    assert _format_zmx_reward(100, "USDT", "coupon") == "100 USDT（coupon）"


def test_business_table_links_prefer_explicit_url_and_build_fallback():
    links = build_business_table_links({
        "FEISHU_BITABLE_BASE_URL": "https://tenant.example/base/",
        "FEISHU_CAMPAIGN_TABLE_URL": "https://explicit.example/campaign",
        "FEISHU_PRODUCT_APP_TOKEN": "product-app",
        "FEISHU_PRODUCT_TABLE_ID": "product-table",
    })
    assert links == {
        "campaign": "https://explicit.example/campaign",
        "product": "https://tenant.example/base/product-app?table=product-table",
        "listing": "",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def conn(db_path):
    from src.db.connection import SCHEMA_PATH
    with get_connection(db_path) as c:
        c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        yield c


def _insert(conn, *, source, locale, article_id, category, status_hint="new",
            title="T", content="C", url=None, group_id=None, is_region_exclusive=False):
    result = upsert_announcement(
        conn, source=source, locale=locale, article_id=article_id,
        title=title, content=content, url=url, post_time=f"{BATCH_DATE}T00:00:00Z",
        fetched_at=f"{BATCH_DATE}T01:00:00Z", category=category, group_id=group_id,
        is_region_exclusive=is_region_exclusive,
    )
    conn.execute(
        """UPDATE announcements
           SET category = ?, status = ?,
               update_time = CASE WHEN ? = 'changed' THEN fetched_at ELSE update_time END
           WHERE uid = ?""",
        (category, status_hint, status_hint, result.uid),
    )
    return result.uid


def _insight(conn, *, source, category, locale, uids, articles, prompt_version="test-v2", tokens=100):
    upsert_insight(
        conn, insight_id=f"{source}_{category}_{locale}_{BATCH_DATE}", batch_date=BATCH_DATE,
        source=source, category=category, locale=locale, related_uids=uids,
        is_locale_derived=False, derived_from_id=None, summary="s",
        articles_analysis=articles, zmx_diff=None, diff_type="不适用", priority="低",
        zmx_evidence_uids=[], prompt_version=prompt_version, llm_tokens_used=tokens,
    )


def test_all_top_level_keys_present(conn, db_path):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    conn.commit()
    data = build_dashboard_data(str(db_path))
    for key in [
        "meta", "overview", "daily_digest", "trend", "campaign", "campaign_all",
        "product", "product_all", "listing", "listing_all", "announcements",
        "announcements_all", "markets", "search_index", "quality",
    ]:
        assert key in data, key


def test_all_sections_are_limited_to_daily_scope(conn, db_path):
    product_uid = _insert(conn, source="Bitunix", locale="EN", article_id="p", category="product")
    listing_uid = _insert(conn, source="Bitunix", locale="EN", article_id="l", category="listing")
    conn.execute(
        "UPDATE announcements SET fetched_at='2026-07-14T01:00:00Z' WHERE uid IN (?, ?)",
        (product_uid, listing_uid),
    )
    _insert(conn, source="Weex", locale="EN", article_id="today", category="campaign")
    conn.commit()

    data = build_dashboard_data(str(db_path))
    assert data["product"] == []
    assert data["product_all"] == []
    assert data["listing"] == []
    assert data["listing_all"] == []


def test_search_index_rows_have_only_specified_fields_no_content_leak(conn, db_path):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign", content="secret body text")
    conn.commit()
    data = build_dashboard_data(str(db_path))
    rows = data["search_index"]["rows"]
    assert len(rows) == 1
    expected_keys = {
            "uid", "group_id", "source", "locale", "markets", "localized_variants",
            "category", "title", "post_time", "status", "diff_type", "diff_tag", "priority", "url",
    }
    assert set(rows[0].keys()) == expected_keys
    assert "content" not in rows[0]
    assert "body" not in rows[0]
    assert "secret body text" not in json.dumps(rows[0])


def test_old_shaped_articles_analysis_missing_new_fields_degrades_gracefully(conn, db_path):
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    conn.commit()
    # 模拟 Phase 4 -v2 上线前的老批次：articles_analysis 里完全没有 diff_type/priority/
    # follow_up/change_kind/listing_kind 这几个新 key。
    old_shaped_article = {"uid": uid, "title": "T", "mechanics": "old shape, no new fields"}
    _insight(conn, source="Bitunix", category="campaign", locale="EN", uids=[uid], articles=[old_shaped_article])
    conn.commit()

    data = build_dashboard_data(str(db_path))  # 不应抛异常
    campaign_rows = data["campaign"]
    assert len(campaign_rows) == 1
    row = campaign_rows[0]
    assert row["diff_type"] is None
    assert row["priority"] is None
    assert row["priority_reason"] is None
    assert row["action_type"] is None
    assert row["owner"] is None
    assert row["follow_up"] is None
    assert row["change_kind"] is None
    assert row["listing_kind"] is None
    assert row["diff_tag"] == "na"

    search_row = data["search_index"]["rows"][0]
    assert search_row["diff_type"] is None
    assert search_row["priority"] is None


def test_validation_failed_batch_null_articles_analysis_does_not_raise(conn, db_path):
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    conn.commit()
    _insight(conn, source="Bitunix", category="campaign", locale="EN", uids=[uid], articles=None)
    conn.commit()
    data = build_dashboard_data(str(db_path))  # articles_analysis=NULL 不应抛异常
    assert data["campaign"][0]["diff_type"] is None


def test_delisting_rows_tagged_within_listing_section(conn, db_path):
    listing_uid = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="listing")
    delisting_uid = _insert(conn, source="Bitunix", locale="EN", article_id="2", category="delisting")
    conn.commit()
    _insight(conn, source="Bitunix", category="listing", locale="EN", uids=[listing_uid],
             articles=[{"uid": listing_uid, "diff_type": "ZMX已有", "priority": "低"}])
    _insight(conn, source="Bitunix", category="delisting", locale="EN", uids=[delisting_uid],
             articles=[{"uid": delisting_uid, "diff_type": "不适用", "priority": "高"}])
    conn.commit()

    data = build_dashboard_data(str(db_path))
    listing_rows = data["listing"]
    categories = {r["uid"]: r["category"] for r in listing_rows}
    assert categories[listing_uid] == "listing"
    assert categories[delisting_uid] == "delisting"


def test_en_asia_placeholder_is_not_exported(conn, db_path):
    _insert(conn, source="Zoomex", locale="EN-Asia", article_id="z1", category="campaign")
    conn.commit()
    data = build_dashboard_data(str(db_path))
    assert "baseline_by_locale" not in data["markets"]
    assert all(r["source"] != "Zoomex" for r in data["campaign"])


def test_listing_uses_title_rule_and_ignores_legacy_llm_fields(conn, db_path):
    uid = _insert(
        conn, source="Bitunix", locale="EN", article_id="1", category="listing",
        title="ABCUSDT Perpetual Contract Is Now Live",
    )
    conn.commit()
    _insight(
        conn, source="Bitunix", category="listing", locale="EN", uids=[uid],
        articles=[{
            "uid": uid, "listing_kind": "spot", "diff_type": "ZMX缺失",
            "priority": "高", "follow_up": "legacy",
        }],
    )
    conn.commit()

    row = build_dashboard_data(str(db_path))["listing"][0]
    assert row["listing_kind"] == "perp"
    assert row["diff_type"] is None
    assert row["priority"] is None
    assert row["follow_up"] is None


def test_listing_exports_category_only_analysis_without_zmx_judgment(conn, db_path):
    uid = _insert(
        conn, source="Bitunix", locale="EN", article_id="ai", category="listing",
        title="ABC/USDT Perpetual Contract Is Now Live",
    )
    conn.commit()
    _insight(
        conn, source="Bitunix", category="listing", locale="EN", uids=[uid],
        articles=[{
            "uid": uid, "token_symbol": "ABC", "trading_pair": "ABC/USDT",
            "listing_type": "Perpetual", "listing_status": "New Listing",
            "token_category": "AI", "classification_confidence": 0.91,
            "diff_type": "ZMX缺失", "priority": "高", "follow_up": "legacy",
        }],
    )
    conn.commit()

    row = build_dashboard_data(str(db_path))["listing"][0]
    assert row["token_symbol"] == "ABC"
    assert row["trading_pair"] == "ABC/USDT"
    assert row["listing_type"] == "Perpetual"
    assert row["listing_status"] == "New Listing"
    assert row["token_category"] == "AI"
    assert row["classification_confidence"] == 0.91
    assert row["diff_type"] is None
    assert row["priority"] is None
    assert row["follow_up"] is None


def test_overview_chip_diff_breakdown_counts_per_article_diff_type(conn, db_path):
    uid1 = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    uid2 = _insert(conn, source="Bitunix", locale="EN", article_id="2", category="campaign")
    conn.commit()
    _insight(conn, source="Bitunix", category="campaign", locale="EN", uids=[uid1, uid2], articles=[
        {"uid": uid1, "diff_type": "ZMX缺失", "priority": "高"},
        {"uid": uid2, "diff_type": "ZMX已有", "priority": "低"},
    ])
    conn.commit()

    data = build_dashboard_data(str(db_path))
    chip = data["overview"]["chips"]["campaign"]
    assert chip["count_new"] == 2
    assert chip["diff_breakdown"]["missing"] == 1
    assert chip["diff_breakdown"]["broad"] == 1


def test_overview_highlights_use_business_priority_not_llm_priority(conn, db_path):
    uid_missing = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    uid_same = _insert(conn, source="Bitunix", locale="EN", article_id="2", category="product")
    uid_low_priority = _insert(conn, source="Bitunix", locale="EN", article_id="3", category="listing")
    conn.commit()
    _insight(conn, source="Bitunix", category="campaign", locale="EN", uids=[uid_missing],
             articles=[{"uid": uid_missing, "diff_type": "ZMX缺失", "priority": "高"}])
    _insight(conn, source="Bitunix", category="product", locale="EN", uids=[uid_same],
             articles=[{"uid": uid_same, "diff_type": "ZMX已有", "priority": "高"}])
    _insight(conn, source="Bitunix", category="listing", locale="EN", uids=[uid_low_priority],
             articles=[{"uid": uid_low_priority, "diff_type": "ZMX缺失", "priority": "低"}])
    conn.commit()

    data = build_dashboard_data(str(db_path))
    highlights = data["overview"]["highlights"]
    # 单个交易所最多两条，因此 P6 被截断；顺序仍只看业务规则，不受 LLM 高/中/低影响。
    assert len(highlights) == 2
    assert [h["priority"] for h in highlights] == ["P1", "P4"]


def test_overview_highlights_are_global_top_five_and_max_two_per_exchange(conn, db_path):
    for i in range(4):
        _insert(conn, source="Bitunix", locale="EN", article_id=f"b{i}", category="campaign",
                title=f"Bitunix campaign {i}")
    for i in range(4):
        _insert(conn, source="Weex", locale="EN", article_id=f"w{i}", category="campaign",
                title=f"Weex campaign {i}")
    _insert(conn, source="Phemex", locale="EN", article_id="p0", category="campaign",
            title="Phemex campaign")
    conn.commit()

    highlights = build_dashboard_data(str(db_path))["overview"]["highlights"]
    assert len(highlights) == 5
    assert sum(h["source"] == "Bitunix" for h in highlights) == 2
    assert sum(h["source"] == "Weex" for h in highlights) == 2
    assert sum(h["source"] == "Phemex" for h in highlights) == 1


def test_stale_first_seen_campaign_backfill_is_not_in_current_highlights(conn, db_path):
    uid = _insert(
        conn, source="Bitunix", locale="EN", article_id="old", category="campaign",
        title="<p>Historical Campaign</p>",
    )
    conn.execute(
        "UPDATE announcements SET post_time='2024-01-01T00:00:00Z' WHERE uid=?", (uid,)
    )
    conn.commit()
    data = build_dashboard_data(str(db_path))
    assert data["overview"]["highlights"] == []
    assert data["campaign_all"] == []


def test_first_seen_product_update_uses_p5_not_p4(conn, db_path):
    _insert(
        conn, source="Bitunix", locale="EN", article_id="update", category="product",
        title="Bitunix to Adjust Risk Limits",
    )
    conn.commit()
    highlight = build_dashboard_data(str(db_path))["overview"]["highlights"][0]
    assert highlight["priority"] == "P5"


def test_overview_summary_deduplicates_multilingual_group(conn, db_path):
    _insert(
        conn, source="Bitunix", locale="EN", article_id="en", category="campaign",
        group_id="same-campaign",
    )
    _insert(
        conn, source="Bitunix", locale="FR", article_id="fr", category="campaign",
        group_id="same-campaign",
    )
    conn.commit()
    chip = build_dashboard_data(str(db_path))["overview"]["chips"]["campaign"]
    assert chip["today"] == 1
    assert chip["count_new"] == 1


def test_detail_tables_merge_localized_variants_and_keep_parallel_links(conn, db_path):
    _insert(conn, source="Bitunix", locale="EN", article_id="en", category="product",
            group_id="same-product", title="Copy Trading Upgrade", url="https://example.com/en")
    _insert(conn, source="Bitunix", locale="FR", article_id="fr", category="product",
            group_id="same-product", title="Mise à niveau Copy Trading", url="https://example.com/fr")
    conn.commit()
    rows = build_dashboard_data(str(db_path))["product"]
    assert len(rows) == 1
    assert rows[0]["markets"] == ["EN", "FR"]
    assert {v["locale"] for v in rows[0]["localized_variants"]} == {"EN", "FR"}
    assert {v["url"] for v in rows[0]["localized_variants"]} == {
        "https://example.com/en", "https://example.com/fr",
    }


def _zmx_catalog_entry(conn, *, category, mechanism_type, exists_flag, capability_desc="desc", example_uids=()):
    conn.execute(
        """INSERT INTO zmx_catalog_entry (id, category, mechanism_type, exists_flag, capability_desc, example_uids, typical_reward, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, NULL, '2026-07-15T00:00:00Z')""",
        (f"{category}_{mechanism_type}", category, mechanism_type, exists_flag, capability_desc, json.dumps(list(example_uids))),
    )


def _zmx_summary_row(conn, *, source_uid, category, locale, mechanism_type, core_summary="s", reward_form=None):
    conn.execute(
        """INSERT INTO zmx_summary (source_uid, group_id, category, locale, mechanism_type, core_summary,
                                     reward_form, content_hash, is_locale_derived, prompt_version, created_at, updated_at)
           VALUES (?, NULL, ?, ?, ?, ?, ?, 'h', 0, 'test-v1', '2026-07-15T00:00:00Z', '2026-07-15T00:00:00Z')""",
        (source_uid, category, locale, mechanism_type, core_summary, reward_form),
    )


def test_product_row_exposes_zmx_catalog_fields_when_mechanism_type_matches(conn, db_path):
    """Phase③：campaign/product 行的 Zoomex 对照现在来自 zmx_catalog_entry（目录
    exists_flag）+ zmx_summary（具体对照示例），不再是旧版的确定性词项重叠匹配。"""
    zmx_uid = _insert(conn, source="Zoomex", locale="EN", article_id="z", category="product",
                       title="Copy Trading Leaderboard", url="https://example.com/zmx")
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="c", category="product",
                   title="Copy Trading Upgrade")
    conn.commit()
    _zmx_catalog_entry(conn, category="product", mechanism_type="copy_trading", exists_flag="yes")
    _zmx_summary_row(conn, source_uid=zmx_uid, category="product", locale="EN", mechanism_type="copy_trading")
    _insight(conn, source="Bitunix", category="product", locale="EN", uids=[uid], articles=[{
        "uid": uid, "feature": "Copy trading upgrade", "mechanism_type": "copy_trading",
        "diff_type": "ZMX已有", "diff_detail": "same mechanism", "zmx_counterpart_uids": [zmx_uid],
    }])
    conn.commit()

    row = build_dashboard_data(str(db_path))["product"][0]
    assert row["zmx_mechanism_type"] == "copy_trading"
    assert row["zmx_exists"] == "yes"
    assert row["zmx_capability_desc"] == "desc"
    assert row["zmx_counterpart"]["url"] == "https://example.com/zmx"
    assert row["comparison_status"] == "analyzed"


def test_diff_tag_downgrades_na_to_broad_when_catalog_confirms_exact_type_match(conn, db_path):
    """2026-07-22 真实数据发现：diff_tag='na'（Stage3 逐篇比对没找到具体对应项）
    跟 zmx_exists='yes'（Zoomex 目录按 mechanism_type 标签确认有同类型玩法）同时
    出现时，前端只显示"未进行对比"会跟旁边确实有内容的 Zoomex 卡片自相矛盾。
    exists_flag 精确命中（不是 'partial' 近似匹配）且没有具体 zmx_counterpart 时，
    展示用的 diff_tag 应该降级为 'broad'，不改写 diff_type/diff_detail 本身。"""
    uid = _insert(conn, source="Lbank", locale="EN", article_id="c", category="campaign",
                  title="P2P Limited-Time Offer for New Users")
    zmx_uid = _insert(conn, source="Zoomex", locale="EN", article_id="z", category="campaign",
                       title="First Trade Protection", url="https://www.zoomex.com/en/help/article/1")
    conn.commit()
    _zmx_catalog_entry(conn, category="campaign", mechanism_type="zero_risk_new_user", exists_flag="yes",
                        example_uids=[zmx_uid])
    _insight(conn, source="Lbank", category="campaign", locale="EN", uids=[uid], articles=[{
        "uid": uid, "mechanism_type": "zero_risk_new_user",
        "diff_type": "不适用", "diff_detail": "候选均不匹配", "zmx_counterpart_uids": [],
    }])
    conn.commit()

    row = build_dashboard_data(str(db_path))["campaign"][0]
    assert row["zmx_exists"] == "yes"
    assert row["zmx_counterpart"] is None
    assert row["diff_tag"] == "broad"
    assert row["diff_type"] == "不适用"  # 原始 Stage3 结论保留，不擅自改写
    assert row["diff_detail"] == "候选均不匹配"
    # 2026-07-22：粗粒度匹配也要能跳转到 Zoomex 目录示例文章，不能只有文字描述
    assert row["zmx_capability_url"] == "https://www.zoomex.com/en/help/article/1"


def test_diff_tag_upgrades_na_to_missing_when_catalog_confirms_no_history(conn, db_path):
    """2026-07-22：反过来，exists_flag='no' 是 rollup 覆盖 Zoomex 全量历史后确认
    这个 mechanism_type 从未出现过——比 Stage3 单批次窄召回的"没找到候选"是更强的
    证据，应该把展示用的 diff_tag 从 'na'（未进行对比）升级成 'missing'
    （未检索到同类），不能让用户以为这条压根没被比对过。"""
    uid = _insert(conn, source="Lbank", locale="EN", article_id="c", category="product")
    conn.commit()
    _zmx_catalog_entry(conn, category="product", mechanism_type="institutional", exists_flag="no")
    _insight(conn, source="Lbank", category="product", locale="EN", uids=[uid], articles=[{
        "uid": uid, "mechanism_type": "institutional",
        "diff_type": "不适用", "diff_detail": "候选均不匹配", "zmx_counterpart_uids": [],
    }])
    conn.commit()

    row = build_dashboard_data(str(db_path))["product"][0]
    assert row["diff_tag"] == "missing"
    assert row["diff_type"] == "不适用"  # 原始 Stage3 结论保留，不擅自改写
    assert row["zmx_exists"] is None
    assert row["zmx_capability_desc"] is None
    assert row["zmx_capability_url"] is None


def test_analyzed_na_becomes_missing_when_mechanism_type_has_no_catalog_entry(conn, db_path):
    uid = _insert(conn, source="Lbank", locale="EN", article_id="c", category="product")
    conn.commit()
    _insight(conn, source="Lbank", category="product", locale="EN", uids=[uid], articles=[{
        "uid": uid, "mechanism_type": None,
        "diff_type": "不适用", "diff_detail": "无法分类", "zmx_counterpart_uids": [],
    }])
    conn.commit()

    row = build_dashboard_data(str(db_path))["product"][0]
    assert row["zmx_exists"] is None
    assert row["diff_tag"] == "missing"


def test_analyzed_na_becomes_missing_when_catalog_match_is_only_partial(conn, db_path):
    """exists_flag='partial' 是词项重叠出来的近似匹配（rollup 自己标注"建议人工
    核对"），置信度不够，不应该触发 na->broad 的降级——只有精确标签命中('yes')
    才算数。"""
    uid = _insert(conn, source="Lbank", locale="EN", article_id="c", category="campaign")
    conn.commit()
    _zmx_catalog_entry(conn, category="campaign", mechanism_type="zero_risk_new_user", exists_flag="partial")
    _insight(conn, source="Lbank", category="campaign", locale="EN", uids=[uid], articles=[{
        "uid": uid, "mechanism_type": "zero_risk_new_user",
        "diff_type": "不适用", "diff_detail": "候选均不匹配", "zmx_counterpart_uids": [],
    }])
    conn.commit()

    row = build_dashboard_data(str(db_path))["campaign"][0]
    assert row["diff_tag"] == "missing"
    assert row["zmx_exists"] is None
    assert row["zmx_capability_desc"] is None


def test_missing_diff_never_exposes_stale_unrelated_zmx_catalog_content(conn, db_path):
    """回归：召回池第一项不能在“未检索到同类”时变成前端 Zoomex 对照内容。"""
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="c", category="product",
                  title="Funding Rate Settlement Frequency")
    zmx_uid = _insert(conn, source="Zoomex", locale="EN", article_id="z", category="product",
                      title="Strategy Bot Center", url="https://example.com/unrelated-bot")
    conn.commit()
    _zmx_catalog_entry(conn, category="product", mechanism_type="bot", exists_flag="yes",
                       example_uids=[zmx_uid])
    _insight(conn, source="Bitunix", category="product", locale="EN", uids=[uid], articles=[{
        "uid": uid, "mechanism_type": "bot", "diff_type": "ZMX缺失",
        "diff_detail": "未检索到资金费率结算同类", "zmx_counterpart_uids": [],
    }])
    conn.commit()

    row = build_dashboard_data(str(db_path))["product"][0]
    assert row["diff_tag"] == "missing"
    assert row["zmx_mechanism_type"] is None
    assert row["zmx_exists"] is None
    assert row["zmx_capability_desc"] is None
    assert row["zmx_capability_url"] is None
    assert row["zmx_counterpart"] is None


def test_row_without_any_analysis_is_pending_not_candidate_found():
    """退休的旧机制会把"没跑过分析"和"跑过但没找到"混为一谈；新设计里没有
    article_index 条目就是 pending，不是某种"未匹配"的分析结论。"""
    import sqlite3
    import tempfile
    from pathlib import Path as _Path

    from src.db.connection import SCHEMA_PATH, get_connection

    with tempfile.TemporaryDirectory() as tmp:
        db_path = _Path(tmp) / "test.db"
        with get_connection(db_path) as conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            _insert(conn, source="Bitunix", locale="EN", article_id="c", category="product", title="No analysis yet")
            conn.commit()
        data = build_dashboard_data(str(db_path))
        row = data["product"][0]
        assert row["comparison_status"] == "pending"
        assert row["zmx_exists"] is None
        assert "zmx_candidates" not in row  # 旧字段已彻底移除，不是留空


def test_overview_summary_deduplicates_same_title_when_source_group_ids_differ(conn, db_path):
    _insert(
        conn, source="Phemex", locale="EN", article_id="en", category="campaign",
        group_id="phemex-en", title="Same Campaign",
    )
    _insert(
        conn, source="Phemex", locale="FR", article_id="fr", category="campaign",
        group_id="phemex-fr", title="Same Campaign",
    )
    conn.commit()
    data = build_dashboard_data(str(db_path))
    assert data["overview"]["chips"]["campaign"]["today"] == 1
    assert len(data["overview"]["highlights"]) == 1


def test_trend_counts_new_and_changed_and_deduplicates_group(conn, db_path):
    _insert(
        conn, source="Bitunix", locale="EN", article_id="en", category="campaign",
        group_id="same-campaign", status_hint="new",
    )
    _insert(
        conn, source="Bitunix", locale="FR", article_id="fr", category="campaign",
        group_id="same-campaign", status_hint="new",
    )
    _insert(
        conn, source="Weex", locale="EN", article_id="changed", category="campaign",
        status_hint="changed",
    )
    conn.commit()
    trend = build_dashboard_data(str(db_path))["trend"]
    assert trend["series"]["campaign"] == [2]


# ---------------------------------------------------------------- Phase⑤: _derive_follow_up ----


def test_derive_follow_up_confirmed_gap_requires_multiple_sources():
    """诚实性核心：单一竞品的孤例不足以断言"行业共性趋势"，只有 mechanism_type
    在同批次被 ≥2 个不同竞品触及时，ZMX缺失 才升级为"建议评估跟进"。"""
    rows = [
        {"source": "Bitunix", "mechanism_type": "deposit_reward", "diff_type": "ZMX缺失", "follow_up": None},
    ]
    _derive_follow_up(rows)
    assert rows[0]["follow_up"] is None

    rows = [
        {"source": "Bitunix", "mechanism_type": "deposit_reward", "diff_type": "ZMX缺失", "follow_up": None},
        {"source": "Weex", "mechanism_type": "deposit_reward", "diff_type": "ZMX缺失", "follow_up": None},
    ]
    _derive_follow_up(rows)
    assert rows[0]["follow_up"] == "建议评估跟进"
    assert rows[1]["follow_up"] == "建议评估跟进"


def test_derive_follow_up_different_mechanism_and_covered():
    rows = [
        {"source": "Bitunix", "mechanism_type": "x", "diff_type": "ZMX玩法不同", "follow_up": None},
        {"source": "Weex", "mechanism_type": "y", "diff_type": "ZMX已有", "follow_up": None},
        {"source": "BingX", "mechanism_type": "z", "diff_type": "不适用", "follow_up": None},
        {"source": "Phemex", "mechanism_type": "z2", "diff_type": "混合", "follow_up": None},
    ]
    _derive_follow_up(rows)
    assert rows[0]["follow_up"] == "建议观察差异"
    assert rows[1]["follow_up"] == "无需关注"
    assert rows[2]["follow_up"] is None
    assert rows[3]["follow_up"] is None


def test_derive_follow_up_does_not_overwrite_existing_value():
    rows = [{"source": "Bitunix", "mechanism_type": "x", "diff_type": "ZMX玩法不同", "follow_up": "already set"}]
    _derive_follow_up(rows)
    assert rows[0]["follow_up"] == "already set"


# ---------------------------------------------------------------- Phase⑤: build_daily_digest ----


def test_build_daily_digest_falls_back_when_no_cache(conn, db_path):
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    conn.commit()
    _insight(conn, source="Bitunix", category="campaign", locale="EN", uids=[uid], articles=[])
    conn.commit()

    digest = build_daily_digest(conn, BATCH_DATE)
    assert digest["source"] == "fallback"
    assert digest["summary"] is None


def test_build_daily_digest_uses_real_cached_llm_response(conn, db_path):
    uid = _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign")
    conn.commit()
    _insight(conn, source="Bitunix", category="campaign", locale="EN", uids=[uid], articles=[])
    conn.commit()

    from src.analysis.daily_digest import load_locale_batches
    batches = load_locale_batches(conn, "ALL", BATCH_DATE)
    cache_key = compute_digest_cache_key(batches)
    set_cached_response(conn, cache_key, json.dumps({
        "daily_summary": "活动侧集中在充值激励。市场变化主要分布在 EN。",
        "campaign_summary": "活动类型以充值激励为主。奖励范围保持稳定。",
        "product_summary": "产品能力集中在交易。整体以功能更新为主。",
        "priority_focus": None,
    }))
    conn.commit()

    digest = build_daily_digest(conn, BATCH_DATE)
    assert digest["source"] == "llm"
    assert digest["daily_summary"] == "活动侧集中在充值激励。市场变化主要分布在 EN。"
    assert digest["summary"] == "活动侧集中在充值激励。市场变化主要分布在 EN。"
    assert digest["campaign_summary"] == "活动类型以充值激励为主。奖励范围保持稳定。"
    assert digest["product_summary"] == "产品能力集中在交易。整体以功能更新为主。"
    assert digest["priority_focus"] is None
