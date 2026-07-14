"""slate_json.py 离线单测：Slate.js 富文本 JSON → 纯文本，基于
tests/fixtures/zoomex_article_detail.json 真实响应快照 + 手写小样本覆盖边界情况。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.parsers.slate_json import parse_content, slate_to_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _real_content_string() -> str:
    payload = json.loads((FIXTURES / "zoomex_article_detail.json").read_text(encoding="utf-8"))
    contents = payload["result"]["contents"]
    en = next(c for c in contents if c["lang"] == "en-US")
    return en["content"]


# ---------------------------------------------------------------- 正常解析 ----

def test_parse_content_extracts_plain_text_from_real_fixture():
    text = parse_content(_real_content_string())
    assert "Dear Zoomex Traders" in text
    assert "Zoomex has officially listed several new Stock Perpetual contracts" in text


def test_parse_content_preserves_table_structure_from_real_fixture():
    text = parse_content(_real_content_string())
    assert "USDT Perpetual Contract\tUnderlying Asset\tMax Leverage" in text
    assert "PURRUSDT" in text


def test_slate_to_text_joins_paragraphs_with_newline():
    nodes = [
        {"type": "paragraph", "children": [{"text": "first"}]},
        {"type": "paragraph", "children": [{"text": "second"}]},
    ]
    assert slate_to_text(nodes) == "first\nsecond"


def test_slate_to_text_handles_nested_inline_elements():
    nodes = [
        {
            "type": "paragraph",
            "children": [
                {"text": "see "},
                {"type": "link", "url": "https://example.com", "children": [{"text": "here"}]},
                {"text": " for details"},
            ],
        }
    ]
    assert slate_to_text(nodes) == "see here for details"


def test_slate_to_text_renders_table_rows_tab_separated_and_newline_between_rows():
    nodes = [
        {
            "type": "table",
            "children": [
                {
                    "type": "table-row",
                    "children": [
                        {"type": "table-cell", "children": [{"text": "Header A"}], "isHeader": True},
                        {"type": "table-cell", "children": [{"text": "Header B"}], "isHeader": True},
                    ],
                },
                {
                    "type": "table-row",
                    "children": [
                        {"type": "table-cell", "children": [{"text": "1"}]},
                        {"type": "table-cell", "children": [{"text": "2"}]},
                    ],
                },
            ],
        }
    ]
    assert slate_to_text(nodes) == "Header A\tHeader B\n1\t2"


# ---------------------------------------------------------------- 缺字段/异常不崩 ----

def test_parse_content_empty_or_none_returns_empty_string():
    assert parse_content(None) == ""
    assert parse_content("") == ""


def test_parse_content_invalid_json_falls_back_to_raw_string_without_crashing():
    assert parse_content("not valid json {{{") == "not valid json {{{"


def test_slate_to_text_skips_non_dict_nodes_gracefully():
    nodes = ["not-a-node", None, {"type": "paragraph", "children": [{"text": "ok"}]}]
    assert slate_to_text(nodes) == "ok"


def test_slate_to_text_non_list_input_returns_empty_string():
    assert slate_to_text({"type": "paragraph"}) == ""
    assert slate_to_text(None) == ""


def test_slate_to_text_node_with_no_children_and_no_text_is_empty():
    assert slate_to_text([{"type": "paragraph"}]) == ""
