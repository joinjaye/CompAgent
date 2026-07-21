"""src/parsers/lbank_events.py 离线单测：用真实抓取的精选活动页面快照
（tests/fixtures/lbank_events_{EN,VN,ID}.html）验证 RSC flight 流提取，以及用
真实抓取的 `atlasActivity/loadingPage` 响应（tests/fixtures/
lbank_events_loadingpage_EN.json）验证详情正文两跳解析，不发任何网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.lbank_events import parse_activity_detail, parse_event_list, resolve_rule_content_url

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _read_json_fixture(name: str) -> dict:
    return json.loads(_read_fixture(name))


def test_parse_event_list_en():
    html = _read_fixture("lbank_events_EN.html")
    items = parse_event_list(html)

    assert len(items) == 8
    first = items[0]
    assert first["id"] == 10002315
    assert first["code"] == "10002315-tendies-brian-listing"
    assert first["title"] == "TENDIES, BRIAN Listing Carnival"
    assert first["start_time_ms"] == 1784289600000
    assert first["end_time_ms"] == 1784548800000
    assert first["route_url"].startswith("https://www.lbank.com/en-US/")


def test_parse_event_list_vn_translated_titles():
    html = _read_fixture("lbank_events_VN.html")
    items = parse_event_list(html)

    assert len(items) == 8
    first = items[0]
    assert first["id"] == 10002315
    assert first["title"] != "TENDIES, BRIAN Listing Carnival"  # 真实越南语译文


def test_parse_event_list_id_translated_titles():
    html = _read_fixture("lbank_events_ID.html")
    items = parse_event_list(html)

    assert len(items) == 8
    first = items[0]
    assert first["id"] == 10002315
    assert first["title"] != "TENDIES, BRIAN Listing Carnival"  # 真实印尼语译文


def test_parse_event_list_ids_consistent_across_locales():
    en_ids = {i["id"] for i in parse_event_list(_read_fixture("lbank_events_EN.html"))}
    vn_ids = {i["id"] for i in parse_event_list(_read_fixture("lbank_events_VN.html"))}
    id_ids = {i["id"] for i in parse_event_list(_read_fixture("lbank_events_ID.html"))}
    assert en_ids == vn_ids == id_ids


def test_parse_event_list_returns_empty_on_garbage_html():
    assert parse_event_list("<html><body>nothing here</body></html>") == []


def test_resolve_rule_content_url_from_absolute_url():
    url = resolve_rule_content_url("https://jiz.lbk.world/content/260717/9La_0717195148141.stxt")
    assert url == "https://www.lbank.com/static-backend-doc/content/260717/9La_0717195148141.stxt"


def test_resolve_rule_content_url_from_relative_path():
    url = resolve_rule_content_url("/static-backend-doc/content/260717/9La_0717195148141.stxt")
    assert url == "https://www.lbank.com/static-backend-doc/content/260717/9La_0717195148141.stxt"


def test_resolve_rule_content_url_missing_returns_none():
    assert resolve_rule_content_url(None) is None
    assert resolve_rule_content_url("") is None
    assert resolve_rule_content_url("https://example.com/no-content-segment.stxt") is None


def test_parse_activity_detail_real_fixture():
    payload = _read_json_fixture("lbank_events_loadingpage_EN.json")
    detail = parse_activity_detail(payload)

    assert detail is not None
    assert detail["title"] == "TENDIES, BRIAN Listing Carnival"
    assert detail["sub_title"] == "Share $10,000 Rewards"
    assert detail["rule_content"] is None  # 真实响应里 content 字段是 null
    assert detail["rule_content_url"] == "https://www.lbank.com/static-backend-doc/content/260717/9La_0717195148141.stxt"


def test_parse_activity_detail_uses_inline_content_when_present():
    payload = {
        "code": 200,
        "data": {
            "headInfo": {"titleInfo": {"title": "t", "subTitle": "s"}},
            "ruleInfo": {"content": "<p>inline rule text</p>", "contentId": "https://x/content/y.stxt"},
        },
    }
    detail = parse_activity_detail(payload)

    assert detail["rule_content"] == "<p>inline rule text</p>"
    assert detail["rule_content_url"] is None  # content 已有值时不需要再解析 contentId


def test_parse_activity_detail_returns_none_on_non_200_code():
    assert parse_activity_detail({"code": 500, "data": {}}) is None
    assert parse_activity_detail({}) is None
    assert parse_activity_detail({"code": 200, "data": None}) is None
