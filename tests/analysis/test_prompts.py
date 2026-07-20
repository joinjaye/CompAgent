"""src/analysis/prompts.py 单测：占位符替换只认 ALL_CAPS 变量、不受信任的正文内容
（可能包含花括号或看起来像占位符的文本）不会破坏模板、只有 campaign/product 构建、
ZMX_NOTE 的两种状态（有基线/零基线——结构化基线注入后不再有"命中数有限、置信度
较低"这个中间档位，见 build_zmx_note 的 docstring）。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.analysis.prompts import build_prompt, build_zmx_note, render
from src.analysis.zmx_baseline import ZmxBaselineEntry


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


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE announcements (uid TEXT, title TEXT, status TEXT, content TEXT)")
    yield c
    c.close()


def _rows(conn, entries):
    for uid, title, status, content in entries:
        conn.execute("INSERT INTO announcements VALUES (?,?,?,?)", (uid, title, status, content))
    conn.commit()
    return conn.execute("SELECT * FROM announcements").fetchall()


@pytest.mark.parametrize("category", ["campaign", "product"])
def test_build_prompt_all_categories_produce_nonempty_prompts(conn, category):
    rows = _rows(conn, [("u1", "Title A", "new", "content A")])
    result = build_prompt(
        category, source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={}, zmx_hits=[],
    )
    assert result.system
    assert "Bitunix" in result.user
    assert "2026-07-14" in result.user
    assert "u1" in result.user


@pytest.mark.parametrize(
    "category,expect_present,expect_absent",
    [
        ("campaign", ['"change_kind"'], ['"listing_kind"']),
        ("product", [], ['"change_kind"', '"listing_kind"']),
    ],
)
def test_build_prompt_per_article_fields_only_where_relevant(conn, category, expect_present, expect_absent):
    # 用带引号的 JSON key 形式（如 '"change_kind"'）而不是裸子串，因为不产出某字段的
    # category 模板里，强制规则文案会提到该字段名本身（"不产出 change_kind 字段"），
    # 裸子串检测会被这句说明文字误判成"存在"。
    rows = _rows(conn, [("u1", "Title A", "new", "content A")])
    result = build_prompt(
        category, source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={}, zmx_hits=[],
    )
    for field in expect_present:
        assert field in result.user, f"{field} should appear in {category} prompt"
    for field in expect_absent:
        assert field not in result.user, f"{field} should NOT appear in {category} prompt"


def test_build_prompt_includes_old_content_only_for_changed(conn):
    rows = _rows(conn, [
        ("u1", "New article", "new", "new content"),
        ("u2", "Changed article", "changed", "new content v2"),
    ])
    result = build_prompt(
        "campaign", source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={"u2": "old content v1"}, zmx_hits=[],
    )
    assert "diff(-before/+after)=" in result.user
    assert "- old content v1" in result.user
    assert "+ new content v2" in result.user
    assert "变更前正文" not in result.user


def test_build_prompt_rejects_unknown_category(conn):
    rows = _rows(conn, [("u1", "T", "new", "C")])
    with pytest.raises(ValueError):
        build_prompt("unknown", source="Bitunix", locale="EN", batch_date="2026-07-14",
                      rows=rows, old_content_by_uid={})


@pytest.mark.parametrize("category", ["listing", "delisting"])
def test_build_prompt_rejects_categories_that_do_not_use_llm(conn, category):
    rows = _rows(conn, [("u1", "T", "new", "C")])
    with pytest.raises(ValueError, match="不使用 LLM"):
        build_prompt(
            category, source="Bitunix", locale="EN", batch_date="2026-07-14",
            rows=rows, old_content_by_uid={},
        )


def test_zmx_note_zero_hits(conn):
    note = build_zmx_note("campaign", 0)
    assert "不适用" in note


def test_zmx_note_nonzero_hits():
    assert build_zmx_note("campaign", 5) == ""


def test_build_prompt_untrusted_content_with_braces_does_not_break_template(conn):
    rows = _rows(conn, [("u1", "Title with {SOURCE} literal braces", "new", 'content { "fake": "json" } {LOCALE}')])
    result = build_prompt(
        "campaign", source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={}, zmx_hits=[],
    )
    assert "Title with {SOURCE} literal braces" in result.user
    assert 'content { "fake": "json" } {LOCALE}' in result.user


def test_build_prompt_zmx_block_rendered_with_hits(conn):
    rows = _rows(conn, [("u1", "T", "new", "C")])
    hits = [
        ZmxBaselineEntry(
            uid="z1", title="ZMX Title", mechanism_type="入金活动",
            key_mechanics="充值满 100 USDT 送体验金", reward_range="5-50 USDT",
            target_users="新注册用户", start_date="2026-06-01", end_date="2026-06-30",
            post_time="2026-06-01T00:00:00Z",
        )
    ]
    result = build_prompt(
        "campaign", source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={}, zmx_hits=hits,
    )
    assert "[Z1] 类型：入金活动" in result.user
    assert "UID: z1" in result.user
    assert "ZMX Title" in result.user
    assert "5-50 USDT" in result.user
