"""Zoomex 详情接口 content 字段解析：Slate.js 富文本 JSON → 纯文本。

原始格式形如 `[{"type":"paragraph","children":[{"text":"..."}]}]`（本身是一个 JSON
字符串，需要先 json.loads 才是嵌套结构）。递归遍历所有节点提取 text 值，段落之间保留
换行；表格（type: table/table-row/table-cell）额外保留行列结构（行用换行分隔、列用
制表符分隔），因为活动奖池信息经常放在表格里，拍平成一坨文字会丢掉这层信息。
"""

from __future__ import annotations

import json
from typing import Any, Optional


def _inline_text(node: Any) -> str:
    """把一个 inline 节点（text 叶子 / link / image 等）拼成纯文本，递归拼接 children，
    不关心块级结构（段落/表格由调用方处理）。"""
    if not isinstance(node, dict):
        return ""
    children = node.get("children")
    if not children:
        return node.get("text") or ""
    return "".join(_inline_text(c) for c in children if isinstance(c, dict))


def _render_table(node: dict[str, Any]) -> str:
    rows = []
    for row in node.get("children") or []:
        if not isinstance(row, dict) or row.get("type") != "table-row":
            continue
        cells = [
            _inline_text(cell).strip()
            for cell in (row.get("children") or [])
            if isinstance(cell, dict) and cell.get("type") == "table-cell"
        ]
        if cells:
            rows.append("\t".join(cells))
    return "\n".join(rows)


def _render_block(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "table":
        return _render_table(node)
    return _inline_text(node).strip()


def slate_to_text(nodes: Any) -> str:
    """content 字段（Slate.js 富文本 JSON 数组，已 json.loads 过）→ 纯文本。"""
    if not isinstance(nodes, list):
        return ""
    blocks = [_render_block(n) for n in nodes]
    return "\n".join(b for b in blocks if b)


def parse_content(raw_content: Optional[str]) -> str:
    """content 字段的原始值是 JSON 字符串。缺失 / 不是合法 JSON 时优雅降级：
    空值给空字符串，解析失败给原始字符串本身（保留数据总比丢数据强），不抛异常。
    """
    if not raw_content:
        return ""
    if not isinstance(raw_content, str):
        return slate_to_text(raw_content)
    try:
        nodes = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return raw_content
    return slate_to_text(nodes)
