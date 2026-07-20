"""Bitunix 活动中心采集器（www.bitunix.com/activity/act-center，非 Zendesk）。

跟常规 `src/collectors/bitunix.py`（ZendeskCollector 子类，走 support.bitunix.com）
是两条完全独立的采集路径，产出数据落进同一张 `announcements` 表（同一个 `source`
值），靠 `crawl_state.category="campaign_center"` 独立维护自己的抓取状态，互不干扰。
解析细节见 `src/parsers/bitunix_activity.py` 顶部注释。

固定单页视图（真实测试过 `?page=2` 会让数据整个消失，不是分页 API），
`pagination: {type: none}`，跟 BingX 首屏聚合视图同一先例——不需要 `max_pages`，
`force_full` 对这个端口是 no-op（没有更多历史可回填）。`strategy=full_scan`：没有
"最后编辑时间"字段，只有活动起止时间，变更检测交给 `upsert_announcement` 的
content_hash 比对。

正文自带在列表响应里（`ruleDescription`，完整 HTML），不需要发详情请求，等同
Bitunix 常规采集器的 `detail_mode: inline`。

`article_id` 加 `actcenter-` 前缀：活动中心的数值 id（如 6223）跟常规 Zendesk
公告的数值 id 是不同空间，但为了绝对避免未来偶然撞号导致两条本质不同的内容被
`upsert_announcement` 错误合并成一行，统一加前缀（跟 `group_id` 用同样前缀，保证
跨 locale 归组不受影响——已用真实数据核对过同一个活动 id 跨 EN/FR/ID 一致）。
"""

from __future__ import annotations

from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.parsers.bitunix_activity import parse_activity_list
from src.parsers.html_text import html_to_text

_ARTICLE_ID_PREFIX = "actcenter-"


class BitunixActivityCollector(BaseCollector):
    source_name = "Bitunix"

    def __init__(self, locale: str, config: dict[str, Any]):
        super().__init__(locale, config)
        self.category = "campaign_center"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan，固定单页视图：since 不参与判断，见本文件顶部说明。
        html = http_fetch(self.config["endpoint"])
        items = parse_activity_list(html)

        raw_items: list[RawItem] = []
        for entry in items:
            content = entry.get("rule_description") or entry.get("description") or ""
            period = _format_period(entry.get("start_time"), entry.get("end_time"))
            if period:
                content = f"{content}\n\n{period}" if content else period
            url = entry.get("url")
            raw_items.append(
                RawItem(
                    article_id=f"{_ARTICLE_ID_PREFIX}{entry['id']}",
                    title=entry.get("title"),
                    content=content,
                    post_time=entry.get("start_time"),
                    url=f"https://www.bitunix.com{url}" if url else None,
                )
            )
        return raw_items

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        content_text = html_to_text(item.content) if item.content else ""
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=str(item.article_id),
            url=item.url,
            title=item.title,
            content=content_text,
            post_time=item.post_time,
            update_time=None,
            category=None,  # 走 Phase 3 pipeline 前不分类；raw_category 已经直接是 campaign 语义
            raw_category=self.category,
            group_id=f"bitunix_{item.article_id}",
            source_endpoint=self.config.get("endpoint"),
        )


def _format_period(start: Optional[str], end: Optional[str]) -> str:
    if not start and not end:
        return ""
    return f"活动周期: {start or '?'} ~ {end or '?'}"
