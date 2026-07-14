"""src/parsers/phemex_web.py 离线单测：验证宽松 JS 对象字面量解析（详情页
`window.preloadedData`，用 Phase 1 侦察阶段真实抓取的 fixture）+ 真实分页 API
响应解析（`parse_query_response`，用 2026-07-14 抓取的 fixture，见 CLAUDE.md
「Phemex 分页升级」），不发任何网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.parsers.phemex_web import (
    _JsLiteralParseError,
    _JsLiteralParser,
    parse_article_detail,
    parse_query_response,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_html_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _read_json_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ------------------------------------------------------- _JsLiteralParser ----

def test_js_literal_parser_handles_basic_types():
    result = _JsLiteralParser("{a:1,b:'x',c:null,d:true,e:false,f:[1,2,3]}").parse()
    assert result == {"a": 1, "b": "x", "c": None, "d": True, "e": False, "f": [1, 2, 3]}


def test_js_literal_parser_handles_nested_objects_and_trailing_comma():
    result = _JsLiteralParser("{a:{b:'y',},}").parse()
    assert result == {"a": {"b": "y"}}


def test_js_literal_parser_handles_escaped_quote_in_string():
    result = _JsLiteralParser(r"{title:'It\'s live'}").parse()
    assert result == {"title": "It's live"}


def test_js_literal_parser_handles_float_numbers():
    result = _JsLiteralParser("{x:1.5,y:-2}").parse()
    assert result == {"x": 1.5, "y": -2}


def test_js_literal_parser_raises_on_malformed_input():
    with pytest.raises(_JsLiteralParseError):
        _JsLiteralParser("{a:").parse()


# ------------------------------------------------------ parse_article_detail ----

def test_parse_article_detail_extracts_content_and_updated_at():
    html = _read_html_fixture("phemex_EN_detail.html")
    detail = parse_article_detail(html)
    assert detail is not None
    assert detail["title"]
    assert "<p" in detail["content"]
    assert detail["published_time"]
    assert detail["updated_at"]


def test_parse_article_detail_none_on_garbage_html():
    assert parse_article_detail("<html><body>nothing here</body></html>") is None


# ------------------------------------------------------ parse_query_response ----

def test_parse_query_response_extracts_items_and_total():
    payload = _read_json_fixture("phemex_api_query_news_en.json")
    items, total = parse_query_response(payload)
    assert len(items) == 5
    assert total is not None and total > 100
    first = items[0]
    assert first["article_id"]
    assert first["title"]
    assert first["url"].startswith("/announcements/")
    assert first["published_time_ms"]


def test_parse_query_response_fr_locale():
    payload = _read_json_fixture("phemex_api_query_news_fr.json")
    items, total = parse_query_response(payload)
    assert len(items) == 5
    assert all(item["title"] for item in items)


def test_parse_query_response_returns_empty_on_malformed_payload():
    items, total = parse_query_response({})
    assert items == []
    assert total is None
    items2, total2 = parse_query_response({"data": {}})
    assert items2 == []
    assert total2 is None
