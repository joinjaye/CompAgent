"""src/parsers/lbank.py 离线单测：用真实抓取的 JSON API 响应快照
（tests/fixtures/lbank_api_*.json，2026-07-14 抓取，见 CLAUDE.md「Lbank 真实
API 重写」）验证列表/详情解析，不发任何网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.lbank import get_total_count, parse_list_response

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_parse_list_response_extracts_items():
    payload = _read_fixture("lbank_api_latestlist_new_listings_en.json")
    items = parse_list_response(payload)
    assert len(items) == 5
    first = items[0]
    assert first["notice_id"]
    assert first["code"]
    assert first["title"]
    assert first["content"]
    assert first["post_time_ms"]


def test_parse_list_response_vn_locale():
    payload = _read_fixture("lbank_api_latestlist_new_listings_vn.json")
    items = parse_list_response(payload)
    assert len(items) == 5
    assert all(item["title"] for item in items)


def test_get_total_count():
    payload = _read_fixture("lbank_api_latestlist_new_listings_en.json")
    total = get_total_count(payload)
    assert total is not None and total > 100


def test_parse_list_response_returns_empty_on_malformed_payload():
    assert parse_list_response({}) == []
    assert parse_list_response({"data": {}}) == []


def test_get_total_count_none_on_malformed_payload():
    assert get_total_count({}) is None
