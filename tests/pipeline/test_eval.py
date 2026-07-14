"""分层抽样单测：验证抽样跨 (source, locale, category) 组合分布，不会被基数最大的
组合淹没，也验证 target 超过可用行数时不会抛异常（有多少给多少）。"""

from __future__ import annotations

from src.pipeline.eval import stratified_sample


def _rows(source, locale, category, n):
    return [
        {"uid": f"{source}-{locale}-{category}-{i}", "source": source, "locale": locale,
         "category": category, "article_id": str(i), "title": "t", "raw_category": "x",
         "layer": "native", "reason": "r"}
        for i in range(n)
    ]


def test_sample_covers_multiple_groups_not_dominated_by_largest():
    rows = _rows("Weex", "EN", "listing", 1000) + _rows("Weex", "EN", "campaign", 2) + _rows(
        "Bitunix", "EN", "delisting", 2
    )
    sample = stratified_sample(rows, target=6)
    categories_seen = {(r["source"], r["category"]) for r in sample}
    assert ("Weex", "campaign") in categories_seen
    assert ("Bitunix", "delisting") in categories_seen
    assert ("Weex", "listing") in categories_seen


def test_sample_size_capped_by_available_rows():
    rows = _rows("Weex", "EN", "listing", 3)
    sample = stratified_sample(rows, target=30)
    assert len(sample) == 3


def test_sample_respects_target_when_enough_rows():
    rows = _rows("Weex", "EN", "listing", 50)
    sample = stratified_sample(rows, target=10)
    assert len(sample) == 10
