"""Bitunix 活动中心页面（www.bitunix.com/activity/act-center）解析。

背景：常规公告采集（src/collectors/bitunix.py，走 support.bitunix.com 的 Zendesk API）
只覆盖 Zendesk "Announcements" 分类，验证后发现活动中心页面上的很多营销活动内容根本
不在这条公告流里出现过——活动中心是主站 www.bitunix.com 自己的一套独立 Nuxt 应用
（跟已确认死路的旧版 platformgateway.bitunix.com 不是一回事，这次真实请求验证过可用）。

**同一页面在不同 locale 下用了两种完全不同的数据格式**（2026-07-20 真实请求逐一核对）：

- EN（`/activity/act-center`，无 locale 前缀）：数据在 Nuxt devalue 编码的
  `<script id="__NUXT_DATA__">` 里，扁平数组 + 整数索引引用，解析方式跟
  `src/parsers/bingx_web.py` 的 `_resolve_all`/`_normalize` 思路一致（本文件独立
  实现一份，不跨文件 import 私有函数）。**比 bingx_web.py 多修一个真实撞见的 bug**：
  `_normalize` 判断 Vue 响应式包装 `["ShallowReactive"/"Reactive"/"Ref", <ref>]`
  时原实现是 `value[0] in _REACTIVE_TAGS`，Bitunix 这个页面的 devalue 数组里出现过
  `[<dict>, ...]` 形状的 list（第一个元素不是字符串 tag，是一个 dict），
  `dict in set` 会因为 dict 不可 hash 直接抛 `TypeError`——加一个
  `isinstance(value[0], str)` 前置判断即可安全跳过，不是新发明的解析逻辑。
- FR（`/fr-fr/activity/act-center`）/ ID（`/id-id/activity/act-center`）：数据不在
  `__NUXT_DATA__` 里，而是一段明文 `window.__custom__nuxt__payload = {};
  Object.assign(window.__custom__nuxt__payload, {...真实JSON...})`——`Object.assign`
  第二个参数本身就是合法 JSON，直接 `json.loads` 即可，完全不需要 devalue 解引用。
  真实核对过：这份 JSON 里的 `id`/`applyStartTime`/`applyEndTime` 等数值字段跨
  locale 一致（如 `id=6223` 在 EN/FR/ID 三个 locale 下都一样），只有
  `name`/`description`/`ruleDescription` 这些文本字段是翻译版本。

两种路径解析出的顶层结构相同：`data` 字典下有
`__act-center__activity-center-ongoing-list-page` /
`__act-center__activity-center-ended-list-page` 两个 key，各自
`{records, total, pages, size, current}`。**单次请求同时给出 ongoing + ended 两个
列表**；真实测试过 `?page=2` 会让这两个 key 从响应里完全消失（不是真分页，是请求被
识别成了不认识的路由），是固定单页视图，不需要 `pagination.max_pages` 逻辑。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_NUXT_DATA_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_CUSTOM_PAYLOAD_ASSIGN = "Object.assign(window.__custom__nuxt__payload,"
_REACTIVE_TAGS = {"ShallowReactive", "Reactive", "Ref"}

_LIST_KEYS = (
    "__act-center__activity-center-ongoing-list-page",
    "__act-center__activity-center-ended-list-page",
)


def _extract_balanced(text: str, start: int, open_ch: str, close_ch: str) -> Optional[str]:
    """从 `start`（指向第一个 `open_ch`）开始按括号配对找到匹配的 `close_ch`，
    返回这段原始文本（含首尾括号）。跟 weex_web.py 的
    `_extract_balanced_json_array` 同一个思路，泛化成任意一对括号字符。"""
    if start >= len(text) or text[start] != open_ch:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_custom_payload(html: str) -> Optional[dict[str, Any]]:
    """FR/ID 用的明文 payload：找 `Object.assign(window.__custom__nuxt__payload,`
    之后第一个 `{`，按大括号配对取出完整 JSON 对象文本，`json.loads`。找不到/解析
    失败返回 None（调用方会退回 devalue 路径）。"""
    idx = html.find(_CUSTOM_PAYLOAD_ASSIGN)
    if idx == -1:
        return None
    obj_start = html.find("{", idx + len(_CUSTOM_PAYLOAD_ASSIGN))
    if obj_start == -1:
        return None
    obj_text = _extract_balanced(html, obj_start, "{", "}")
    if obj_text is None:
        return None
    try:
        payload = json.loads(obj_text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_all(raw: list[Any]) -> list[Any]:
    """devalue 扁平数组解引用，跟 bingx_web.py 的 `_resolve_all` 同一个算法。"""
    cache: dict[int, Any] = {}

    def resolve(idx: int) -> Any:
        if idx in cache:
            return cache[idx]
        cache[idx] = None  # 环路守卫
        v = raw[idx]
        if isinstance(v, list):
            resolved: Any = [resolve(x) if isinstance(x, int) else x for x in v]
        elif isinstance(v, dict):
            resolved = {k: (resolve(x) if isinstance(x, int) else x) for k, x in v.items()}
        else:
            resolved = v
        cache[idx] = resolved
        return resolved

    return [resolve(i) for i in range(len(raw))]


def _normalize(value: Any) -> Any:
    """解引用之后的收尾清洗，跟 bingx_web.py 的 `_normalize` 同一个算法，多一处
    防御：判断 Reactive 包装标记前先确认 `value[0]` 是字符串，避免 Bitunix 这个
    页面真实出现过的 `[<dict>, ...]` 形状 list 让 `in _REACTIVE_TAGS` 因为 dict
    不可 hash 而抛异常（见本文件顶部注释）。"""
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str) and value[0] in _REACTIVE_TAGS:
            return _normalize(value[1])
        if value and isinstance(value[0], str) and value[0] == "null" and len(value) % 2 == 1:
            pairs = value[1:]
            return {k: _normalize(v) for k, v in zip(pairs[0::2], pairs[1::2])}
        return [_normalize(x) for x in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _extract_devalue_data(html: str) -> Optional[dict[str, Any]]:
    """EN 用的 devalue `__NUXT_DATA__` 路径，返回顶层 `data` 字典。"""
    m = _NUXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    resolved_all = _resolve_all(raw)
    root = _normalize(resolved_all[0])
    if not isinstance(root, dict):
        return None
    data = _normalize(root.get("data"))
    return data if isinstance(data, dict) else None


def parse_activity_list(html: str) -> list[dict[str, Any]]:
    """活动中心页面 -> 活动条目 dict list（合并 ongoing + ended 两个子列表）。

    先试 FR/ID 用的明文 `window.__custom__nuxt__payload`（更简单、不需要 devalue
    解引用），找不到才退回 EN 用的 devalue `__NUXT_DATA__` 路径——两种格式实测都
    真实出现过，不能假设只有一种。都解析不到时返回空 list，不抛异常。

    字段：id / title / description / rule_description（HTML，含完整活动规则）/
    start_time / end_time（均已经是 UTC ISO8601 字符串，源端字段本来就是这个格式，
    不需要 ms_to_iso 转换）/ url（相对路径，如 "/activity/basic/pizza-day-2026"）/
    status（"ongoing"/"ended"，仅供参考，不驱动任何采集逻辑）。
    """
    data = _extract_custom_payload(html)
    if data is None:
        data = _extract_devalue_data(html)
    if not data:
        return []

    result: list[dict[str, Any]] = []
    for key in _LIST_KEYS:
        page = data.get(key)
        if not isinstance(page, dict):
            continue
        status = "ongoing" if "ongoing" in key else "ended"
        for record in page.get("records") or []:
            if not isinstance(record, dict) or record.get("id") is None:
                continue
            result.append(
                {
                    "id": record.get("id"),
                    "title": record.get("title") or record.get("name"),
                    "description": record.get("description"),
                    "rule_description": record.get("ruleDescription"),
                    "start_time": record.get("applyStartTime"),
                    "end_time": record.get("applyEndTime"),
                    "url": record.get("url"),
                    "status": status,
                }
            )
    return result
