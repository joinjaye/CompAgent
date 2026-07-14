"""Bitunix 采集器测试（mock HTTP 层，不发真实请求，基于 tests/fixtures 快照）。

Weex 从 2026-07-14 起不再走这套 ZendeskCollector 实现（改为解析 www.weex.com 前台
页面，见 src/collectors/weex.py 顶部注释 + CLAUDE.md「Weex 数据源迁移」），Weex 专属
测试已迁到 tests/collectors/test_weex_collector.py；本文件只保留 Bitunix（仍然跑在
Zendesk 上）的测试，以及跟具体交易所无关的 ZendeskCollector 通用行为测试。

覆盖：
- normalize() 字段映射、group_id 拼接、article_id 转 str
- 幂等：同一份数据连跑两次，watermark 会挡住已处理过的条目，第二轮 0 new/changed
- 变更检测：手动 tamper content_hash 后，用 --force-full 语义（force_full=True）
  重新全量校验，能正确识别为 changed（watermark 模式下未真正变更的旧条目本来就不会
  被自然轮询重新拉取，见 src/collectors/base.py run() 的 force_full 说明）
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.collectors.base import RawItem
from src.collectors.bitunix import BitunixCollector
from src.db.connection import get_connection, init_db
from src.db.operations import compute_uid

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

BASE_CFG = {
    "pagination": {"type": "cursor", "page_size": 100},
    "rate_limit_ms": 0,
    "detail_mode": "inline",
    "strategy": "watermark",
    "headers": {},
}

BITUNIX_CFG = {
    **BASE_CFG,
    "endpoint": "https://support.bitunix.com/api/v2/help_center/en-us/categories/13760946490649/articles.json",
}


def _load_single_page_fixture(name: str) -> dict:
    """加载 fixture 并把 next_page 清空，测试只用一页，不需要为第二页也造 mock。"""
    payload = copy.deepcopy(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    payload["next_page"] = None
    return payload


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


# ---------------------------------------------------------------- normalize ----

def test_bitunix_normalize_maps_fields_and_builds_group_id():
    collector = BitunixCollector("EN", BITUNIX_CFG)
    raw = RawItem(
        article_id=59923371883417,
        title="Bitunix to Launch SKHYUSDT",
        content="<p>body</p>",
        post_time="2026-07-11T00:45:26Z",
        update_time="2026-07-13T00:24:06Z",
        url="https://support.bitunix.com/hc/en-us/articles/59923371883417",
        category_raw=13762037166105,
    )

    ann = collector.normalize(raw)

    assert ann.source == "Bitunix"
    assert ann.locale == "EN"
    assert ann.article_id == "59923371883417"  # 转成 str
    assert ann.group_id == "bitunix_59923371883417"
    assert ann.category is None  # Phase 3 之前不分类
    assert ann.raw_category == "13762037166105"  # section_id 原样保留，转成字符串
    assert ann.source_endpoint == BITUNIX_CFG["endpoint"]
    assert ann.post_time == "2026-07-11T00:45:26Z"
    assert ann.update_time == "2026-07-13T00:24:06Z"


def test_bitunix_normalize_cleans_html_content_to_plain_text():
    collector = BitunixCollector("EN", BITUNIX_CFG)
    raw = RawItem(article_id=1, content="<div>hello <strong>world</strong></div>")

    ann = collector.normalize(raw)

    assert ann.content == "hello world"
    assert "<" not in ann.content


def test_bitunix_normalize_raw_category_none_when_category_raw_missing():
    collector = BitunixCollector("EN", BITUNIX_CFG)
    raw = RawItem(article_id=1, content="c")

    ann = collector.normalize(raw)

    assert ann.raw_category is None


# ---------------------------------------------------------------- 多分类（Phase 2.7） ----

def test_zendesk_collector_defaults_to_empty_category_for_backward_compat():
    """不传 category_key（Bitunix 单分类源、以及批次 1 时代的调用方式）时
    crawl_state.category 恒为 ''，跟批次 1 的行为完全一致。"""
    collector = BitunixCollector("EN", BITUNIX_CFG)
    assert collector.category == ""


def test_zendesk_collector_accepts_explicit_category_key():
    collector = BitunixCollector("EN", BITUNIX_CFG, category_key="some_category")
    assert collector.category == "some_category"


# ---------------------------------------------------------------- 幂等 ----

def test_bitunix_first_run_inserts_all_then_second_run_is_idempotent(db_path, monkeypatch):
    payload = _load_single_page_fixture("bitunix_EN.json")
    monkeypatch.setattr("src.collectors.zendesk_base.fetch_json", lambda url, **kw: payload)

    collector = BitunixCollector("EN", BITUNIX_CFG)

    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.new == len(payload["articles"])
    assert first.changed == 0
    assert first.failed == 0

    with get_connection(db_path) as conn:
        row_count_before = conn.execute("SELECT COUNT(*) c FROM announcements").fetchone()["c"]

    # 第二轮：watermark 已推进到上一轮最大 update_time，源端数据未变，
    # 应该 0 new / 0 changed（要么因为水位线挡住不再重取，要么重取后 hash 相同）。
    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.failed == 0

    with get_connection(db_path) as conn:
        row_count_after = conn.execute("SELECT COUNT(*) c FROM announcements").fetchone()["c"]
    assert row_count_after == row_count_before  # 未产生重复行


def test_bitunix_run_persists_plain_text_content_and_raw_category(db_path, monkeypatch):
    payload = _load_single_page_fixture("bitunix_EN.json")
    monkeypatch.setattr("src.collectors.zendesk_base.fetch_json", lambda url, **kw: payload)

    collector = BitunixCollector("EN", BITUNIX_CFG)
    with get_connection(db_path) as conn:
        collector.run(conn)

    first_article = payload["articles"][0]
    uid = compute_uid("Bitunix", "EN", str(first_article["id"]))
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT content, raw_category FROM announcements WHERE uid = ?", (uid,)
        ).fetchone()
    assert "<" not in row["content"]
    assert row["raw_category"] == str(first_article["section_id"])


# ---------------------------------------------------------------- 变更检测 ----

def test_force_full_rerun_detects_manually_tampered_content_hash(db_path, monkeypatch):
    payload = _load_single_page_fixture("bitunix_EN.json")
    monkeypatch.setattr("src.collectors.zendesk_base.fetch_json", lambda url, **kw: payload)

    collector = BitunixCollector("EN", BITUNIX_CFG)

    with get_connection(db_path) as conn:
        collector.run(conn)

    tampered_article = payload["articles"][0]
    uid = compute_uid("Bitunix", "EN", str(tampered_article["id"]))
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE announcements SET content_hash = 'tampered-hash' WHERE uid = ?", (uid,)
        )

    # 强制全量重跑（force_full=True），忽略 watermark，重新拉取并比对全部条目。
    with get_connection(db_path) as conn:
        second = collector.run(conn, force_full=True)

    assert second.changed == 1
    assert second.unchanged == len(payload["articles"]) - 1
    assert second.new == 0

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT content_hash, status FROM announcements WHERE uid = ?", (uid,)
        ).fetchone()
    assert row["status"] == "changed"
    assert row["content_hash"] != "tampered-hash"


# ---------------------------------------------------------------- cursor 分页（Phase 2.7） ----

def test_fetch_list_follows_cursor_across_multiple_pages(db_path, monkeypatch):
    """经典 offset 分页在 Weex listings_delistings（3199 条）上因为 Zendesk 的
    page=100 硬限制而 400，改成 cursor 分页后需要验证多页真的会被翻完、且不依赖
    响应里那个有 bug 的 links.next（见 zendesk_base.py 顶部注释）。"""
    page1 = {
        "articles": [
            {"id": 1, "title": "a", "body": "<p>a</p>", "created_at": "2026-01-01T00:00:00Z",
             "updated_at": "2026-01-02T00:00:00Z", "section_id": 111, "html_url": "https://x/1"},
        ],
        "meta": {"has_more": True, "after_cursor": "CURSOR_1"},
    }
    page2 = {
        "articles": [
            {"id": 2, "title": "b", "body": "<p>b</p>", "created_at": "2026-01-01T00:00:00Z",
             "updated_at": "2026-01-03T00:00:00Z", "section_id": 111, "html_url": "https://x/2"},
        ],
        "meta": {"has_more": False, "after_cursor": None},
    }
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        return page1 if "page%5Bafter%5D" not in url else page2

    monkeypatch.setattr("src.collectors.zendesk_base.fetch_json", fake_fetch)

    collector = BitunixCollector("EN", BITUNIX_CFG)
    items = collector.fetch_list(since=None)

    assert len(calls) == 2
    assert "page%5Bsize%5D=100" in calls[0]
    assert "page%5Bafter%5D=CURSOR_1" in calls[1]  # 自己拼的 URL，不是抄 links.next
    assert sorted(int(i.article_id) for i in items) == [1, 2]


def test_fetch_list_stops_at_watermark_without_requesting_further_pages(db_path, monkeypatch):
    page1 = {
        "articles": [
            {"id": 1, "title": "new", "body": "c", "created_at": "2026-01-01T00:00:00Z",
             "updated_at": "2026-01-05T00:00:00Z", "section_id": 111, "html_url": "https://x/1"},
            {"id": 2, "title": "old", "body": "c", "created_at": "2026-01-01T00:00:00Z",
             "updated_at": "2026-01-01T00:00:00Z", "section_id": 111, "html_url": "https://x/2"},
        ],
        "meta": {"has_more": True, "after_cursor": "CURSOR_1"},
    }
    calls = []

    def fake_fetch(url, **kw):
        calls.append(url)
        return page1

    monkeypatch.setattr("src.collectors.zendesk_base.fetch_json", fake_fetch)

    collector = BitunixCollector("EN", BITUNIX_CFG)
    items = collector.fetch_list(since="2026-01-02T00:00:00Z")

    assert len(calls) == 1  # 遇到 update_time <= since 立刻停止，不该再翻页
    assert [i.article_id for i in items] == [1]
