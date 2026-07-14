"""BingX 采集器。

【2026-07-14 政策调整，见 CLAUDE.md「水位逻辑策略调整」】BingX 首屏聚合视图
（.../support/notice-center）**不是**分页接口——`?page=`/`?sectionId=` 等 query
参数不改变 SSR 输出，真正的翻页是未逆向的客户端交互（Phase 1 结论，实测确认这次
仍然如此）。Phase 2.6 曾经记录过一个"日常增量用 sitemap diff 找新增 + 首屏~20条
兼顾编辑检测"的设计，本次改用户要求的统一简化模型后已废弃：不实现 sitemap 全量
枚举，`fetch_list()` 固定只返回首屏这一屏（跨 12 个分区聚合，约 20 条，Phase 1
确认过），不管 `force_full` 是 True 还是 False——**force_full 对 BingX 是
no-op**，如实记录，不假装支持全量回填（全量回填能力只保留给 Zoomex，见
CLAUDE.md 铁律调整）。这意味着如果单日发布量超过首屏窗口，多出来的公告会被
永久漏采（不是"变更检测不到"，是从未进过 fetch_list 的返回值）——这是本次政策
调整下接受的已知代价，不是被忽略的问题。

strategy=full_scan：createTime/updateTime 抽样恒等（Phase 1 及本次真实请求复核
一致），watermark 不可靠，`since` 参数不参与判断，变更检测交给
`upsert_announcement` 的 content_hash 比对。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.collectors.http import rate_limit_seconds
from src.collectors.timeutil import offset_iso_to_utc_iso
from src.parsers.bingx_web import parse_article_detail, parse_article_list
from src.parsers.html_text import html_to_text

logger = logging.getLogger(__name__)


class BingXCollector(BaseCollector):
    source_name = "BingX"

    def __init__(self, locale: str, config: dict[str, Any], category_key: str = ""):
        super().__init__(locale, config)
        self.category = category_key  # 恒为 ''：BingX 没有 categories 结构，首屏
        # 本身就是跨分区聚合，不按分区单独维护水位线

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan：since 不参与判断。首屏聚合视图不是分页接口，force_full 对
        # BingX 没有额外数据源可切换，见本文件顶部说明。
        html = http_fetch(self.config["endpoint"])
        items: list[RawItem] = []
        for raw in parse_article_list(html):
            items.append(
                RawItem(
                    article_id=raw["article_id"],
                    title=raw["title"],
                    post_time=offset_iso_to_utc_iso(raw["create_time"]),
                    update_time=offset_iso_to_utc_iso(raw["update_time"]),
                    category_raw=raw["section_id"],
                )
            )
        return items

    def fetch_detail(self, item: RawItem) -> RawItem:
        rate_limit_s = rate_limit_seconds(self.config)
        time.sleep(rate_limit_s)

        detail_url = self.config["detail_endpoint"].format(article_id=item.article_id)
        html = http_fetch(detail_url)
        detail = parse_article_detail(html)
        if detail is None:
            logger.warning("BingX 详情页解析失败，正文置空：article_id=%s url=%s", item.article_id, detail_url)
            return item
        item.title = detail.get("title") or item.title
        item.content = detail.get("body")
        if detail.get("section_id") is not None:
            item.category_raw = detail["section_id"]
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        article_id = str(item.article_id)
        content_text = html_to_text(item.content) if item.content else ""
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=article_id,
            url=self.config["detail_endpoint"].format(article_id=article_id),
            title=item.title,
            content=content_text,
            post_time=item.post_time,
            update_time=item.update_time,
            category=None,  # Phase 3 之前不分类
            raw_category=str(item.category_raw) if item.category_raw is not None else None,
            group_id=f"bingx_{article_id}",  # article_id 跨 locale 一致，Phase 1 已确认
            source_endpoint=self.config.get("endpoint"),
        )
