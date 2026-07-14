"""人工抽查用：跨 source × locale × category 分层抽样打印分类结果，方便肉眼核对
准确率。不是纯随机——纯随机会被 Weex listing（基数最大）淹没，看不到别的类别。

抽样对象是"分类结果"这个三元组 (source, locale, category)（category 是 classify_row
算出来的最终值，不依赖 announcements.category 是否已经写库），每个组合尽量雨露均沾，
轮询取样直到凑够 target 条或所有组合耗尽。
"""

from __future__ import annotations

import random
import sqlite3
from typing import Optional

from src.pipeline.category import classify_row


def collect_classified_rows(
    conn: sqlite3.Connection,
    mapping: dict[str, Optional[dict[str, str]]],
    sources: tuple[str, ...] = ("Bitunix", "Weex", "Zoomex"),
) -> list[dict]:
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"SELECT uid, source, locale, article_id, title, raw_category FROM announcements "
        f"WHERE source IN ({placeholders})",
        sources,
    ).fetchall()

    result = []
    for uid, source, locale, article_id, title, raw_category in rows:
        r = classify_row(source, raw_category, title, mapping)
        result.append(
            {
                "uid": uid,
                "source": source,
                "locale": locale,
                "article_id": article_id,
                "title": title,
                "raw_category": raw_category,
                "category": r.category,
                "layer": r.layer,
                "reason": r.reason,
            }
        )
    return result


def stratified_sample(rows: list[dict], target: int = 30, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["source"], row["locale"], row["category"])
        groups.setdefault(key, []).append(row)

    for key in groups:
        rng.shuffle(groups[key])

    keys_sorted = sorted(groups.keys(), key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    sample: list[dict] = []
    exhausted = set()
    idx = 0
    while len(sample) < target and len(exhausted) < len(keys_sorted):
        key = keys_sorted[idx % len(keys_sorted)]
        idx += 1
        if key in exhausted:
            continue
        bucket = groups[key]
        if not bucket:
            exhausted.add(key)
            continue
        sample.append(bucket.pop())
        if not bucket:
            exhausted.add(key)
    return sample


def print_sample(rows: list[dict]) -> None:
    for row in rows:
        print("=" * 90)
        print(f"source={row['source']} locale={row['locale']} article_id={row['article_id']}")
        print(f"title={row['title']!r}")
        print(f"raw_category={row['raw_category']} -> category={row['category']}  layer={row['layer']}")
        print(f"reason={row['reason']}")
