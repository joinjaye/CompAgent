"""CLI: python -m src.collectors [--source bitunix] [--locale EN] [--category x] [--force-full]

按 config/sources.yaml 驱动地跑一遍采集，落库到 SQLite。单源失败不影响其他源
（失败写日志、计入 failed 计数，继续跑下一个）。

COLLECTOR_BUILDERS 目前只登记了已实现的交易所（Bitunix / Weex / Zoomex）；sources.yaml
里其余尚未实现的 source 会被跳过，不报错——后续批次实现后在这里补登记即可。

多分类源（如 Zoomex 每个 locale 下的 3-4 个 menu_id、Weex 从 Phase 2.7 起的 2 个
Zendesk category）一个 builder 会展开成多个 collector 实例，用 --category 可以只跑
其中一个分类（如调试/复核用）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.collectors.base import BaseCollector, RunStats
from src.collectors.bitunix import BitunixCollector
from src.collectors.weex import WeexCollector
from src.collectors.zoomex import ZoomexCollector
from src.db.connection import DEFAULT_DB_PATH, get_connection, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SOURCES_PATH = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"

CollectorBuilder = Callable[[str, dict[str, Any]], list[BaseCollector]]


def _categorized_collector_builder(collector_cls: type[BaseCollector]) -> CollectorBuilder:
    """Bitunix（ZendeskCollector）/Weex（2026-07-14 起改为独立的页面解析实现，见
    weex.py）共用：一个 locale 配置块下，如果有 categories.<name>（各自的
    endpoint），每个 category 展开成一个 collector 实例（跟 zoomex 的 menu_id 模式一致）；
    没有 categories 结构（如 Bitunix 早期，单分类）时按原来的方式一个 locale 一个实例，
    crawl_state.category 恒为 ''，行为不变。要求 collector_cls 的构造函数签名是
    `(locale, config)` 或 `(locale, config, category_key)`，不要求继承同一个基类。"""

    def build(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
        categories = cfg.get("categories")
        if not categories:
            return [collector_cls(locale, cfg)]
        collectors: list[BaseCollector] = []
        for category_key, category_cfg in categories.items():
            merged_cfg = {**cfg, "endpoint": category_cfg["endpoint"]}
            merged_cfg.pop("categories", None)
            collectors.append(collector_cls(locale, merged_cfg, category_key))
        return collectors

    return build


def _zoomex_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """多分类源：一个 locale 配置块下的每个 categories.<name>（menu_id）各展开成一个实例。"""
    categories = cfg.get("categories") or {}
    collectors: list[BaseCollector] = []
    for category_key, category_cfg in categories.items():
        merged_cfg = {**cfg}
        merged_cfg.pop("categories", None)
        collectors.append(ZoomexCollector(locale, merged_cfg, category_key, category_cfg["menu_id"]))
    return collectors


COLLECTOR_BUILDERS: dict[str, CollectorBuilder] = {
    "bitunix": _categorized_collector_builder(BitunixCollector),
    "weex": _categorized_collector_builder(WeexCollector),
    "zoomex": _zoomex_builder,
}


def _build_collectors(
    sources: dict[str, Any],
    source_filter: Optional[str],
    locale_filter: Optional[str],
    category_filter: Optional[str],
) -> list[BaseCollector]:
    collectors: list[BaseCollector] = []
    for source_key, locales in sources.items():
        if source_filter and source_key != source_filter:
            continue
        builder = COLLECTOR_BUILDERS.get(source_key)
        if builder is None:
            continue  # 尚未实现的源（后续批次），跳过不报错
        for locale, cfg in locales.items():
            if locale_filter and locale != locale_filter:
                continue
            for collector in builder(locale, cfg):
                if category_filter and getattr(collector, "category", "") != category_filter:
                    continue
                collectors.append(collector)
    return collectors


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.collectors")
    parser.add_argument("--source", default=None, help="只跑单个 source（如 bitunix）")
    parser.add_argument("--locale", default=None, help="只跑单个 locale（如 EN）")
    parser.add_argument("--category", default=None, help="只跑单个分类（多分类源，如 zoomex 的 menu 名）")
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="忽略已存的 high_watermark，强制全量重跑（watermark 策略的源用于人工复核）",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH))
    args = parser.parse_args()

    with open(args.sources_path, encoding="utf-8") as f:
        sources = yaml.safe_load(f)["sources"]

    collectors = _build_collectors(sources, args.source, args.locale, args.category)
    if not collectors:
        parser.error("没有匹配到任何已实现的采集器（检查 --source/--locale/--category 或 COLLECTOR_BUILDERS）")

    init_db(args.db_path)
    all_stats: list[RunStats] = []
    with get_connection(args.db_path) as conn:
        for collector in collectors:
            label = f"{collector.source_name}/{collector.locale}"
            if collector.category:
                label += f"/{collector.category}"
            logger.info("开始采集 %s", label)
            stats = collector.run(conn, force_full=args.force_full)
            all_stats.append(stats)
            logger.info(
                "完成 %s：new=%d changed=%d unchanged=%d failed=%d",
                label, stats.new, stats.changed, stats.unchanged, stats.failed,
            )

    print(f"{'source':<10} {'locale':<8} {'new':<6} {'changed':<8} {'unchanged':<10} {'failed':<6}")
    for s in all_stats:
        print(f"{s.source:<10} {s.locale:<8} {s.new:<6} {s.changed:<8} {s.unchanged:<10} {s.failed:<6}")

    total_failed = sum(s.failed for s in all_stats)
    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
