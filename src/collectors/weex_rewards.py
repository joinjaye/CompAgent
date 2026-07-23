"""Weex 活动奖励采集器（www.weex.com/rewards，非帮助中心）。

跟常规 `src/collectors/weex.py`（走 help.categories/sections 帮助中心页面）是两条
独立采集路径，产出数据落进同一张 `announcements` 表（同一个 `source` 值），靠
`crawl_state.category="rewards"` 独立维护抓取状态。解析细节见
`src/parsers/weex_rewards.py` 顶部注释。

固定精选列表（真实测试确认 18 EN / 11 FR，不是分页 API），`pagination:
{type: none}`，`force_full` 对这个端口是 no-op。`strategy=full_scan`：没有可靠的
"最后编辑时间"字段，只有活动起止时间，变更检测交给 `upsert_announcement` 的
content_hash 比对。

四个新端口里**唯一一个详情页真的 SSR 出正文**的：`fetch_detail()` 请求
`/[locale/]events/{sub}/{slug}`（`sub` 由 `resolve_detail_path(activityType)`
决定，查不到映射时记警告、跳过详情请求，只用列表页字段兜底），正文 =
`agentShareContent`（顶层摘要）+ 每个 `miniActivity` 任务里匹配当前 locale
lang code 的 `introI18` 文本拼接。lang code 精确值（`en_US`/`fr_FR`）已用真实
详情页请求核对过，不是猜的；某个任务在当前 locale 没有对应语言版本时直接跳过
那条（不拿别的语言凑数）。

`article_id` 加 `reward-` 前缀，理由跟 `bitunix_activity.py`/`lbank_events.py`
一致（避免跟常规 Weex 帮助中心的 `documentId`/`id` 数值空间偶然撞号）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.collectors.timeutil import ms_to_iso
from src.parsers.html_text import html_to_text
from src.parsers.weex_rewards import parse_reward_detail, parse_reward_list, resolve_detail_path

logger = logging.getLogger(__name__)

_ARTICLE_ID_PREFIX = "reward-"

# collector 的 locale（EN/FR）-> 源站详情页 introI18 用的 lang code，见
# src/parsers/weex_rewards.py 顶部说明（真实请求核对过 en_US/fr_FR 两个值）。
_LOCALE_LANG_CODE = {
    "EN": "en_US",
    "FR": "fr_FR",
}


class WeexRewardsCollector(BaseCollector):
    source_name = "Weex"

    def __init__(self, locale: str, config: dict[str, Any]):
        super().__init__(locale, config)
        self.category = "rewards"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan，固定精选列表：since 不参与判断，见本文件顶部说明。
        html = http_fetch(self.config["endpoint"])
        items = parse_reward_list(html)

        raw_items: list[RawItem] = []
        for entry in items:
            locale_path = self.config.get("locale_path", "")
            slug = entry.get("slug")
            sub_path = resolve_detail_path(entry.get("activity_type"))
            detail_url = None
            if slug and sub_path:
                detail_url = f"https://www.weex.com/{locale_path}events/{sub_path}/{slug}"
            elif slug:
                logger.warning(
                    "Weex rewards 未识别的 activityType，跳过详情请求：activity_id=%s activity_type=%s",
                    entry.get("activity_id"), entry.get("activity_type"),
                )
            raw_items.append(
                RawItem(
                    article_id=f"{_ARTICLE_ID_PREFIX}{entry['activity_id']}",
                    title=entry.get("title"),
                    content=entry.get("sub_title") or "",
                    post_time=None,
                    url=detail_url,
                    extra={
                        "start_time_ms": entry.get("start_time_ms"),
                        "end_time_ms": entry.get("end_time_ms"),
                    },
                )
            )
        return raw_items

    def fetch_detail(self, item: RawItem) -> RawItem:
        if not item.url:
            return item
        html = http_fetch(item.url)
        detail = parse_reward_detail(html)
        if detail is None:
            logger.warning("Weex rewards 详情页解析失败，仅用列表摘要：article_id=%s url=%s", item.article_id, item.url)
            return item

        lang_code = _LOCALE_LANG_CODE.get(self.locale)
        parts = [detail["agent_share_content"]] if detail["agent_share_content"] else []
        if lang_code:
            for task in detail["tasks"]:
                text = task.get(lang_code)
                if text:
                    parts.append(text)
        item.content = "\n\n".join(parts) if parts else item.content
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        content_text = html_to_text(item.content) if item.content else ""
        activity_start = ms_to_iso(item.extra.get("start_time_ms"))
        activity_end = ms_to_iso(item.extra.get("end_time_ms"))
        period = _format_period(activity_start, activity_end)
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
            activity_start_time=activity_start,
            activity_end_time=activity_end,
            category=None,
            raw_category=self.category,
            group_id=f"weex_{item.article_id}",
            source_endpoint=self.config.get("endpoint"),
        )


def _format_period(start: Optional[str], end: Optional[str]) -> str:
    if not start and not end:
        return ""
    return f"活动周期: {start or '?'} ~ {end or '?'}"
