"""zoomex.py 解析器离线单测，基于 tests/fixtures/zoomex_{menu,EN_platform_announcement,
article_detail}.json 真实响应快照。"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.zoomex import get_total_count, parse_detail_response, parse_list_response

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 列表 ----

def test_parse_list_response_extracts_expected_fields_for_matching_lang():
    payload = _load("zoomex_EN_platform_announcement.json")
    items = parse_list_response(payload, "en-US")

    assert len(items) == len(payload["result"]["content"])
    first = items[0]
    assert first["article_id"] == 4116
    assert first["title"].startswith("PURRUSDT")
    assert first["post_time"] == 1783931588556
    assert first["update_time"] == 1783939844802


def test_parse_list_response_lang_not_present_is_skipped_not_crashed():
    payload = _load("zoomex_EN_platform_announcement.json")
    items = parse_list_response(payload, "xx-XX")  # 不存在的 locale
    assert items == []


def test_parse_list_response_missing_result_returns_empty_list():
    assert parse_list_response({}, "en-US") == []
    assert parse_list_response({"result": {}}, "en-US") == []
    assert parse_list_response({"result": {"content": None}}, "en-US") == []


def test_get_total_count():
    payload = _load("zoomex_EN_platform_announcement.json")
    assert get_total_count(payload) == payload["result"]["totalCount"]
    assert get_total_count({}) == 0


# ---------------------------------------------------------------- 详情 ----

def test_parse_detail_response_extracts_content_for_matching_lang():
    payload = _load("zoomex_article_detail.json")
    detail = parse_detail_response(payload, "en-US")

    assert detail["article_id"] == 4116
    assert detail["title"].startswith("PURRUSDT")
    assert detail["content"]  # 原始 Slate JSON 字符串，非空
    assert detail["update_time"] == payload["result"]["article"]["gmtUpdatedAt"]


def test_parse_detail_response_lang_not_present_gives_none_title_and_content():
    payload = _load("zoomex_article_detail.json")
    detail = parse_detail_response(payload, "xx-XX")
    assert detail["title"] is None
    assert detail["content"] is None
    # article 级别字段（不按 lang 拆分）依然能拿到
    assert detail["article_id"] == 4116


def test_parse_detail_response_missing_result_does_not_crash():
    detail = parse_detail_response({}, "en-US")
    assert detail == {
        "article_id": None,
        "title": None,
        "content": None,
        "post_time": None,
        "update_time": None,
    }
