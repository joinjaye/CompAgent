"""Phemex 采集器。

【2026-07-14 分页升级，见 CLAUDE.md「Phemex 分页升级」】原实现（列表页
`window.preloadedData`，固定 20 条无分页）已废弃列表获取路径，改用 headless
browser 抓包找到的真实匿名分页 API：`GET prod-cms-api.phemex.com/articles/query
?categoryKey=AnnouncementCategory<id>&entryKey=Announcement&language=<lang>&
pageNo=N&pageSize=M`。真正支持翻页（pageNo 递增返回不同文章，已用真实请求验证），
不需要签名/cookie。`categoryKey` 的数字部分**不随 locale 变化**——真实验证过用
EN 侧记录的 432/442/452（不是 FR 侧那组 i18n 独立编号 438/448/458）配合
`language=fr` 参数，能正确拿到法语标题的 Phemex FR 数据，总数与 Phase 1 侦察
记录的 FR 总数吻合，见 src/parsers/phemex_web.py 顶部「2026-07-14 补充」说明。

**详情页抓取路径不变**：这个新接口的 `desc` 字段只是截断预览，不是完整正文；
完整正文仍然靠 `window.preloadedData`（`parse_article_detail()`），未受这次
改动影响。

**force_full 不再是 no-op**：现在有真正的翻页能力，`force_full=True` 时忽略
`pagination.max_pages` 上限，翻到 `rows` 返回空为止，等同 Zoomex/Weex/Lbank 的
全量核查语义；默认（`force_full=False`）只翻前 `max_pages` 页，遵守项目政策
（见 CLAUDE.md「水位逻辑策略调整」）。

strategy=full_scan：createdAt/updatedAt 抽样只有 0-28 秒的发布流程噪音，不是真实
编辑信号（Phase 1 结论），`since` 不参与判断，变更检测交给 `upsert_announcement`
的 content_hash 比对。

3 个分类（news/activities/newsletter）各自独立的 `list_category_id`，一个
PhemexCollector 实例 = 一个 locale × 一个 category，`raw_category` 直接存这个
category key（不解析响应里的 `category.name`——locale 相关的翻译文本，见
CLAUDE.md Phase 2.6 订正），跟 Zoomex 用 menu_id、Weex 用 categories.* 展开是
同一个模式。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.collectors.http import fetch_json, rate_limit_seconds
from src.collectors.timeutil import ms_to_iso
from src.parsers.html_text import html_to_text
from src.parsers.phemex_web import parse_article_detail, parse_query_response

logger = logging.getLogger(__name__)


class PhemexCollector(BaseCollector):
    source_name = "Phemex"

    def __init__(self, locale: str, config: dict[str, Any], category_key: str):
        super().__init__(locale, config)
        self.category = category_key  # news / activities / newsletter

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan：since 不参与判断，见本文件顶部说明。
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        page_size = pagination.get("page_size", 20)
        max_pages = None if self.force_full else pagination.get("max_pages", 5)
        rate_limit_s = rate_limit_seconds(cfg)

        items: list[RawItem] = []
        page_no = 1
        while True:
            query = urlencode(
                {
                    "categoryKey": f"AnnouncementCategory{cfg['list_category_id']}",
                    "entryKey": "Announcement",
                    "language": cfg["language"],
                    "pageNo": page_no,
                    "pageSize": page_size,
                }
            )
            payload = fetch_json(f"{cfg['list_endpoint']}?{query}")
            raw_items, _total = parse_query_response(payload)
            if not raw_items:
                break
            for raw in raw_items:
                items.append(
                    RawItem(
                        article_id=raw["article_id"],
                        title=raw["title"],
                        url=f"https://phemex.com{raw['url']}" if raw.get("url") else None,
                        post_time=ms_to_iso(raw["published_time_ms"]),
                    )
                )

            if max_pages is not None and page_no >= max_pages:
                break
            page_no += 1
            time.sleep(rate_limit_s)
        return items

    def fetch_detail(self, item: RawItem) -> RawItem:
        if not item.url:
            logger.warning("Phemex 文章缺少详情页 URL，跳过正文抓取：article_id=%s", item.article_id)
            return item
        rate_limit_s = rate_limit_seconds(self.config)
        time.sleep(rate_limit_s)

        html = http_fetch(item.url)
        detail = parse_article_detail(html)
        if detail is None:
            logger.warning("Phemex 详情页解析失败，正文置空：article_id=%s url=%s", item.article_id, item.url)
            return item
        item.title = detail.get("title") or item.title
        item.content = detail.get("content")
        item.update_time = detail.get("updated_at")  # 抽样显示只是发布流程噪音，仅记录不参与增量判断
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        article_id = str(item.article_id)
        content_text = html_to_text(item.content) if item.content else ""
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=article_id,
            url=item.url,
            title=item.title,
            content=content_text,
            post_time=item.post_time,
            update_time=item.update_time,
            category=None,  # Phase 3 之前不分类
            raw_category=self.category,  # news/activities/newsletter，非响应字段解析值
            group_id=f"phemex_{article_id}",
            source_endpoint=self.config.get("list_endpoint"),
        )
