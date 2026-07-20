"""CLI: python -m src.collectors [--source bitunix] [--locale EN] [--category x] [--force-full]

按 config/sources.yaml 驱动地跑一遍采集，落库到 SQLite。单源失败不影响其他源
（失败写日志、计入 failed 计数，继续跑下一个）。

COLLECTOR_BUILDERS 目前登记了全部 6 个交易所（Bitunix / Weex / Zoomex / BingX /
Phemex / Lbank，Phase 2 批次 1-4 全部完成）；sources.yaml 里出现的新 source 需要
在这里补登记才会被 CLI 认领，否则跳过不报错。

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
from src.collectors.bingx import BingXCollector
from src.collectors.bingx_events import BingXEventsCollector
from src.collectors.bitunix import BitunixCollector
from src.collectors.bitunix_activity import BitunixActivityCollector
from src.collectors.lbank import LbankCollector
from src.collectors.lbank_events import LbankEventsCollector
from src.collectors.phemex import PhemexCollector
from src.collectors.weex import WeexCollector
from src.collectors.weex_rewards import WeexRewardsCollector
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


def _campaign_endpoint_config(cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 locale 配置块下的 `campaign_endpoint` 子块跟父级 cfg 合并成一份完整
    config（父级字段打底、`campaign_endpoint` 自己的字段覆盖，如 `endpoint`/
    `pagination`/`strategy`/`rate_limit_ms`），供活动端口专属的 collector 使用。
    没有 `campaign_endpoint` 时返回 None（该 locale 暂未配置活动端口）。"""
    campaign_cfg = cfg.get("campaign_endpoint")
    if not campaign_cfg:
        return None
    merged = {**cfg, **campaign_cfg}
    merged.pop("categories", None)
    merged.pop("campaign_endpoint", None)
    return merged


def _bitunix_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """Bitunix：常规 Zendesk 采集（单实例/categories 展开，逻辑完全不变，复用
    `_categorized_collector_builder`）+ 新增的 `campaign_endpoint`（活动中心
    www.bitunix.com/activity/act-center，非 Zendesk）额外产出一个
    `BitunixActivityCollector` 实例，两路数据并集写入同一个 source，见
    src/collectors/bitunix_activity.py 顶部注释。"""
    collectors = _categorized_collector_builder(BitunixCollector)(locale, cfg)
    campaign_config = _campaign_endpoint_config(cfg)
    if campaign_config:
        collectors.append(BitunixActivityCollector(locale, campaign_config))
    return collectors


def _weex_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """Weex：常规帮助中心采集（categories 展开，逻辑不变）+ 新增的
    `campaign_endpoint`（活动奖励 www.weex.com/rewards）额外产出一个
    `WeexRewardsCollector` 实例，见 src/collectors/weex_rewards.py 顶部注释。"""
    collectors = _categorized_collector_builder(WeexCollector)(locale, cfg)
    campaign_config = _campaign_endpoint_config(cfg)
    if campaign_config:
        collectors.append(WeexRewardsCollector(locale, campaign_config))
    return collectors


def _bingx_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """BingX：常规首屏聚合采集（逻辑不变）+ 新增的 `campaign_endpoint`（活动中心
    bingx.com/{locale}/events，浏览器驱动）额外产出一个 `BingXEventsCollector`
    实例，见 src/collectors/bingx_events.py 顶部注释。"""
    collectors = _categorized_collector_builder(BingXCollector)(locale, cfg)
    campaign_config = _campaign_endpoint_config(cfg)
    if campaign_config:
        collectors.append(BingXEventsCollector(locale, campaign_config))
    return collectors


def _zoomex_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """多分类源：一个 locale 配置块下的每个 categories.<name>（menu_id）各展开成一个实例。"""
    categories = cfg.get("categories") or {}
    collectors: list[BaseCollector] = []
    for category_key, category_cfg in categories.items():
        merged_cfg = {**cfg}
        merged_cfg.pop("categories", None)
        collectors.append(ZoomexCollector(locale, merged_cfg, category_key, category_cfg["menu_id"]))
    return collectors


def _phemex_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """多分类源：2026-07-14 分页升级后，全部 categories.<name> 共用同一个
    `list_endpoint`（真实分页 API），只是请求参数里的 `list_category_id` 不同，
    合并进 config 供 PhemexCollector.fetch_list() 读取（不是构造函数的位置参数，
    跟 Zoomex/Lbank 把分类标识当位置参数传不同，因为 Phemex 只需要一个数字 id，
    直接放 config 里更简单）。"""
    categories = cfg.get("categories") or {}
    collectors: list[BaseCollector] = []
    for category_key, category_cfg in categories.items():
        merged_cfg = {**cfg, "list_category_id": category_cfg["list_category_id"]}
        merged_cfg.pop("categories", None)
        collectors.append(PhemexCollector(locale, merged_cfg, category_key))
    return collectors


def _lbank_builder(locale: str, cfg: dict[str, Any]) -> list[BaseCollector]:
    """多分类源：全部 categories.<name> 共用同一个 endpoint（真实 JSON API，见
    src/collectors/lbank.py），只是请求体里的 categoryCode 不同，跟 Zoomex 的
    menu_id 模式一致（不是 Bitunix/Weex/Phemex 那种"每个分类各自独立 endpoint"）。
    额外新增的 `campaign_endpoint`（精选活动 www.lbank.com/new-popular-events，
    跟 `notice/latestList` 是完全不同的接口）产出一个 `LbankEventsCollector`
    实例，见 src/collectors/lbank_events.py 顶部注释。"""
    categories = cfg.get("categories") or {}
    collectors: list[BaseCollector] = []
    for category_key, category_cfg in categories.items():
        merged_cfg = {**cfg}
        merged_cfg.pop("categories", None)
        collectors.append(LbankCollector(locale, merged_cfg, category_key, category_cfg["category_code"]))
    campaign_config = _campaign_endpoint_config(cfg)
    if campaign_config:
        collectors.append(LbankEventsCollector(locale, campaign_config))
    return collectors


COLLECTOR_BUILDERS: dict[str, CollectorBuilder] = {
    "bitunix": _bitunix_builder,
    "weex": _weex_builder,
    "zoomex": _zoomex_builder,
    "bingx": _bingx_builder,
    "phemex": _phemex_builder,
    "lbank": _lbank_builder,
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
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help=(
            "只保留 update_time/post_time 落在最近 N 天内的条目，替代无限回填。"
            "watermark 策略源（如 Bitunix）在 crawl_state 为空时用它播种 since 下限，"
            "避免首次运行等价于全量历史回填；full_scan 策略源（Weex/BingX/Phemex/Lbank）"
            "用它过滤掉 pagination 窗口里过旧的条目。不传则保留现状（无日期限制）。"
            "对 --force-full 无效。"
        ),
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
            stats = collector.run(conn, force_full=args.force_full, lookback_days=args.lookback_days)
            all_stats.append(stats)
            logger.info(
                "完成 %s：new=%d changed=%d unchanged=%d failed=%d skipped_by_date=%d",
                label, stats.new, stats.changed, stats.unchanged, stats.failed, stats.skipped_by_date,
            )

    print(
        f"{'source':<10} {'locale':<8} {'new':<6} {'changed':<8} {'unchanged':<10} "
        f"{'failed':<6} {'skipped_by_date':<15}"
    )
    for s in all_stats:
        print(
            f"{s.source:<10} {s.locale:<8} {s.new:<6} {s.changed:<8} {s.unchanged:<10} "
            f"{s.failed:<6} {s.skipped_by_date:<15}"
        )

    total_failed = sum(s.failed for s in all_stats)
    if total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
