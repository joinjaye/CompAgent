"""Phemex 公告页面（phemex.com/{locale}/announcements/...）解析。

服务端渲染（非 SPA），列表页和详情页都内嵌 `window.preloadedData = {...}`——一个
JS 对象字面量（key 不带引号、字符串用单引号，不是严格 JSON，不能直接
`json.loads`，Phase 1 侦察已确认，见 CLAUDE.md/sources.yaml phemex 块）。
2026-07-14 实现前用真实请求核对过具体的键路径（不是照 Phase 1 记录的
`field_mapping` 猜的解析路径，那份记录只标注了字段名，没有标注"字段在哪一层
嵌套 key 下"）：

- 列表页：`pageData.total`（分类下全部文章数）+ `pageData.articles[]`（本页
  展示的文章，固定 20 条，没有分页——query 参数不生效，真翻页是未逆向的客户端
  交互，同 BingX）。每条：id/locale/title/slug/desc/author/publishedTime/
  publishedAt/headerImage/url/month/day/year，**不含 content**。
- 详情页：`pageData.id/title/content`（HTML 正文）/publishedTime（朴素字符串
  'YYYY-MM-DD HH:MM:SS'，无时区）/i18n.updatedAt（ISO8601 UTC，抽样显示只是
  发布流程级别的秒级噪音，不可靠，仅记录不参与增量判断）/category（`{id,
  name,...}` —— **不要**把 `category.name` 存进 `raw_category`，locale 相关
  的翻译文本，见 CLAUDE.md Phase 2.6 订正；`raw_category` 应该用采集时已知的
  `categories.*` 配置 key，collector 层处理，不是这个 parser 的事）。

因为源数据不是严格 JSON，本文件用一个手写的最小 JS 对象字面量解析器
（`_JsLiteralParser`）而不是正则替换 key/引号——正则在字符串内容恰好包含
`key:`/单引号等模式时会误判，手写的字符级解析器逐字符判断当前在字符串内部还是
结构内部，不会有这个问题。只支持 object/array/string/number/true/false/null
这几种字面量（Next.js/Nuxt 的 preloadedData 转储不会出现函数/正则/日期对象等
JS 特有类型，未观察到过，也不打算支持）。

【2026-07-14 补充：找到真实分页 API，见 CLAUDE.md「Phemex 分页升级」】上面记录的
"列表页没有分页，query 参数不生效"结论对 `window.preloadedData` 这条路径成立，
但用 headless browser 抓包发现页面 hydration 之后真正调用的是一个匿名 JSON API：
`GET https://prod-cms-api.phemex.com/articles/query?categoryKey=Announcement
Category<id>&entryKey=Announcement&language=<lang>&pageNo=N&pageSize=M`，
**真正支持翻页**（pageNo 递增返回完全不同的文章，已用真实请求验证）、无需签名/
cookie。**关键发现**：`categoryKey` 的数字部分不随 locale 变化——一直是 EN 侧
记录的 432/442/452（news/activities/newsletter），不是 Phase 2.6 订正记录的
"i18n 各 locale 独立编号"那组值（438/442/452 是 FR/... 那批，本项目 EN 侧
category_id 才是这个查询接口认的稳定 key）；切换语言完全靠 `language` 参数
（`en`/`fr`，已用真实请求验证 FR 参数生效、标题正确翻译、总数与 Phase 1 侦察
记录的 FR 总数吻合）。`data.rows[]` 每条只有 id/language/slug/publishedTime/
title/authorName/desc（**截断预览，不是完整正文**）/tagKeys/categoryKey，
`data.total` 是分类下全部文章数。**完整正文仍然要靠详情页的 `window.
preloadedData`**（`parse_article_detail()`，未受这次改动影响——detail 页没有
类似的分页/截断问题，此前已验证正文完整、不需要动）。
"""

from __future__ import annotations

from typing import Any, Optional


class _JsLiteralParseError(ValueError):
    pass


