"""src/analysis/prompts.py 单测：占位符替换只认 ALL_CAPS 变量、不受信任的正文内容
（可能包含花括号或看起来像占位符的文本）不会破坏模板、四个 category 都能正确构建、
ZMX_NOTE 的三种状态（充分/有限/零命中）。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.analysis.prompts import build_prompt, build_zmx_note, render
from src.analysis.zmx_index import ZmxArticle


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


@pytest.mark.parametrize("category", ["campaign", "product", "listing", "delisting"])
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


def test_build_prompt_includes_old_content_only_for_changed(conn):
    rows = _rows(conn, [
        ("u1", "New article", "new", "new content"),
        ("u2", "Changed article", "changed", "new content v2"),
    ])
    result = build_prompt(
        "campaign", source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={"u2": "old content v1"}, zmx_hits=[],
    )
    assert "变更前正文：old content v1" in result.user
    # 只有一条真正带旧正文的数据行；campaign 模板本身的格式说明文案里也提到了
    # "变更前正文" 一次，所以总出现次数是数据 1 次 + 说明文案 1 次。
    assert result.user.count("变更前正文") == 2


def test_build_prompt_rejects_unknown_category(conn):
    rows = _rows(conn, [("u1", "T", "new", "C")])
    with pytest.raises(ValueError):
        build_prompt("unknown", source="Bitunix", locale="EN", batch_date="2026-07-14",
                      rows=rows, old_content_by_uid={})


def test_build_prompt_delisting_has_no_zmx_block(conn):
    rows = _rows(conn, [("u1", "T", "new", "C")])
    result = build_prompt(
        "delisting", source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={},
    )
    assert "Zoomex" not in result.user


def test_zmx_note_zero_hits(conn):
    note = build_zmx_note("campaign", 0, 3)
    assert "不适用" in note


def test_zmx_note_limited_hits():
    note = build_zmx_note("campaign", 2, 3)
    assert "基线数据有限" in note
    assert "2" in note


def test_zmx_note_sufficient_hits():
    assert build_zmx_note("campaign", 5, 3) == ""


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
    hits = [ZmxArticle(uid="z1", title="ZMX Title", content_preview="preview text", post_time="2026-06-01T00:00:00Z", similarity_score=0.9)]
    result = build_prompt(
        "campaign", source="Bitunix", locale="EN", batch_date="2026-07-14",
        rows=rows, old_content_by_uid={}, zmx_hits=hits,
    )
    assert "[Z1] UID: z1" in result.user
    assert "ZMX Title" in result.user
