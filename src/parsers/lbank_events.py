"""Lbank 精选活动页面（www.lbank.com/new-popular-events）解析。

背景：常规公告采集（src/collectors/lbank.py，走 `notice/latestList` JSON API）只
覆盖 7 个顶层公告分类，验证后发现这个独立的活动落地页上很多营销活动内容根本不在
公告流里出现过。这个页面走跟旧版（已删除的）`src/parsers/lbank_web.py` 同一种
Next.js RSC flight 流机制（`self.__next_f.push([1,"..."])`），不是常规采集器现在
用的那个 JSON API——本文件只写这次需要的这一小块提取逻辑，不复活旧文件。

真实请求核对过（2026-07-20）：
- EN（无 locale 前缀）/ VN（`/vi-VN/...`）/ ID（`/id/...`）均可用，跟常规 Lbank
  collector 的 `lang_header`/`locale_path` 配置值一致，标题/副标题确实是翻译版本
  （`id` 数值跨 locale 一致）。
- 固定 8 条精选活动，`?page=2` 返回跟 `?page=1`（或不带参数）完全相同的 8 条——
  不是真分页，是一个固定的"最新最热"精选列表。
- 每条活动的详情页（`routeUrl`）真实请求确认**没有 SSR 出任何正文**，只有 meta
  description，正文靠后续未逆向的客户端请求获取——所以本文件只解析列表页字段，
  没有对应的详情页解析函数。
- 拼接后的 flight 流本身就是合法 JSON（不是 Phemex 那种宽松 JS 对象字面量），
  `"list":[...]` 数组可以直接按 key 定位、`json.loads` 解析。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL)
_LIST_KEY = '"list":'


def _extract_flight_text(html: str) -> str:
    """拼接全部 `self.__next_f.push(...)` 的原始内容，还原成真实文本。跟
    `weex_web.py._extract_flight_text` 同一个方法（拼接后当 JSON 字符串字面量
    解析，不能用 unicode_escape，会把多字节字符拆成 mojibake，Weex 已经踩过这个坑）。
    """
    chunks = _NEXT_F_PUSH_RE.findall(html)
    joined = "".join(chunks)
    try:
        return json.loads(f'"{joined}"')
    except json.JSONDecodeError:
        return joined


def _extract_balanced_json_array(text: str, key: str) -> Optional[str]:
    """跟 `weex_web.py._extract_balanced_json_array` 同一个方法：从 `key` 后面
    第一个 `[` 开始按方括号配对找匹配的 `]`，避免非贪婪正则在数组内部第一个 `]`
    处提前截断。"""
    idx = text.find(key)
    if idx == -1:
        return None
    start = text.find("[", idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_event_list(html: str) -> list[dict[str, Any]]:
    """精选活动落地页 -> 活动条目 dict list。字段：id / code / title
    （`activityName`，标题不带 HTML）/ subtitle / start_time / end_time（均原始
    unix 毫秒，转 UTC ISO8601 是 collector 的事）/ route_url。找不到 `"list":`
    数组（页面结构变了）时返回空 list，不抛异常。
    """
    full = _extract_flight_text(html)
    arr_text = _extract_balanced_json_array(full, _LIST_KEY)
    if not arr_text:
        return []
    try:
        raw_list = json.loads(arr_text)
    except json.JSONDecodeError:
        return []

    result: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        result.append(
            {
                "id": item.get("id"),
                "code": item.get("code"),
                "title": item.get("activityName") or item.get("title"),
                "subtitle": item.get("subtitle"),
                "start_time_ms": item.get("startTime"),
                "end_time_ms": item.get("endTime"),
                "route_url": item.get("routeUrl"),
            }
        )
    return result
