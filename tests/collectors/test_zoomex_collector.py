"""ZoomexCollector 测试（mock HTTP 层，不发真实请求）。

Zoomex 跟 Bitunix/Weex 不一样的地方，是这套测试要覆盖到的重点：
- fetch_list() 不依赖排序提前退出（见 src/collectors/zoomex.py 顶部关于「实测列表不是
  按 gmtUpdatedAt 排序」的说明）；force_full=False（daily 增量）时翻页数有上限
  （pagination.max_pages），force_full=True（建仓/全量核查）时翻完全部页
- needs_detail() 用 DB 里已存的 update_time 做增量判断，只有新增/变更的条目才会
  真正发一次详情请求（用调用计数断言，而不是只看最终落库结果）
- force_full=True 会跳过 needs_detail 的增量判断，重新请求全部详情——用于验证
  「手动 tamper content_hash 后能被识别为 changed」（正常增量运行下不会重新校验
  update_time 没变的旧条目，这是设计使然，不是 bug，跟 Bitunix/Weex 用 watermark
  达到同样效果同一个道理）
"""

from __future__ import annotations

import json

import pytest

from src.collectors.timeutil import ms_to_iso
from src.collectors.zoomex import ZoomexCollector
from src.db.connection import get_connection, init_db
from src.db.operations import compute_uid

LIST_ENDPOINT = "http://fake.test/getArticleListByMenuId"
DETAIL_ENDPOINT = "http://fake.test/getArticleById"

CFG = {
    "endpoint": LIST_ENDPOINT,
    "detail_endpoint": DETAIL_ENDPOINT,
    "method": "POST",
    "headers": {"Content-Type": "application/json"},
    "pagination": {"type": "offset", "param": "body:pageNum", "page_size_param": "body:pageSize", "page_size": 2},
    "rate_limit_ms": 0,
    "detail_mode": "separate_api",
    "strategy": "watermark",
    "lang_code": "en-US",
    "url_locale": "en",
}

def _default_articles() -> dict[int, dict]:
    """每个测试都要拿一份新的字典——FakeHttp 会读它模拟"源端状态"，测试之间不共享。"""
    return {
        101: {"created": 1700000000000, "updated": 1700000000000, "title": "Article 101"},
        102: {"created": 1700000100000, "updated": 1700000100000, "title": "Article 102"},
        103: {"created": 1700000200000, "updated": 1700000200000, "title": "Article 103"},
    }


class FakeHttp:
    """记录调用次数的 fetch_json 替身，按 URL 区分列表/详情请求。

    持有自己的 articles 字典（每个测试独立构造），测试可以直接改这个字典模拟"源端某篇
    文章被编辑过"，不依赖任何跨测试共享的全局状态。
    """

    def __init__(self, articles: dict[int, dict], page_size: int):
        self.articles = articles
        self.page_size = page_size
        self.detail_calls: list[int] = []
        self.list_calls = 0

    def _list_payload(self, page_num: int) -> dict:
        article_ids = list(self.articles.keys())
        start = (page_num - 1) * self.page_size
        page_ids = article_ids[start : start + self.page_size]
        return {
            "result": {
                "totalCount": len(article_ids),
                "page": page_num,
                "pageSize": self.page_size,
                "content": [
                    {
                        "article": {
                            "id": aid,
                            "gmtCreatedAt": self.articles[aid]["created"],
                            "gmtUpdatedAt": self.articles[aid]["updated"],
                        },
                        "contents": [{"lang": "en-US", "title": self.articles[aid]["title"]}],
                    }
                    for aid in page_ids
                ],
            }
        }

    def _detail_payload(self, article_id: int) -> dict:
        article = self.articles[article_id]
        # 正文里带上 updated 时间戳，模拟"内容真的被编辑过"（不能只改 gmtUpdatedAt
        # 却让详情正文原封不动，那样测试永远看不出 changed，见本文件顶部说明）。
        body_text = f"body of {article_id}, updated={article['updated']}"
        content = json.dumps([{"type": "paragraph", "children": [{"text": body_text}]}])
        return {
            "result": {
                "article": {
                    "id": article_id,
                    "gmtCreatedAt": article["created"],
                    "gmtUpdatedAt": article["updated"],
                },
                "contents": [{"lang": "en-US", "title": article["title"], "content": content}],
            }
        }

    def __call__(self, url, *, method="GET", headers=None, body=None):
        payload = json.loads(body)
        if url == LIST_ENDPOINT:
            self.list_calls += 1
            return self._list_payload(payload["pageNum"])
        if url == DETAIL_ENDPOINT:
            self.detail_calls.append(payload["id"])
            return self._detail_payload(payload["id"])
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _collector() -> ZoomexCollector:
    return ZoomexCollector("EN", CFG, category_key="platform_announcement", menu_id=26)


