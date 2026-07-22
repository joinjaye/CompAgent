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
- 【2026-07-20 新增，见 CLAUDE.md「补充活动类内容采集端口」】`campaign_endpoint`
  子块能让 `_bitunix_builder`/`_weex_builder`/`_bingx_builder`/`_lbank_builder`
  在常规展开结果之外，额外产出一个活动端口专属的 collector 实例，两路数据并集
  写入同一个 source；没有配置 `campaign_endpoint` 时行为跟改动前逐字节一致
  （不额外产出任何实例）
"""

from __future__ import annotations

from src.collectors.__main__ import (
    _bingx_builder,
    _bitunix_builder,
    _build_collectors,
    _categorized_collector_builder,
    _lbank_builder,
    _weex_builder,
)
from src.collectors.bingx_events import BingXEventsCollector
from src.collectors.bitunix import BitunixCollector
from src.collectors.bitunix_activity import BitunixActivityCollector
from src.collectors.lbank_events import LbankEventsCollector
from src.collectors.weex import WeexCollector
from src.collectors.weex_rewards import WeexRewardsCollector

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


def test_build_collectors_source_filter_is_case_insensitive():
    sources = {
        "weex": {
            "EN": {
                **SHARED_CFG,
                "categories": {
                    "latest_announcements": {
                        "endpoint": "https://weexsupport.zendesk.com/articles.json"
                    },
                },
            }
        }
    }

    collectors = _build_collectors(
        sources, source_filter="Weex", locale_filter="EN", category_filter=None,
    )

    assert len(collectors) == 1


# ---------------------------------------------------------- campaign_endpoint ----

def test_bitunix_builder_without_campaign_endpoint_unchanged():
    cfg = {**SHARED_CFG, "endpoint": "https://support.bitunix.com/.../articles.json"}

    collectors = _bitunix_builder("EN", cfg)

    assert len(collectors) == 1
    assert isinstance(collectors[0], BitunixCollector)


def test_bitunix_builder_with_campaign_endpoint_appends_activity_collector():
    cfg = {
        **SHARED_CFG,
        "endpoint": "https://support.bitunix.com/.../articles.json",
        "campaign_endpoint": {"endpoint": "https://www.bitunix.com/activity/act-center", "strategy": "full_scan"},
    }

    collectors = _bitunix_builder("EN", cfg)

    assert len(collectors) == 2
    assert isinstance(collectors[0], BitunixCollector)
    assert isinstance(collectors[1], BitunixActivityCollector)
    assert collectors[1].category == "campaign_center"
    assert collectors[1].config["endpoint"] == "https://www.bitunix.com/activity/act-center"


def test_weex_builder_with_campaign_endpoint_appends_rewards_collector():
    cfg = {
        **SHARED_CFG,
        "categories": {"latest_announcements": {"endpoint": "https://www.weex.com/en/help/categories/x"}},
        "campaign_endpoint": {"endpoint": "https://www.weex.com/rewards", "strategy": "full_scan"},
    }

    collectors = _weex_builder("EN", cfg)

    assert len(collectors) == 2
    assert isinstance(collectors[-1], WeexRewardsCollector)
    assert collectors[-1].category == "rewards"


def test_bingx_builder_with_campaign_endpoint_appends_events_collector():
    cfg = {
        **SHARED_CFG,
        "endpoint": "https://bingx.com/en/support/notice-center",
        "campaign_endpoint": {"endpoint": "https://bingx.com/en/events", "strategy": "full_scan"},
    }

    collectors = _bingx_builder("EN", cfg)

    assert len(collectors) == 2
    assert isinstance(collectors[-1], BingXEventsCollector)
    assert collectors[-1].category == "activity_center"


def test_lbank_builder_with_campaign_endpoint_appends_events_collector():
    cfg = {
        **SHARED_CFG,
        "lang_header": "en-US",
        "categories": {"new_listings": {"category_code": "CO00000053"}},
        "campaign_endpoint": {"endpoint": "https://www.lbank.com/new-popular-events", "strategy": "full_scan"},
    }

    collectors = _lbank_builder("EN", cfg)

    assert len(collectors) == 2
    assert isinstance(collectors[-1], LbankEventsCollector)
    assert collectors[-1].category == "new_popular_events"


def test_build_collectors_category_filter_selects_campaign_endpoint_only():
    sources = {
        "bitunix": {
            "EN": {
                **SHARED_CFG,
                "endpoint": "https://support.bitunix.com/.../articles.json",
                "campaign_endpoint": {"endpoint": "https://www.bitunix.com/activity/act-center", "strategy": "full_scan"},
            }
        }
    }

    collectors = _build_collectors(sources, source_filter="bitunix", locale_filter="EN", category_filter="campaign_center")

    assert len(collectors) == 1
    assert isinstance(collectors[0], BitunixActivityCollector)
