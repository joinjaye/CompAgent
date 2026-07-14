"""Weex 公告页面（www.weex.com/{locale}/help/...）解析。

背景（2026-07-14 侦察，见 CLAUDE.md「Weex 数据源迁移」）：weexsupport.zendesk.com 的
公开 Zendesk REST API 已经不是 Weex 公告的真实来源——实测确认它从 2026-05-16 起就没有
再更新过，但 www.weex.com/help 前台页面（Next.js SSR）能看到最新到当天的公告。前台不
经过任何可发现的 JSON/XHR 接口（Playwright 抓包为空），列表和详情的数据都是服务端直接
渲染进返回的 HTML 里的：

- 列表页（分类或 section 页均可，URL 形如
  `https://www.weex.com/{locale}/help/categories/{category_id}?page={n}` 或
  `.../help/sections/{section_id}?page={n}`）：数据以 Next.js React Server
  Components 的 flight 流格式内嵌在若干个
  `<script>self.__next_f.push([1,"..."])</script>` 标签里，把这些标签的第二个参数
  （本身是一个 JSON 字符串转义过的大字符串）按出现顺序拼接、做一次 unicode_escape
  解码，就能在结果里找到一个字面量的 `"articleListData":[...]` JSON 数组，逐条给出
  文章的 id（本项目当 article_id 用）/name（标题）/createdAt（ms 时间戳）/
  sectionId（当 raw_category 用）/prioritise（置顶，不参与任何"按时间排序"的假设，
  见下）/url。以及一个 `"pageInfo":{"page":...}` + `"totalCount":N,"totalPage":M`
  用于分页判断。
- 详情页（`.../help/articles/{id}`，id 既可能是老 Zendesk 数字 ID 也可能是新系统的
  小写字母数字 slug，两种都用同一套页面结构，实测验证过）：正文**不需要**解析 flight
  流——它同时以真正的服务端渲染 HTML 存在于页面正文里（`<div class="zendesk-html
  ...">...</div>`），比解析 flight 流里的 `"body":"$50"` 引用（还要再找对应的
  `50:T<hexlen>,<原始文本>` 分段、处理 RSC 的文本分段协议）简单可靠得多，直接摘出
  这个 div 的内层 HTML，交给 `src/parsers/html_text.py` 转纯文本即可（跟 Bitunix/
  Weex 旧 Zendesk 数据走的是同一套 HTML→纯文本转换器，格式一致）。

排序上的教训（跟 Zoomex 批次 2 同一个坑，见 CLAUDE.md「Phase 2 批次 2」）：`prioritise`
字段标记的置顶文章可能出现在列表最前面而不遵循时间顺序，本模块因此不假设整体严格按
`createdAt` 降序排列，只负责把每条数据如实解析出来，是否可以依赖顺序做提前退出翻页
是 collector 层的判断（本次接入按 full_scan 策略实现，不依赖排序假设，见
src/collectors/weex.py）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_NEXT_F_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL)
_ARTICLE_LIST_KEY = '"articleListData":'
_PAGE_INFO_RE = re.compile(r'"totalCount":(\d+),"totalPage":(\d+)')
_ZENDESK_HTML_DIV_OPEN_RE = re.compile(
    r'<div\s+class="[^"]*\bzendesk-html\b[^"]*"[^>]*>', re.IGNORECASE
)
_DIV_TAG_RE = re.compile(r"<(/?)div\b[^>]*>", re.IGNORECASE)


def _extract_flight_text(html: str) -> str:
    """拼接全部 self.__next_f.push(...) 的原始内容，还原成真实文本。

    捕获到的内容本质是 JSON 字符串字面量的内部文本（`push([1, "STRING"])`
    的 STRING 部分遵循 JSON 字符串转义规则：`\\"`/`\\\\`/`\\n`/`\\uXXXX` 等），
    但真正的非 ASCII 字符（如中文、法语重音字符）是**原样的 UTF-8 字符**直接嵌在
    HTML 源码里，不是转义序列——`html` 本身在更早的 `resp.read().decode("utf-8")`
    那一步就已经是正确解码的 Python str。

    早期实现在这里用过 `text.encode("utf-8").decode("unicode_escape")`，这是错的：
    把已经正确的 Unicode 字符串重新编码成 UTF-8 字节，再用 unicode_escape（本质是
    Latin-1 + 转义处理）解码，会把每个多字节 UTF-8 字符拆成几个乱码字符（典型
    mojibake，如 "é" 变成 "Ã©"）——真实抓取的法语 P2P 公告标题
    （"Offre spéciale..."）曾经被错误解析成 "Offre spÃ©ciale..."，2026-07-14
    真实网络验收时发现，见 CLAUDE.md「Weex 数据源迁移」。

    正确做法：把拼接后的文本当成一个 JSON 字符串字面量的内容，套上引号交给
    `json.loads` 解析——只有真正的转义序列会被处理，非转义的字面量 Unicode 字符
    完全不受影响。"""
    chunks = _NEXT_F_PUSH_RE.findall(html)
    joined = "".join(chunks)
    try:
        return json.loads(f'"{joined}"')
    except json.JSONDecodeError:
        # 极端情况下（页面结构变化导致拼接结果不是合法的 JSON 字符串内容）降级为
        # 原样返回，不抛异常——调用方的正则/JSON 解析会自然找不到数据、返回空结果，
        # 比整个采集流程崩溃更安全。
        return joined


def _extract_balanced_json_array(text: str, key: str) -> Optional[str]:
    """从 `key` 后面第一个 `[` 开始，按方括号配对找到匹配的 `]`，返回这段原始
    JSON 数组文本。avoid 用非贪婪正则整体匹配——数组内部本来就会出现更多 `[`/`]`
    （如 keywords 数组），非贪婪匹配会在第一个内部 `]` 处提前截断。"""
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


def parse_article_list(html: str) -> list[dict[str, Any]]:
    """列表页（category 或 section 均可）-> 文章条目 dict list。

    字段命名对齐本项目 RawItem 的用法习惯：article_id / title / post_time_ms /
    section_id / prioritise / url。post_time_ms 是原始 unix 毫秒整数，转 UTC
    ISO8601 是 collector.normalize() 的事（跟 zoomex.py 的 timeutil 用法一致）。
    找不到 articleListData（页面结构变了，或本页确实没有文章）时返回空 list，
    不抛异常——空 list 会被 collector 当成"这页没有更多数据"处理。
    """
    full = _extract_flight_text(html)
    arr_text = _extract_balanced_json_array(full, _ARTICLE_LIST_KEY)
    if not arr_text:
        return []
    try:
        raw_list = json.loads(arr_text)
    except json.JSONDecodeError:
        return []

    result: list[dict[str, Any]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        # 用 id 做 article_id，不是 documentId：2026-07-14 真实采集时撞见过同一篇
        # 旧文章（同一个数字 id、同一个 url）在列表里出现两条不同 documentId 的记录
        # （疑似 Weex 自己 CMS 迁移历史文章时留下的重复 document 记录，likeId 相邻
        # 但不同），如果拿 documentId 当 article_id 会把同一篇文章的正文重复插成
        # 两行。id 才是贯穿新旧两套体系、真正稳定的标识（新文章的 id 本来就等于
        # documentId，旧文章的 id 是原来的 Zendesk 数字 ID，url 里的路径段也是它）。
        article_id = item.get("id") if item.get("id") is not None else item.get("documentId")
        if article_id is None:
            continue
        result.append(
            {
                "article_id": str(article_id),
                "title": item.get("name"),
                "post_time_ms": item.get("createdAt"),
                "section_id": item.get("sectionId"),
                "prioritise": bool(item.get("prioritise")),
                "url": item.get("url"),
            }
        )
    return result


def parse_page_info(html: str) -> Optional[tuple[int, int]]:
    """返回 (totalCount, totalPage)，解析不到时返回 None（调用方应当把它当成
    "翻页翻到头了/页面结构异常"处理，不强行假设还有下一页）。"""
    full = _extract_flight_text(html)
    m = _PAGE_INFO_RE.search(full)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def extract_article_body_html(html: str) -> Optional[str]:
    """详情页 -> 正文 HTML 片段（`<div class="zendesk-html ...">` 的内层 HTML，
    未转纯文本）。用手写的 div 深度计数定位匹配的结束标签，而不是非贪婪正则——
    正文内部必然还有别的 `<div>`（如图片的 `<figure>` 容器、表格外层 div 等），
    非贪婪匹配会在正文内部第一个 `</div>` 处就误判提前结束。找不到该 div（页面
    结构变了）时返回 None，调用方应该记日志、不要把 None 当空字符串静默吞掉。
    """
    m = _ZENDESK_HTML_DIV_OPEN_RE.search(html)
    if not m:
        return None
    start = m.end()
    depth = 1
    for tm in _DIV_TAG_RE.finditer(html, start):
        if tm.group(1):
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            return html[start : tm.start()]
    return None