# ---------------------------------------------------------------- normalize ----

def test_normalize_builds_group_id_and_url_and_leaves_category_none():
    from src.collectors.base import RawItem

    collector = _collector()
    raw = RawItem(article_id=4116, title="t", content="c", post_time="2026-01-01T00:00:00Z", update_time="2026-01-02T00:00:00Z")
    ann = collector.normalize(raw)

    assert ann.source == "Zoomex"
    assert ann.article_id == "4116"
    assert ann.group_id == "zoomex_4116"
    assert ann.url == "https://www.zoomex.com/en/help/article/4116"
    assert ann.category is None


def test_normalize_raw_category_is_menu_id():
    from src.collectors.base import RawItem

    collector = _collector()  # menu_id=26
    raw = RawItem(article_id=4116, title="t", content="c")
    ann = collector.normalize(raw)

    assert ann.raw_category == "26"


# ---------------------------------------------------------------- 全量翻页 ----

def test_fetch_list_walks_all_pages_regardless_of_since(db_path, monkeypatch):
    articles = _default_articles()
    fake = FakeHttp(articles=articles, page_size=2)
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    collector = _collector()
    items = collector.fetch_list(since="2099-01-01T00:00:00Z")  # 故意给一个"未来"水位线

    # since 不影响 fetch_list 的翻页范围（见本文件顶部说明），3 篇文章、pageSize=2 应该翻 2 页
    assert fake.list_calls == 2
    assert sorted(int(i.article_id) for i in items) == [101, 102, 103]
    assert items[0].update_time == ms_to_iso(articles[101]["updated"])


# ---------------------------------------------------------------- 分页数上限（daily 增量） ----

def test_fetch_list_caps_pages_when_not_force_full(db_path, monkeypatch):
    # 6 篇文章、page_size=1、max_pages=2 -> 正常增量只应该翻 2 页（拿到前 2 篇）
    articles = {
        i: {"created": 1700000000000 + i, "updated": 1700000000000 + i, "title": f"Article {i}"}
        for i in range(1, 7)
    }
    fake = FakeHttp(articles=articles, page_size=1)
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    cfg = {**CFG, "pagination": {**CFG["pagination"], "page_size": 1, "max_pages": 2}}
    collector = ZoomexCollector("EN", cfg, category_key="platform_announcement", menu_id=26)
    collector.force_full = False  # 未经过 run()，模拟 daily 增量路径

    items = collector.fetch_list(since=None)

    assert fake.list_calls == 2
    assert sorted(int(i.article_id) for i in items) == [1, 2]


def test_fetch_list_ignores_page_cap_when_force_full(db_path, monkeypatch):
    articles = {
        i: {"created": 1700000000000 + i, "updated": 1700000000000 + i, "title": f"Article {i}"}
        for i in range(1, 7)
    }
    fake = FakeHttp(articles=articles, page_size=1)
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    cfg = {**CFG, "pagination": {**CFG["pagination"], "page_size": 1, "max_pages": 2}}
    collector = ZoomexCollector("EN", cfg, category_key="platform_announcement", menu_id=26)
    collector.force_full = True  # 模拟 run(force_full=True) 已经把这个属性同步过

    items = collector.fetch_list(since=None)

    assert fake.list_calls == 6
    assert sorted(int(i.article_id) for i in items) == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------- 增量：只对新增/变更详情请求 ----

