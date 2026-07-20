"""Lbank 精选活动采集器（www.lbank.com/new-popular-events，非 `notice/latestList`）。

跟常规 `src/collectors/lbank.py`（真实 JSON API，走 `categories.*` 的 7 个顶层公告
分类）是两条独立采集路径，产出数据落进同一张 `announcements` 表（同一个 `source`
值），靠 `crawl_state.category="new_popular_events"` 独立维护抓取状态。解析细节见
`src/parsers/lbank_events.py` 顶部注释。

固定精选列表（真实测试过 `?page=2` 返回跟不带参数完全相同的 8 条，不是真分页），
`pagination: {type: none}`，`force_full` 对这个端口是 no-op（没有更多历史可回填）。
`strategy=full_scan`：没有可靠的"最后编辑时间"字段，只有活动起止时间，变更检测交给
`upsert_announcement` 的 content_hash 比对。

**content 只有 title+subtitle，没有更深正文可用**：真实请求确认详情页
（`route_url`）没有 SSR 出任何正文，只有 meta description；正文靠后续未逆向的
客户端请求获取，本采集器不发详情请求，跟常规 Lbank collector"不发详情请求"的
既有简化设计一致（`fetch_detail` 走 `BaseCollector` 默认的恒等实现，不需要覆写）。

`article_id` 加 `event-` 前缀，理由跟 `src/collectors/bitunix_activity.py` 一致
（避免跟常规 Lbank 公告流的 `noticeId` 数值空间偶然撞号）。真实核对过同一个活动
`id` 跨 EN/VN/ID 一致，`group_id` 用同样前缀能正确跨 locale 归组。
"""

from __future__ import annotations

from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.collectors.timeutil import ms_to_iso
from src.parsers.html_text import html_to_text
from src.parsers.lbank_events import parse_event_list

_ARTICLE_ID_PREFIX = "event-"


class LbankEventsCollector(BaseCollector):
    source_name = "Lbank"

    def __init__(self, locale: str, config: dict[str, Any]):
        super().__init__(locale, config)
        self.category = "new_popular_events"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan，固定精选列表：since 不参与判断，见本文件顶部说明。
        headers = {"ex-language": self.config["lang_header"]}
        html = http_fetch(self.config["endpoint"], headers=headers)
        items = parse_event_list(html)

        raw_items: list[RawItem] = []
        for entry in items:
            start_iso = ms_to_iso(entry.get("start_time_ms"))
            end_iso = ms_to_iso(entry.get("end_time_ms"))
            content = entry.get("subtitle") or ""
            period = _format_period(start_iso, end_iso)
            if period:
                content = f"{content}\n\n{period}" if content else period
            raw_items.append(
                RawItem(
                    article_id=f"{_ARTICLE_ID_PREFIX}{entry['id']}",
                    title=entry.get("title"),
                    content=content,
                    post_time=start_iso,
                    url=entry.get("route_url"),
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
            category=None,
            raw_category=self.category,
            group_id=f"lbank_{item.article_id}",
            source_endpoint=self.config.get("endpoint"),
        )


def _format_period(start: Optional[str], end: Optional[str]) -> str:
    if not start and not end:
        return ""
    return f"活动周期: {start or '?'} ~ {end or '?'}"
