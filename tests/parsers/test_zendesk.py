"""zendesk.py 解析器离线单测，基于 tests/fixtures/{bitunix,weex}_*.json 真实响应快照。

不发任何网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.zendesk import get_next_cursor, parse_articles

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 正常解析 ----

def test_parse_articles_extracts_expected_fields_from_bitunix_fixture():
    payload = _load("bitunix_EN.json")
    items = parse_articles(payload)

    assert len(items) == len(payload["articles"])
    first = items[0]
    assert first["article_id"] == 59923371883417
    assert first["title"] == "Bitunix to Launch SKHYUSDT in Perpetual Futures"
    assert first["post_time"] == "2026-07-11T00:45:26Z"
    assert first["update_time"] == "2026-07-13T00:24:06Z"
    assert first["url"].startswith("https://support.bitunix.com/hc/")
    assert first["content"]  # body 非空 HTML 字符串


def test_parse_articles_extracts_expected_fields_from_weex_fixture():
    payload = _load("weex_EN.json")
    items = parse_articles(payload)

    assert len(items) == len(payload["articles"])
    assert all(item["article_id"] for item in items)
    assert all(item["update_time"] for item in items)


# ---------------------------------------------------------------- 缺字段不崩 ----

def test_parse_articles_missing_articles_key_returns_empty_list():
    assert parse_articles({}) == []
    assert parse_articles({"articles": None}) == []
    assert parse_articles({"articles": "not-a-list"}) == []


def test_parse_articles_skips_non_dict_entries():
    payload = {"articles": [{"id": 1, "title": "ok"}, "not-a-dict", None, 42]}
    items = parse_articles(payload)
    assert len(items) == 1
    assert items[0]["article_id"] == 1


def test_parse_articles_missing_optional_fields_does_not_crash():
    payload = {"articles": [{"id": 42}]}  # 缺 title/body/created_at/updated_at/section_id
    items = parse_articles(payload)
    assert len(items) == 1
    assert items[0] == {
        "article_id": 42,
        "title": None,
        "content": None,
        "post_time": None,
        "update_time": None,
        "section_id": None,
        "url": None,
    }


# ---------------------------------------------------------------- 时间格式 ----

def test_time_fields_are_passed_through_as_utc_iso8601_z_suffix():
    # Bitunix / Weex 源端本来就是 ISO8601 UTC（Z 后缀），按 CLAUDE.md「直接存」，
    # parser 不做任何转换，这里确认没有被意外改写。
    payload = _load("weex_EN.json")
    items = parse_articles(payload)
    for item in items:
        assert item["post_time"].endswith("Z")
        assert item["update_time"].endswith("Z")


# ---------------------------------------------------------------- 分页（cursor，Phase 2.7） ----

def test_get_next_cursor_returns_after_cursor_when_has_more():
    payload = {"meta": {"has_more": True, "after_cursor": "abc123"}}
    assert get_next_cursor(payload) == "abc123"


def test_get_next_cursor_returns_none_when_no_more_results():
    payload = {"meta": {"has_more": False, "after_cursor": "abc123"}}
    assert get_next_cursor(payload) is None


def test_get_next_cursor_missing_meta_returns_none():
    # 经典 offset 分页的旧 fixture（next_page 字段，没有 meta）应该被当成
    # "没有更多结果" 处理——不是 cursor 分页响应形状，不能假装解析出下一页。
    payload = _load("bitunix_EN.json")
    assert "meta" not in payload
    assert get_next_cursor(payload) is None


def test_get_next_cursor_empty_payload_returns_none():
    assert get_next_cursor({}) is None
