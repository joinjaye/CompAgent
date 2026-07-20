"""src/dashboard/export_data.py 单测：category-first schema 的 8 个顶层 key 都在、
search_index 字段窄且不含正文、老批次（缺 5 个新字段）优雅降级不报错、delisting 行
在 listing 段里正确带 category 标签、Listing/Delisting 不暴露旧 LLM 分析。

全部离线：真实 schema.sql 建的临时库 + upsert_announcement/upsert_insight 直接写行，
不发真实请求、不调用真实 LLM。
"""

from __future__ import annotations

import json

import pytest

from src.analysis.run import upsert_insight
from src.dashboard.export_data import build_dashboard_data
from src.db.connection import get_connection
from src.db.operations import upsert_announcement

BATCH_DATE = "2026-07-15"


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
    conn.execute("UPDATE announcements SET category = ?, status = ? WHERE uid = ?",
                 (category, status_hint, result.uid))
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
    for key in ["meta", "overview", "trend", "campaign", "product", "listing", "markets", "search_index"]:
        assert key in data, key


def test_search_index_rows_have_only_specified_fields_no_content_leak(conn, db_path):
    _insert(conn, source="Bitunix", locale="EN", article_id="1", category="campaign", content="secret body text")
    conn.commit()
    data = build_dashboard_data(str(db_path))
    rows = data["search_index"]["rows"]
    assert len(rows) == 1
    expected_keys = {"uid", "source", "locale", "category", "title", "post_time", "status", "diff_type", "priority", "url"}
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
    assert chip["diff_breakdown"]["same"] == 1


def test_overview_highlights_only_priority_high_sorted_by_diff_severity(conn, db_path):
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
    # 只有 priority=高 的两条进入 highlights，低优先级那条被排除
    assert len(highlights) == 2
    # ZMX缺失 排在 ZMX已有 前面（diff_type 严重度排序）
    assert highlights[0]["diff_tag"] == "missing"
    assert highlights[1]["diff_tag"] == "same"
