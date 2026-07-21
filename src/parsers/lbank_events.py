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
- 拼接后的 flight 流本身就是合法 JSON（不是 Phemex 那种宽松 JS 对象字面量），
  `"list":[...]` 数组可以直接按 key 定位、`json.loads` 解析。

【2026-07-20 补充，详情正文】列表页/详情页（`routeUrl`）的 SSR HTML 都没有真实
正文，用 Playwright 抓包活动详情页找到了真正承载正文的两个请求：
- `POST https://www.lbank.com/lbk-api/huli-bazaar-center/atlasActivity/
  loadingPage`，body `{"activityCode": "<code>"}`（`code` 就是列表接口给的
  `code` 字段，"pointmall/" 这类 routeUrl 前缀不影响，真实测试过），header
  `ex-language: <en-US|vi-VN|id>` 控制语言。**不需要签名**（跟 BingX 的
  `qq-os.com` 网关不同，Playwright 里挂着的 `ex-signature`/`ex-device-id` 等
  请求头是可选的，真实测试过纯 curl 不带这些头也返回 200 + 完整数据）。响应里
  `data.ruleInfo` 是规则正文的落脚点：`content` 字段本身有时候是 null，这时候
  真正的正文在 `contentId`（指向一个静态文本文件的 URL）。
- `contentId` 的域名不稳定（真实观察到两种形式：`https://jiz.lbk.world/
  content/{...}.stxt` 或 `/static-backend-doc/content/{...}.stxt`），
  `jiz.lbk.world` 这个域名有 Cloudflare bot 挑战，纯 curl 请求会被拦
  （`Attention Required! | Cloudflare`）；但同一份内容在
  `www.lbank.com/static-backend-doc/content/{...}.stxt` 这个同源代理路径下
  可以直接拿到（无挑战、无需登录），真实测试过对不同 activityCode/不同 locale
  的多个样本都成立。`resolve_rule_content_url()` 统一从 `contentId` 里截取
  `content/...` 往后的部分，重新拼到 `www.lbank.com/static-backend-doc/` 前缀
  下，规避掉不稳定的域名。
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


def resolve_rule_content_url(content_id: Optional[str]) -> Optional[str]:
    """`ruleInfo.contentId` -> 走 www.lbank.com 同源代理的静态文件 URL，规避
    `jiz.lbk.world` 域名的 Cloudflare 挑战（见本文件顶部说明）。`content_id`
    可能是完整 URL 也可能已经是 `/static-backend-doc/...` 相对路径，统一从
    `content/` 出现的位置截取，找不到这个片段（响应结构变了）时返回 None。
    """
    if not content_id:
        return None
    idx = content_id.find("content/")
    if idx == -1:
        return None
    return f"https://www.lbank.com/static-backend-doc/{content_id[idx:]}"


def parse_activity_detail(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """`atlasActivity/loadingPage` 响应 -> `{title, sub_title, rule_content,
    rule_content_url}`。`rule_content`：`ruleInfo.content` 字段本身有文本时直接
    给出（省一次静态文件请求）；`rule_content_url`：`content` 为空时，从
    `contentId` 解析出的可直接请求的 URL，调用方（collector）负责再发一次请求
    拿到真实 HTML 正文。两者最多一个非 None。解析不到 `data` 字典（响应结构变了/
    `code != 200`）时返回 None。
    """
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    head_info = data.get("headInfo") or {}
    title_info = head_info.get("titleInfo") or {}
    rule_info = data.get("ruleInfo") or {}
    rule_content = rule_info.get("content")

    return {
        "title": title_info.get("title"),
        "sub_title": title_info.get("subTitle"),
        "rule_content": rule_content if rule_content else None,
        "rule_content_url": resolve_rule_content_url(rule_info.get("contentId")) if not rule_content else None,
    }