class _JsLiteralParser:
    """把 `window.preloadedData = {...}` 里那段 JS 对象字面量文本解析成 Python
    对象。字符级手写解析，不用正则整体替换——字符串内容本身可能包含冒号、单引号
    等结构字符（如公告标题里的 "SKHYUSDT / USDT" 这类文本），正则替换容易在这些
    地方误判结构边界。"""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.len = len(text)

    def parse(self) -> Any:
        self._skip_ws()
        return self._parse_value()

    def _skip_ws(self) -> None:
        while self.pos < self.len and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < self.len else ""

    def _parse_value(self) -> Any:
        self._skip_ws()
        c = self._peek()
        if c == "{":
            return self._parse_object()
        if c == "[":
            return self._parse_array()
        if c in ("'", '"'):
            return self._parse_string()
        if self.text.startswith("null", self.pos):
            self.pos += 4
            return None
        if self.text.startswith("true", self.pos):
            self.pos += 4
            return True
        if self.text.startswith("false", self.pos):
            self.pos += 5
            return False
        return self._parse_number()

    def _parse_object(self) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        self.pos += 1  # {
        self._skip_ws()
        if self._peek() == "}":
            self.pos += 1
            return obj
        while True:
            self._skip_ws()
            key = self._parse_key()
            self._skip_ws()
            if self._peek() != ":":
                raise _JsLiteralParseError(f"expected ':' at {self.pos}: {self.text[self.pos:self.pos+40]!r}")
            self.pos += 1
            value = self._parse_value()
            obj[key] = value
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
                self._skip_ws()
                if self._peek() == "}":  # 容忍尾随逗号
                    self.pos += 1
                    break
                continue
            if self._peek() == "}":
                self.pos += 1
                break
            raise _JsLiteralParseError(f"unexpected char at {self.pos}: {self.text[self.pos:self.pos+40]!r}")
        return obj

    def _parse_key(self) -> str:
        c = self._peek()
        if c in ("'", '"'):
            return self._parse_string()
        start = self.pos
        while self.pos < self.len and (self.text[self.pos].isalnum() or self.text[self.pos] in "_$"):
            self.pos += 1
        if start == self.pos:
            raise _JsLiteralParseError(f"expected key at {self.pos}: {self.text[self.pos:self.pos+40]!r}")
        return self.text[start : self.pos]

    def _parse_array(self) -> list[Any]:
        arr: list[Any] = []
        self.pos += 1  # [
        self._skip_ws()
        if self._peek() == "]":
            self.pos += 1
            return arr
        while True:
            arr.append(self._parse_value())
            self._skip_ws()
            if self._peek() == ",":
                self.pos += 1
                self._skip_ws()
                if self._peek() == "]":
                    self.pos += 1
                    break
                continue
            if self._peek() == "]":
                self.pos += 1
                break
            raise _JsLiteralParseError(f"unexpected char at {self.pos}: {self.text[self.pos:self.pos+40]!r}")
        return arr

    _ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}

    def _parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        chars: list[str] = []
        while True:
            if self.pos >= self.len:
                raise _JsLiteralParseError("unterminated string literal")
            c = self.text[self.pos]
            if c == "\\":
                nxt = self.text[self.pos + 1] if self.pos + 1 < self.len else ""
                if nxt == "u":
                    hexcode = self.text[self.pos + 2 : self.pos + 6]
                    chars.append(chr(int(hexcode, 16)))
                    self.pos += 6
                elif nxt in self._ESCAPES:
                    chars.append(self._ESCAPES[nxt])
                    self.pos += 2
                else:
                    chars.append(nxt)
                    self.pos += 2
                continue
            if c == quote:
                self.pos += 1
                break
            chars.append(c)
            self.pos += 1
        return "".join(chars)

    def _parse_number(self) -> Any:
        start = self.pos
        while self.pos < self.len and self.text[self.pos] in "0123456789+-.eE":
            self.pos += 1
        s = self.text[start : self.pos]
        if not s:
            raise _JsLiteralParseError(f"unexpected char at {self.pos}: {self.text[self.pos:self.pos+40]!r}")
        if any(ch in s for ch in ".eE"):
            return float(s)
        return int(s)


def _extract_preloaded_data(html: str) -> Optional[dict[str, Any]]:
    """从 HTML 里找到 `window.preloadedData = {...}`，用花括号配对（而不是非贪婪
    正则）取出完整对象文本——对象内部必然还有更多 `{`/`}`（嵌套的 category/i18n
    等），非贪婪正则会在第一个内部 `}` 处提前截断。找不到/解析失败返回 None。"""
    marker = "window.preloadedData"
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None
    depth = 0
    end = -1
    in_string: Optional[str] = None
    i = start
    while i < len(html):
        c = html[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == in_string:
                in_string = None
        elif c in ("'", '"'):
            in_string = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
        i += 1
    if end == -1:
        return None
    try:
        result = _JsLiteralParser(html[start : end + 1]).parse()
    except _JsLiteralParseError:
        return None
    return result if isinstance(result, dict) else None


def parse_article_detail(html: str) -> Optional[dict[str, Any]]:
    """详情页 -> {title, content, published_time, updated_at}；解析不到返回
    None（调用方应记日志，不要把 None 当空字符串静默吞掉）。"""
    data = _extract_preloaded_data(html)
    if not data:
        return None
    page_data = data.get("pageData")
    if not isinstance(page_data, dict):
        return None
    i18n = page_data.get("i18n")
    updated_at = i18n.get("updatedAt") if isinstance(i18n, dict) else None
    return {
        "title": page_data.get("title"),
        "content": page_data.get("content"),
        "published_time": page_data.get("publishedTime"),
        "updated_at": updated_at,
    }


def parse_query_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], Optional[int]]:
    """`prod-cms-api.phemex.com/articles/query` 响应（真正分页的匿名 JSON API，
    见本文件顶部「2026-07-14 补充」）-> (文章条目 dict list, total)。跟
    `parse_article_list()` 的返回形状一致（url 未加 phemex.com 前缀，交给
    collector 拼），方便 collector 复用同一套 RawItem 构建逻辑。响应结构异常
    时返回 `([], None)`，不抛异常。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        return [], None
    rows = data.get("rows")
    total = data.get("total")
    if not isinstance(rows, list):
        return [], total if isinstance(total, int) else None
    result: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        result.append(
            {
                "article_id": item.get("id"),
                "title": item.get("title"),
                "url": item.get("slug"),
                "published_time_ms": item.get("publishedTime"),
            }
        )
    return result, (total if isinstance(total, int) else None)
