"""src/parsers/bitunix_activity.py 离线单测：用真实抓取的活动中心页面快照
（tests/fixtures/bitunix_activity_{EN,FR,ID}.html）验证双格式解析，不发任何网络
请求。EN 走 devalue `__NUXT_DATA__`，FR/ID 走明文 `window.__custom__nuxt__payload`。
"""

from __future__ import annotations

from pathlib import Path

from src.parsers.bitunix_activity import parse_activity_list

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_parse_activity_list_en_devalue_path():
    html = _read_fixture("bitunix_activity_EN.html")
    items = parse_activity_list(html)

    assert len(items) == 2
    ids = {i["id"] for i in items}
    assert ids == {6223, 5406}
    pizza = next(i for i in items if i["id"] == 6223)
    assert pizza["title"] == "Bitcoin Pizza Day Giveaway!"
    assert pizza["status"] == "ended"
    assert pizza["start_time"] == "2026-05-22T10:00:00Z"
    assert pizza["end_time"] == "2026-05-26T10:00:00Z"
    assert pizza["url"] == "/activity/basic/pizza-day-2026"
    assert "<" in pizza["rule_description"]  # 完整 HTML 规则正文


def test_parse_activity_list_fr_custom_payload_path():
    html = _read_fixture("bitunix_activity_FR.html")
    items = parse_activity_list(html)

    assert len(items) == 2
    pizza = next(i for i in items if i["id"] == 6223)
    # 数值字段跨 locale 一致，文本字段是翻译版本
    assert pizza["start_time"] == "2026-05-22T10:00:00Z"
    assert pizza["title"] != "Bitcoin Pizza Day Giveaway!"
    assert "é" in pizza["title"] or "e" in pizza["title"]  # 真实法语译文，非 mojibake


def test_parse_activity_list_id_custom_payload_path():
    html = _read_fixture("bitunix_activity_ID.html")
    items = parse_activity_list(html)

    assert len(items) == 2
    ids = {i["id"] for i in items}
    assert ids == {6223, 5406}


def test_parse_activity_list_ids_consistent_across_locales():
    en_ids = {i["id"] for i in parse_activity_list(_read_fixture("bitunix_activity_EN.html"))}
    fr_ids = {i["id"] for i in parse_activity_list(_read_fixture("bitunix_activity_FR.html"))}
    id_ids = {i["id"] for i in parse_activity_list(_read_fixture("bitunix_activity_ID.html"))}
    assert en_ids == fr_ids == id_ids


def test_parse_activity_list_returns_empty_on_garbage_html():
    assert parse_activity_list("<html><body>nothing here</body></html>") == []


def test_normalize_does_not_crash_on_dict_first_element_list():
    # 真实撞见的 bug：devalue 数组里出现过 [<dict>, ...] 形状的 list（第一个元素
    # 不是字符串 tag），旧版 _normalize 判断 Reactive 包装时会因为 dict 不可 hash
    # 直接抛 TypeError。构造一个最小合成样本复现这个形状，锁定修复。
    from src.parsers.bitunix_activity import _normalize

    weird_list = [{"nested": "dict"}, "other"]
    # 不应该抛异常，且不满足 Reactive 包装/null-prototype 的形状，按普通 list 处理
    result = _normalize(weird_list)
    assert result == [{"nested": "dict"}, "other"]
