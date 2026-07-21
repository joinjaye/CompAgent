"""Lbank 精选活动采集器（www.lbank.com/new-popular-events，非 `notice/latestList`）。

跟常规 `src/collectors/lbank.py`（真实 JSON API，走 `categories.*` 的 7 个顶层公告
分类）是两条独立采集路径，产出数据落进同一张 `announcements` 表（同一个 `source`
值），靠 `crawl_state.category="new_popular_events"` 独立维护抓取状态。列表解析
细节见 `src/parsers/lbank_events.py` 顶部注释。

固定精选列表（真实测试过 `?page=2` 返回跟不带参数完全相同的 8 条，不是真分页），
`pagination: {type: none}`，`force_full` 对这个端口是 no-op（没有更多历史可回填）。
`strategy=full_scan`：没有可靠的"最后编辑时间"字段，只有活动起止时间，变更检测交给
`upsert_announcement` 的 content_hash 比对。

【2026-07-20 补充详情正文】列表页/`route_url` 详情页的 SSR HTML 都没有真实正文，
但用 Playwright 抓包活动详情页找到了两跳真正拿到正文的路径（详见
`src/parsers/lbank_events.py` 顶部「补充，详情正文」说明）：
1. `POST .../atlasActivity/loadingPage`（body `{"activityCode": <code>}`，
   `ex-language` 头控制语言）拿到 `ruleInfo.content`（有时候直接是文本）或
   `ruleInfo.contentId`（需要再请求一次的静态文件 URL）。这一跳**不需要签名**
   （跟 BingX 的 `qq-os.com` 网关不同，真实测试过纯 curl 不带任何签名头也返回
   200 + 完整数据）。
2. 如果拿到的是 `contentId`，再 GET 一次
   `resolve_rule_content_url()` 处理过的同源代理 URL（`www.lbank.com/
   static-backend-doc/content/...`，绕开 `jiz.lbk.world` 的 Cloudflare 挑战）
   拿到真实 HTML 规则正文。
两跳里任何一步失败（网络错误、响应结构变了）都记警告、正文退回列表页的
`subtitle`，不会让整个采集失败。

`article_id` 加 `event-` 前缀，理由跟 `src/collectors/bitunix_activity.py` 一致
（避免跟常规 Lbank 公告流的 `noticeId` 数值空间偶然撞号）。真实核对过同一个活动
`id` 跨 EN/VN/ID 一致，`group_id` 用同样前缀能正确跨 locale 归组。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.collectors.http import fetch_json
from src.collectors.timeutil import ms_to_iso
from src.parsers.html_text import html_to_text
from src.parsers.lbank_events import parse_activity_detail, parse_event_list

logger = logging.getLogger(__name__)

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
            raw_items.append(
                RawItem(
                    article_id=f"{_ARTICLE_ID_PREFIX}{entry['id']}",
                    title=entry.get("title"),
                    content=entry.get("subtitle") or "",
                    post_time=ms_to_iso(entry.get("start_time_ms")),
                    url=entry.get("route_url"),
                    extra={"code": entry.get("code"), "end_time_ms": entry.get("end_time_ms")},
                )
            )
        return raw_items

    def fetch_detail(self, item: RawItem) -> RawItem:
        code = item.extra.get("code")
        if not code:
            logger.warning("Lbank events 缺少 code，跳过详情请求：article_id=%s", item.article_id)
            return item

        headers = {"Content-Type": "application/json", "ex-language": self.config["lang_header"]}
        body = json.dumps({"activityCode": code}).encode()
        try:
            payload = fetch_json(self.config["detail_endpoint"], method="POST", headers=headers, body=body)
        except Exception:
            logger.warning("Lbank events loadingPage 请求失败，仅用列表摘要：article_id=%s code=%s", item.article_id, code)
            return item

        detail = parse_activity_detail(payload)
        if detail is None:
            logger.warning("Lbank events loadingPage 响应解析失败，仅用列表摘要：article_id=%s code=%s", item.article_id, code)
            return item

        rule_html = detail.get("rule_content")
        if not rule_html and detail.get("rule_content_url"):
            try:
                rule_html = http_fetch(detail["rule_content_url"])
            except Exception:
                logger.warning(
                    "Lbank events 规则正文静态文件请求失败，仅用列表摘要：article_id=%s url=%s",
                    item.article_id, detail["rule_content_url"],
                )
                rule_html = None

        if rule_html:
            item.content = f"{item.content}\n\n{rule_html}" if item.content else rule_html
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        content_text = html_to_text(item.content) if item.content else ""
        period = _format_period(item.post_time, ms_to_iso(item.extra.get("end_time_ms")))
        if period:
            content_text = f"{content_text}\n\n{period}" if content_text else period
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
