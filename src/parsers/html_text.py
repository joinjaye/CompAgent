"""HTML 正文 -> 纯文本，纯函数，不发请求，离线可单测。

用于 Zendesk（Bitunix/Weex）以及后续 Phemex/BingX/Lbank 三个批次的正文清洗。清洗前移到
采集层（Phase 2.5），落库的 content 从此就是清洗后的纯文本，content_hash 是清洗后正文的
SHA256，见 CLAUDE.md schema 表格。

表格的文本表示法（行以 "\n" 分隔、单元格以 "\t" 分隔）刻意跟 src/parsers/slate_json.py
的 `_render_table` 保持一致，不发明第二套表示法——Zoomex 的正文来自 Slate.js JSON，
其它源来自 HTML，两条链路最终应该产出同一种"表格转文本"的观感。

不做完整 HTML5 解析（不需要）：基于标准库 html.parser.HTMLParser，按标签边界切分文本块
（块级元素/<br> 触发换行），跳过 script/style/nav/header/footer 等模板标签，以及
class/id 命中常见导航/页脚/免责声明模板关键词的元素。畸形 HTML 不抛异常（html.parser
本身对畸形标签是容错的；万一真的抛出，兜底降级为正则去标签，不丢数据）。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "nav",
        "header",
        "footer",
        "noscript",
        "iframe",
        "form",
        "button",
        "svg",
        "object",
        "embed",
    }
)

_NOISE_MARKERS = (
    "nav",
    "footer",
    "disclaimer",
    "cookie",
    "breadcrumb",
    "sidebar",
    "social-share",
    "site-menu",
)

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "section",
        "article",
        "figure",
        "figcaption",
        "br",
        "hr",
        "pre",
        "dl",
        "dt",
        "dd",
    }
)

_WHITESPACE_RE = re.compile(r"[ \t\r\n]+")
_TAG_RE = re.compile(r"<[^>]+>")


def _is_noise_element(tag: str, attrs: list[tuple[str, Optional[str]]]) -> bool:
    if tag in _SKIP_TAGS:
        return True
    attrs_dict = dict(attrs)
    marker_text = f"{attrs_dict.get('class') or ''} {attrs_dict.get('id') or ''}".lower()
    return any(marker in marker_text for marker in _NOISE_MARKERS)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buffer: list[str] = []
        self._skip_stack: list[str] = []
        self._table_rows: Optional[list[str]] = None
        self._table_current_row: Optional[list[str]] = None
        self._in_cell = False

    # -- 跳过标签（导航/页脚/脚本等模板噪音） --------------------------------

    def _skipping(self) -> bool:
        return bool(self._skip_stack)

    # -- 内联文本缓冲区 -------------------------------------------------------

    def _flush(self) -> None:
        text = _WHITESPACE_RE.sub(" ", "".join(self._buffer)).strip()
        self._buffer = []
        if text:
            self.blocks.append(text)

    def _block_boundary(self) -> None:
        """块级标签（p/div/br 等）的起止边界。单元格内部（_in_cell）不能直接调用
        _flush()——那样会把单元格文字提前推进顶层 self.blocks，导致 _end_cell() 拿到
        的 buffer 是空的（表格塌成一堆空 tab）。这是 Phase 2.7 用真实 Weex 上币公告
        （article_id=56648741969433，<td><p>Trading pair</p></td> 这种 cell 内嵌 <p>
        的表格）实测发现的：单元格里的块级边界只插入一个空格分隔（避免"line1line2"
        连读），不产生新的顶层 block；单元格外部行为不变。"""
        if self._in_cell:
            self._buffer.append(" ")
        else:
            self._flush()

    # -- 表格 ------------------------------------------------------------

    def _start_table(self) -> None:
        self._flush()
        self._table_rows = []

    def _end_table(self) -> None:
        if self._table_rows:
            table_text = "\n".join(self._table_rows)
            if table_text:
                self.blocks.append(table_text)
        self._table_rows = None
        self._table_current_row = None

    def _start_row(self) -> None:
        if self._table_rows is not None:
            self._table_current_row = []

    def _end_row(self) -> None:
        if self._table_rows is not None and self._table_current_row is not None:
            row_text = "\t".join(self._table_current_row)
            self._table_rows.append(row_text)
        self._table_current_row = None

    def _start_cell(self) -> None:
        if self._table_current_row is not None:
            self._buffer = []
            self._in_cell = True

    def _end_cell(self) -> None:
        if self._in_cell and self._table_current_row is not None:
            cell_text = _WHITESPACE_RE.sub(" ", "".join(self._buffer)).strip()
            self._table_current_row.append(cell_text)
            self._buffer = []
        self._in_cell = False

    # -- HTMLParser 回调 -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if self._skipping():
            if _is_noise_element(tag, attrs):
                self._skip_stack.append(tag)
            return
        if _is_noise_element(tag, attrs):
            self._skip_stack.append(tag)
            return

        if tag == "table":
            self._start_table()
        elif tag == "tr":
            self._start_row()
        elif tag in ("td", "th"):
            self._start_cell()
        elif tag in _BLOCK_TAGS:
            self._block_boundary()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # 自闭合标签（如 <br/>）：起止事件都触发一次，行为等价于 handle_starttag。
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
            return
        if self._skipping():
            return

        if tag == "table":
            self._end_table()
        elif tag == "tr":
            self._end_row()
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag in _BLOCK_TAGS:
            self._block_boundary()

    def handle_data(self, data: str) -> None:
        if self._skipping():
            return
        self._buffer.append(data)

    def get_text(self) -> str:
        self._flush()
        if self._table_rows is not None:
            self._end_table()
        return "\n".join(b for b in self.blocks if b)


def html_to_text(html: Optional[str]) -> str:
    """HTML 正文 -> 纯文本。字段缺失 / 畸形 HTML 都不崩，优雅降级。"""
    if not html:
        return ""
    try:
        extractor = _HtmlTextExtractor()
        extractor.feed(html)
        return extractor.get_text()
    except Exception:
        # html.parser 本身对畸形标签是容错的，这里只是兜底：万一真的抛出，
        # 退化成正则去标签，保留数据总比丢数据强。
        return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()
