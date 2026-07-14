"""src/collectors/__main__.py 里 collector 展开逻辑的单测（不发真实请求，纯构造逻辑）。

覆盖：
- _categorized_collector_builder 对没有 categories 结构的单分类配置（Bitunix）
  原样返回一个 collector，category=''，向后兼容路径不能破
- _categorized_collector_builder 对有 categories 结构的多分类配置展开成多个
  collector，每个 category 各自拿到自己的 endpoint、crawl_state.category——这里借用
  WeexCollector 的构造函数签名验证展开逻辑本身（config 内容是随手写的旧式占位，不
  代表 Weex 现在真实的 sources.yaml 结构，见 src/collectors/weex.py 的真实实现）；
  WeexCollector 不再继承 ZendeskCollector 之后这个通用 builder 依然对它适用，因为
  builder 只要求 `(locale, config)` / `(locale, config, category_key)` 构造签名，
  不要求特定基类
- --category 过滤（_build_collectors）能只选中其中一个分类
"""

from __future__ import annotations

from src.collectors.__main__ import _build_collectors, _categorized_collector_builder
from src.collectors.bitunix import BitunixCollector
from src.collectors.weex import WeexCollector

SHARED_CFG = {
    "method": "GET",
    "headers": {},
    "pagination": {"type": "offset", "param": "page", "page_size_param": "per_page", "page_size": 30},
    "rate_limit_ms": 0,
    "detail_mode": "inline",
    "strategy": "watermark",
    "field_mapping": {"article_id": "id", "title": "title", "content": "body",
                       "post_time": "created_at", "update_time": "updated_at", "category": "section_id"},
}


def test_single_category_config_returns_one_collector_with_empty_category():
    cfg = {**SHARED_CFG, "endpoint": "https://support.bitunix.com/.../articles.json"}
    build = _categorized_collector_builder(BitunixCollector)

    collectors = build("EN", cfg)

    assert len(collectors) == 1
    assert collectors[0].category == ""
    assert collectors[0].config["endpoint"] == cfg["endpoint"]


def test_multi_category_config_expands_into_one_collector_per_category():
    cfg = {
        **SHARED_CFG,
        "categories": {
            "latest_announcements": {"endpoint": "https://weexsupport.zendesk.com/.../18540264809497/articles.json"},
            "listings_delistings": {"endpoint": "https://weexsupport.zendesk.com/.../44507081559193/articles.json"},
        },
    }
    build = _categorized_collector_builder(WeexCollector)

    collectors = build("EN", cfg)

    assert len(collectors) == 2
    by_category = {c.category: c for c in collectors}
    assert set(by_category.keys()) == {"latest_announcements", "listings_delistings"}
    assert by_category["latest_announcements"].config["endpoint"].endswith("18540264809497/articles.json")
    assert by_category["listings_delistings"].config["endpoint"].endswith("44507081559193/articles.json")
    # 每个分类的 config 不应该再带 categories 键（避免递归/误用）
    assert "categories" not in by_category["latest_announcements"].config


def test_build_collectors_category_filter_selects_single_category():
    sources = {
        "weex": {
            "EN": {
                **SHARED_CFG,
                "categories": {
                    "latest_announcements": {"endpoint": "https://weexsupport.zendesk.com/.../a/articles.json"},
                    "listings_delistings": {"endpoint": "https://weexsupport.zendesk.com/.../b/articles.json"},
                },
            }
        }
    }

    collectors = _build_collectors(sources, source_filter="weex", locale_filter="EN", category_filter="listings_delistings")

    assert len(collectors) == 1
    assert collectors[0].category == "listings_delistings"


def test_build_collectors_without_category_filter_returns_all_categories():
    sources = {
        "weex": {
            "EN": {
                **SHARED_CFG,
                "categories": {
                    "latest_announcements": {"endpoint": "https://weexsupport.zendesk.com/.../a/articles.json"},
                    "listings_delistings": {"endpoint": "https://weexsupport.zendesk.com/.../b/articles.json"},
                },
            }
        }
    }

    collectors = _build_collectors(sources, source_filter="weex", locale_filter="EN", category_filter=None)

    assert len(collectors) == 2