def test_first_run_fetches_detail_for_all_then_second_run_skips_unchanged(db_path, monkeypatch):
    fake = FakeHttp(articles=_default_articles(), page_size=2)
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    collector = _collector()
    with get_connection(db_path) as conn:
        first = collector.run(conn)
    assert first.new == 3
    assert first.changed == 0
    assert sorted(fake.detail_calls) == [101, 102, 103]

    fake.detail_calls.clear()
    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 3
    assert fake.detail_calls == []  # update_time 没变，一次详情请求都不该发


def test_only_articles_with_changed_update_time_trigger_detail_fetch(db_path, monkeypatch):
    fake = FakeHttp(articles=_default_articles(), page_size=2)
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    collector = _collector()
    with get_connection(db_path) as conn:
        collector.run(conn)

    # 模拟源端只有 102 被编辑过：更新它的 gmtUpdatedAt
    fake.articles[102]["updated"] = 1700000999000
    fake.detail_calls.clear()

    with get_connection(db_path) as conn:
        second = collector.run(conn)
    assert second.changed == 1
    assert second.unchanged == 2
    assert fake.detail_calls == [102]


# ---------------------------------------------------------------- 变更检测（tamper） ----

def test_force_full_rerun_detects_manually_tampered_content_hash(db_path, monkeypatch):
    fake = FakeHttp(articles=_default_articles(), page_size=2)
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    collector = _collector()
    with get_connection(db_path) as conn:
        collector.run(conn)

    uid = compute_uid("Zoomex", "EN", "101")
    with get_connection(db_path) as conn:
        conn.execute("UPDATE announcements SET content_hash = 'tampered-hash' WHERE uid = ?", (uid,))

    fake.detail_calls.clear()
    with get_connection(db_path) as conn:
        second = collector.run(conn, force_full=True)

    # force_full 下三篇都会重新请求详情（跳过 needs_detail 优化）
    assert sorted(fake.detail_calls) == [101, 102, 103]
    assert second.changed == 1
    assert second.unchanged == 2

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT content_hash, status FROM announcements WHERE uid = ?", (uid,)).fetchone()
    assert row["status"] == "changed"
    assert row["content_hash"] != "tampered-hash"


# ---------------------------------------------------------------- 空正文兜底 ----

def test_fetch_detail_falls_back_to_title_when_content_is_empty(db_path, monkeypatch):
    """真实核查（2026-07-21，curl getArticleById）发现两类正文为空的文章：isRedirect=true
    的跳转型文章（真实内容在 redirectUrl 落地页，不在 content 字段里）、Slate.js 内容
    只有图片没有文字节点的公告。两种都不能让 content 存成空字符串悄悄从下游分析里消失，
    退化到用标题兜底。"""
    fake = FakeHttp(articles=_default_articles(), page_size=2)

    def empty_content_payload(article_id: int) -> dict:
        article = fake.articles[article_id]
        content = json.dumps(
            [{"type": "paragraph", "children": [{"type": "image", "src": "https://x/y.png"}, {"text": ""}]}]
        )
        return {
            "result": {
                "article": {"id": article_id, "gmtCreatedAt": article["created"], "gmtUpdatedAt": article["updated"]},
                "contents": [{"lang": "en-US", "title": article["title"], "content": content}],
            }
        }

    fake._detail_payload = empty_content_payload  # type: ignore[method-assign]
    monkeypatch.setattr("src.collectors.zoomex.fetch_json", fake)

    collector = _collector()
    with get_connection(db_path) as conn:
        collector.run(conn)

    uid = compute_uid("Zoomex", "EN", "101")
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT content FROM announcements WHERE uid = ?", (uid,)).fetchone()
    assert row["content"] == "Article 101"
