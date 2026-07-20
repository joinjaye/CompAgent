"""Weex 活动奖励页面（www.weex.com/rewards）解析。

背景：常规公告采集（src/collectors/weex.py，走 www.weex.com/help/... 帮助中心页面）
只覆盖 3 个 Zendesk 迁移出来的分类，验证后发现 `/rewards` 落地页上很多营销活动
内容根本不在帮助中心里出现过。这是标准 Next.js `__NEXT_DATA__`（不是 flight 流，
比帮助中心页面简单），列表页 `props.pageProps.myPopular[]` 就是精选活动数组。

真实请求核对过（2026-07-20）：
- EN（`/rewards`，`/en/rewards` 会 301 到无前缀）/ FR（`/fr/rewards`）均可用，标题/
  副标题确实是翻译版本。固定精选列表（18 EN / 11 FR），`activityId`/`startTime`/
  `endTime` 跨 locale 一致。
- 详情页 URL 是 `/[locale/]events/{sub}/{showUrl}`，`{sub}` 不是固定值——真实抓取
  确认至少有 `promo`（activityType=7，最常见）、`roll`（activityType=23）、
  `draw`（activityType=5）三种，从页面的 `<a href>` 逐一核对过 `activityType` 跟
  子路径一一对应，不是从字段名猜的。`_ACTIVITY_TYPE_PATH` 只登记了这三个已验证值，
  遇到未登记的 `activityType` 时 `resolve_detail_path()` 返回 None（调用方应该
  记日志、跳过详情请求，不要猜一个子路径去发请求）。
- 详情页 `props.pageProps.defaultDetail`：`agentShareContent`（顶层短摘要）+
  `miniActivity[]`（活动内的子任务），每个子任务的 `introI18[]` 是**全部语言**
  一次性返回的数组（不是只返回当前 URL locale 那一种）——真实核对过同一份英文
  URL（`/fr/events/promo/tradfi` 和不带前缀的英文 URL）返回的 `introI18` 都包含
  `en_US`/`fr_FR`/`zh_CN`/... 全部 locale，URL 前缀只影响页面壳的语言，不影响
  这份数据本身。所以只需要请求一次详情页（不需要按 collector 的 locale 分别请求），
  由调用方按自己的 locale 去 `tasks` 里找对应语言的文本。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)

# activityType -> 详情页 URL 子路径，真实请求逐一核对过（见本文件顶部注释）。
_ACTIVITY_TYPE_PATH = {
    7: "promo",
    23: "roll",
    5: "draw",
}


def _load_next_data(html: str) -> Optional[dict[str, Any]]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def resolve_detail_path(activity_type: Any) -> Optional[str]:
    """activityType -> 详情页子路径（"promo"/"roll"/"draw"），未登记的类型返回 None。"""
    try:
        return _ACTIVITY_TYPE_PATH.get(int(activity_type))
    except (TypeError, ValueError):
        return None


def parse_reward_list(html: str) -> list[dict[str, Any]]:
    """`/rewards` 列表页 -> 活动条目 dict list。字段：activity_id / title
    （偶尔带 `<p>`/`<strong>` 等内联 HTML，不是纯文本）/ sub_title / slug（`showUrl`，
    拼详情页 URL 用）/ start_time_ms / end_time_ms / activity_type（配合
    `resolve_detail_path` 用）。解析不到 `myPopular` 时返回空 list，不抛异常。
    """
    data = _load_next_data(html)
    if not data:
        return []
    page_props = ((data.get("props") or {}).get("pageProps")) or {}
    my_popular = page_props.get("myPopular")
    if not isinstance(my_popular, list):
        return []

    result: list[dict[str, Any]] = []
    for item in my_popular:
        if not isinstance(item, dict) or item.get("activityId") is None:
            continue
        result.append(
            {
                "activity_id": item.get("activityId"),
                "title": item.get("title"),
                "sub_title": item.get("subTitle"),
                "slug": item.get("showUrl"),
                "start_time_ms": item.get("startTime"),
                "end_time_ms": item.get("endTime"),
                "activity_type": item.get("activityType"),
            }
        )
    return result


def parse_reward_detail(html: str) -> Optional[dict[str, Any]]:
    """详情页 -> `{"agent_share_content": str, "tasks": [dict[lang_code, html], ...]}`。

    `tasks` 里每个 dict 是一个 `miniActivity` 子任务的全部语言版本（key 是源站的
    lang code，如 "en_US"/"fr_FR"，value 是该语言的富文本 HTML），调用方按自己的
    locale 去查对应 key，查不到就跳过这个子任务（不猜别的语言充数）。解析不到
    `defaultDetail` 时返回 None（调用方应该记日志、正文置空，不要假装成功）。
    """
    data = _load_next_data(html)
    if not data:
        return None
    page_props = ((data.get("props") or {}).get("pageProps")) or {}
    detail = page_props.get("defaultDetail")
    if not isinstance(detail, dict):
        return None

    tasks: list[dict[str, str]] = []
    for mini in detail.get("miniActivity") or []:
        if not isinstance(mini, dict):
            continue
        intro_by_lang: dict[str, str] = {}
        for entry in mini.get("introI18") or []:
            if not isinstance(entry, dict):
                continue
            lang = entry.get("lang")
            html_text = entry.get("name")
            if lang and html_text:
                intro_by_lang[lang] = html_text
        if intro_by_lang:
            tasks.append(intro_by_lang)

    return {
        "agent_share_content": detail.get("agentShareContent") or "",
        "tasks": tasks,
    }
