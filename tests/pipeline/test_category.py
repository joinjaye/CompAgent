"""分类打标单测：第一层（raw_category 字典查找）+ 第二层（标题关键词），离线跑，
不发请求、不依赖真实 category_mapping.yaml 内容（用手写的 mapping fixture）。
"""

from __future__ import annotations

import pytest

from src.db.connection import get_connection, init_db
from src.db.operations import upsert_announcement
from src.pipeline.category import apply_layer1_layer2, classify_by_keyword, classify_row, dry_run

MAPPING = {
    "bitunix": {
        "100": "campaign",
        "101": "listing",
        "102": "other",  # 需要看关键词能不能细分
    },
    "weex": {
        "200": "delisting",
    },
    "lbank": None,  # 整源无 per-item raw_category
}


# ---------------------------------------------------------------- classify_by_keyword ----

def test_keyword_priority_listing_before_product():
    # "launch" 命中 product，但 "listing" 应该优先命中（listing 在规则列表里排第一）
    assert classify_by_keyword("New listing: XYZUSDT futures launch") == "listing"


def test_keyword_delisting():
    assert classify_by_keyword("XYZUSDT delisting notice") == "delisting"


def test_keyword_chinese_delisting():
    assert classify_by_keyword("XYZUSDT 下架公告") == "delisting"


def test_keyword_campaign():
    assert classify_by_keyword("Join the trading contest for bonus rewards") == "campaign"


def test_keyword_product():
    assert classify_by_keyword("App update: new feature now supports X") == "product"


def test_keyword_other():
    assert classify_by_keyword("Scheduled system maintenance notice") == "other"


def test_keyword_no_match_returns_none():
    assert classify_by_keyword("Welcome to our platform") is None


def test_keyword_none_title_returns_none():
    assert classify_by_keyword(None) is None


# ---------------------------------------------------------------- classify_row ----

def test_native_layer_resolves_directly_for_non_other():
    result = classify_row("Bitunix", "100", "anything", MAPPING)
    assert result.category == "campaign"
    assert result.layer == "native"


def test_native_other_refined_by_keyword():
    result = classify_row("Bitunix", "102", "System maintenance window", MAPPING)
    assert result.category == "other"
    assert result.layer == "keyword"


def test_native_other_stays_other_without_keyword_hit():
    result = classify_row("Bitunix", "102", "Just a regular headline", MAPPING)
    assert result.category == "other"
    assert result.layer == "native_other"


def test_raw_category_not_in_mapping_flagged_unmapped():
    result = classify_row("Bitunix", "999", "Some new section article", MAPPING)
    assert result.category is None
    assert result.layer == "unmapped_native"


def test_source_with_no_mapping_falls_to_keyword():
    result = classify_row("Lbank", None, "New listing: ABCUSDT", MAPPING)
    assert result.category == "listing"
    assert result.layer == "keyword"


def test_source_with_no_mapping_and_no_keyword_is_llm_pending():
    result = classify_row("Lbank", None, "Welcome to our platform", MAPPING)
    assert result.category is None
    assert result.layer == "llm_pending"


def test_raw_category_none_for_mapped_source_falls_to_keyword():
    result = classify_row("Bitunix", None, "New listing: ABCUSDT", MAPPING)
    assert result.category == "listing"
    assert result.layer == "keyword"


# ---------------------------------------------------------------- dry_run / apply (DB) ----

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _seed(conn, source, locale, article_id, title, raw_category):
    upsert_announcement(
        conn,
        source=source,
        locale=locale,
        article_id=article_id,
        title=title,
        content="content body",
        raw_category=raw_category,
    )


def test_dry_run_counts_layers_without_writing(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "any title", "100")  # native
        _seed(conn, "Bitunix", "EN", "2", "System maintenance", "102")  # keyword (refines other)
        _seed(conn, "Bitunix", "EN", "3", "Just a headline", "102")  # native_other
        _seed(conn, "Bitunix", "EN", "4", "Some article", "999")  # unmapped_native
        _seed(conn, "Bitunix", "EN", "5", "Random headline, nothing special", None)  # llm_pending

        report = dry_run(conn, MAPPING, sources=("Bitunix",))
        assert report.total == 5
        assert report.layer_counts["native"] == 1
        assert report.layer_counts["keyword"] == 1
        assert report.layer_counts["native_other"] == 1
        assert report.layer_counts["unmapped_native"] == 1
        assert report.layer_counts["llm_pending"] == 1

        # dry_run 只读，不应该改库
        rows = conn.execute("SELECT category FROM announcements").fetchall()
        assert all(r[0] is None for r in rows)


def test_apply_writes_only_resolved_categories(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, "Bitunix", "EN", "1", "any title", "100")  # native -> campaign
        _seed(conn, "Bitunix", "EN", "4", "Some article", "999")  # unmapped_native -> stays NULL
        _seed(conn, "Bitunix", "EN", "5", "Random headline, nothing special", None)  # llm_pending -> stays NULL

        counts = apply_layer1_layer2(conn, MAPPING, sources=("Bitunix",))
        conn.commit()
        assert counts["_written"] == 1

        rows = {r["article_id"]: r["category"] for r in conn.execute("SELECT article_id, category FROM announcements")}
        assert rows["1"] == "campaign"
        assert rows["4"] is None
        assert rows["5"] is None
