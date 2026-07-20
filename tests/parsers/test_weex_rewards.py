"""src/parsers/weex_rewards.py 离线单测：用真实抓取的活动奖励落地页 + 详情页快照
（tests/fixtures/weex_rewards_*.html）验证 __NEXT_DATA__ 解析，不发任何网络请求。
"""

from __future__ import annotations

from pathlib import Path

from src.parsers.weex_rewards import parse_reward_detail, parse_reward_list, resolve_detail_path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_reward_list_en():
    html = _read_fixture("weex_rewards_EN.html")
    items = parse_reward_list(html)

    assert len(items) == 18
    roll_item = next(i for i in items if i["slug"] == "weex-football-carnival")
    assert roll_item["activity_id"] == 10763
    assert roll_item["activity_type"] == 23
    # 源端字段实测是字符串形式的毫秒时间戳，不是 int（ms_to_iso 在 collector 层
    # 会做 int() 转换，parser 这里只负责如实透传，不提前转换）。
    assert roll_item["start_time_ms"] == "1781107200000"
    assert roll_item["end_time_ms"] == "1784563199000"


def test_parse_reward_list_fr_translated():
    html = _read_fixture("weex_rewards_FR.html")
    items = parse_reward_list(html)

    assert len(items) == 11
    roll_item = next(i for i in items if i["slug"] == "weex-football-carnival")
    assert roll_item["title"] != "WEEX Cup: Dice Rush"  # 真实法语译文


def test_parse_reward_list_returns_empty_on_garbage_html():
    assert parse_reward_list("<html><body>nothing</body></html>") == []


def test_resolve_detail_path_known_types():
    assert resolve_detail_path(7) == "promo"
    assert resolve_detail_path(23) == "roll"
    assert resolve_detail_path(5) == "draw"
    assert resolve_detail_path("7") == "promo"  # 真实数据里可能是 int 或字符串


def test_resolve_detail_path_unknown_type_returns_none():
    assert resolve_detail_path(999) is None
    assert resolve_detail_path(None) is None


def test_parse_reward_detail_en():
    html = _read_fixture("weex_rewards_EN_detail.html")
    detail = parse_reward_detail(html)

    assert detail is not None
    assert "TradFi" in detail["agent_share_content"]
    assert len(detail["tasks"]) == 3
    first_task = detail["tasks"][0]
    assert "en_US" in first_task
    assert "<p>" in first_task["en_US"]


def test_parse_reward_detail_contains_all_locales_regardless_of_url_prefix():
    # 真实核对过：详情页不管走哪个 locale 前缀请求，introI18 都会返回全部语言，
    # 不是只返回当前 URL locale 那一种。
    html = _read_fixture("weex_rewards_FR_detail.html")
    detail = parse_reward_detail(html)

    assert detail is not None
    first_task = detail["tasks"][0]
    assert "en_US" in first_task
    assert "fr_FR" in first_task
    assert first_task["en_US"] != first_task["fr_FR"]


def test_parse_reward_detail_returns_none_on_garbage_html():
    assert parse_reward_detail("<html><body>nothing</body></html>") is None
