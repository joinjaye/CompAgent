"""src/parsers/lbank_events.py 离线单测：用真实抓取的精选活动页面快照
（tests/fixtures/lbank_events_{EN,VN,ID}.html）验证 RSC flight 流提取，不发任何
网络请求。
"""

from __future__ import annotations

from pathlib import Path

from src.parsers.lbank_events import parse_event_list

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


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
