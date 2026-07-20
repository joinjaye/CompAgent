"""WeexRewardsCollector 测试。mock HTTP 层，不发真实请求；列表/详情解析用真实
抓取的 fixture（tests/fixtures/weex_rewards_EN*.html）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.weex_rewards import WeexRewardsCollector
from src.db.connection import get_connection, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "endpoint": "https://www.weex.com/rewards",
    "locale_path": "",
    "pagination": {"type": "none"},
    "rate_limit_ms": 0,
    "strategy": "full_scan",
}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _list_html() -> str:
    return (FIXTURES / "weex_rewards_EN.html").read_text(encoding="utf-8")


def _detail_html() -> str:
    return (FIXTURES / "weex_rewards_EN_detail.html").read_text(encoding="utf-8")


def test_fetch_list_maps_fields_and_resolves_detail_url(monkeypatch):
    monkeypatch.setattr("src.collectors.weex_rewards.http_fetch", lambda url: _list_html())

    collector = WeexRewardsCollector("EN", CFG)
    items = collector.fetch_list(since=None)

    assert len(items) == 18
    promo_item = next(i for i in items if i.article_id == "reward-11093")  # tradfi, activityType=7
    assert promo_item.url == "https://www.weex.com/events/promo/tradfi"

    roll_item = next(i for i in items if i.article_id == "reward-10763")  # weex-football-carnival, type=23
    assert roll_item.url == "https://www.weex.com/events/roll/weex-football-carnival"


def test_fetch_list_returns_empty_on_garbage_html(monkeypatch):
    monkeypatch.setattr("src.collectors.weex_rewards.http_fetch", lambda url: "<html></html>")

    collector = WeexRewardsCollector("EN", CFG)
    assert collector.fetch_list(since=None) == []


def test_fetch_detail_appends_agent_share_content_and_matching_locale_tasks(monkeypatch):
    monkeypatch.setattr("src.collectors.weex_rewards.http_fetch", lambda url: _detail_html())

    collector = WeexRewardsCollector("EN", CFG)
    raw = RawItem(article_id="reward-11093", title="t", content="short teaser", url="https://www.weex.com/events/promo/tradfi")
    result = collector.fetch_detail(raw)

    assert "TradFi Trading Challenge" in result.content  # agentShareContent
    assert "<p>" in result.content  # en_US task intro，未转纯文本（normalize 才转）


def test_fetch_detail_fr_locale_picks_fr_fr_tasks(monkeypatch):
    monkeypatch.setattr("src.collectors.weex_rewards.http_fetch", lambda url: _detail_html())

    collector = WeexRewardsCollector("FR", {**CFG, "locale_path": "fr/"})
    raw = RawItem(article_id="reward-11093", title="t", content="short teaser", url="https://www.weex.com/fr/events/promo/tradfi")
    result = collector.fetch_detail(raw)

    assert "Remplissez les conditions" in result.content  # fr_FR task intro


def test_fetch_detail_no_url_returns_item_unchanged():
    collector = WeexRewardsCollector("EN", CFG)
    raw = RawItem(article_id="reward-1", title="t", content="teaser", url=None)

    result = collector.fetch_detail(raw)

    assert result.content == "teaser"


def test_normalize_sets_literal_raw_category_and_prefixed_group_id():
    collector = WeexRewardsCollector("EN", CFG)
    raw = RawItem(
        article_id="reward-11093", title="t", content="<p>hello</p>",
        post_time="2026-07-14T00:00:00Z", url="https://www.weex.com/events/promo/tradfi",
        extra={"end_time_ms": "1784822399000"},
    )

    ann = collector.normalize(raw)

    assert ann.source == "Weex"
    assert ann.raw_category == "rewards"
    assert ann.group_id == "weex_reward-11093"
    assert "hello" in ann.content
    assert "活动周期" in ann.content
    assert ann.update_time is None


def test_run_end_to_end_idempotent(db_path, monkeypatch):
    def fake_fetch(url):
        return _list_html() if url == CFG["endpoint"] else _detail_html()

    monkeypatch.setattr("src.collectors.weex_rewards.http_fetch", fake_fetch)

    collector = WeexRewardsCollector("EN", CFG)

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.failed == 0
    assert first.total == 18

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.unchanged == 18
