"""采集器基类：统一 fetch_list / fetch_detail / normalize 契约，
以及基于 crawl_state.strategy（watermark / full_scan）的落库编排。

每个交易所一个子类（src/collectors/<exchange>.py），通过 config/sources.yaml 里
对应 source × locale 的配置块驱动，不在这里写任何交易所专属逻辑。
"""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.db.operations import get_crawl_state, set_crawl_state, upsert_announcement

logger = logging.getLogger(__name__)


@dataclass
class RawItem:
    """从列表页（视情况 + 详情页）拿到的原始条目。

    值仍是源端原始格式（时间未转 UTC、id 未转 str）——转换是 normalize() 的职责。
    """

    article_id: Any
    title: Optional[str] = None
    content: Optional[str] = None
    post_time: Any = None
    update_time: Any = None
    url: Optional[str] = None
    category_raw: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedAnnouncement:
    """normalize() 的输出，字段名与 upsert_announcement 的关键字参数一一对应。"""

    source: str
    locale: str
    article_id: str
    url: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    post_time: Optional[str] = None
    update_time: Optional[str] = None
    category: Optional[str] = None
    raw_category: Optional[str] = None
    group_id: Optional[str] = None
    source_endpoint: Optional[str] = None


@dataclass
class RunStats:
    source: str
    locale: str
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped_by_date: int = 0  # lookback_days 过滤掉的条目数（full_scan 策略），不是静默丢弃

    @property
    def total(self) -> int:
        return self.new + self.changed + self.unchanged + self.failed


class BaseCollector(ABC):
    """一个 source × locale 实例对应一个 Collector。"""

    source_name: str  # 落库用的 source 值，如 "Bitunix"（需与 CLAUDE.md 里的命名一致）

    category: str = ""  # crawl_state 的第三个 key。单分类源留空；多分类源（如 Zoomex 的
    # 各 menu_id）的子类 __init__ 里覆写成各自的分类标识，独立维护水位线，见 zoomex.py。

    force_full: bool = False  # run() 开始时同步成当次调用的 force_full 参数，供 fetch_list()
    # 里需要区分"daily 增量"还是"全量核查/建仓"的子类读取（目前只有 ZoomexCollector 用到，
    # 见 zoomex.py 的分页上限逻辑）。默认 False 保证直接调用 fetch_list()（不经过 run()）的
    # 单测行为不变。

    def __init__(self, locale: str, config: dict[str, Any]):
        self.locale = locale
        self.config = config
        self.strategy: str = config.get("strategy", "watermark")

    @abstractmethod
    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        """拉取列表条目。RawItem 的时间字段在这一步就应该转成 UTC ISO8601 字符串
        （不要留到 normalize 才转），因为 watermark 比较、needs_detail 的增量判断都
        需要在 fetch_list/run 阶段就能拿可比较的时间值。

        watermark 策略：语义上应只返回 update_time > since 的条目（since=None 时代表
        首次全量抓取，需要一路翻页到底）；但具体怎么实现「只返回」由子类决定——如果
        源端列表接口的翻页顺序未经验证不是按 update_time 排序（不能假设，需要实测），
        不要用「遇到 update_time <= since 就提前退出翻页」这种依赖排序的写法，应该
        返回全部条目，再靠 needs_detail() 基于已入库的 update_time 做增量判断
        （Zoomex 就是这种情况，见 zoomex.py 顶部注释）。
        full_scan 策略：since 参数忽略，返回本轮抓取范围内的全部条目，交给 normalize
        + upsert_announcement 的 content_hash 比对去判断有没有变化。
        """

    def needs_detail(self, conn: sqlite3.Connection, item: RawItem) -> bool:
        """要不要为这条条目发详情请求。默认总是 True（inline 源 fetch_detail 本来就是
        no-op，不需要省这次调用）。detail_mode=separate_api 且列表已经带 update_time 的源
        （如 Zoomex）可以覆写：查 DB 里这个 uid 当前的 update_time，没变就跳过详情请求，
        省掉一次网络调用。"""
        return True

    def fetch_detail(self, item: RawItem) -> RawItem:
        """detail_mode != inline 的源需要覆写，另请求详情页取正文。默认原样返回。"""
        return item

    @abstractmethod
    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        """把 RawItem 转成落库字段：article_id 转 str、拼 group_id 等（时间已经是 UTC
        ISO8601，见 fetch_list 的约定，这里不需要再转）。"""

    def run(
        self, conn: sqlite3.Connection, *, force_full: bool = False,
        lookback_days: Optional[int] = None, collection_date: Optional[str] = None,
    ) -> RunStats:
        """跑一轮采集并落库。

        force_full=True 时：(1) 忽略已存的 high_watermark，强制从头拉取；(2) 跳过
        needs_detail() 的增量判断，对拉到的每一条都重新请求详情——两者都是为了人工
        复核（如验证「手动改 content_hash 后能否被识别为 changed」）：正常增量运行下，
        未真正变更的旧条目本来就不会被重新拉取/重新校验，需要 force_full 才能强制
        重新过一遍全部条目。force_full 时 lookback_days 完全不生效（语义上"全量核查"
        不该被日期窗口限制）。

        lookback_days：只保留 update_time/post_time 落在最近 N 天内的条目，解决两类
        真实问题：(1) watermark 策略源（如 Bitunix）在 crawl_state 为空（首次运行/空库）
        时，since=None 会导致早停条件永远不触发、翻页翻到底——等价于一次全量历史回填，
        给它播种一个 cutoff 下限能避免这个情况；(2) full_scan 策略源（Weex/BingX/Phemex/
        Lbank）完全不做任何日期过滤，只靠 pagination.max_pages 圈一个固定页数窗口，这个
        窗口对应的时间跨度可能是"今天"也可能是"过去两年"（取决于该分类的发布频率），
        过滤后能让"daily 增量"这个语义名副其实。默认 None 完全不影响现有行为（不传时
        逐字节兼容修改前的历史调用）。
        """
        self.force_full = force_full
        stats = RunStats(source=self.source_name, locale=self.locale)
        since = None
        if self.strategy == "watermark" and not force_full:
            state = get_crawl_state(conn, self.source_name, self.locale, category=self.category)
            since = state["high_watermark"] if state else None

        cutoff = None
        cutoff_end = None
        if collection_date is not None and not force_full:
            day = datetime.strptime(collection_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            cutoff = day.strftime("%Y-%m-%dT%H:%M:%SZ")
            cutoff_end = (day + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if since is None or since < cutoff:
                since = cutoff
        if lookback_days is not None and not force_full:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if since is None:
                since = cutoff

        try:
            items = self.fetch_list(since)
        except Exception:
            logger.exception("拉取列表失败：%s/%s", self.source_name, self.locale)
            stats.failed += 1
            return stats

        if cutoff is not None and (collection_date is not None or self.strategy == "full_scan"):
            kept = []
            for raw in items:
                item_date = raw.update_time or raw.post_time
                if item_date is not None and item_date >= cutoff and (cutoff_end is None or item_date < cutoff_end):
                    kept.append(raw)
                else:
                    stats.skipped_by_date += 1
            items = kept

        max_update_time = since
        for raw in items:
            try:
                if not force_full and not self.needs_detail(conn, raw):
                    stats.unchanged += 1
                    if raw.update_time and (max_update_time is None or raw.update_time > max_update_time):
                        max_update_time = raw.update_time
                    continue

                detailed = self.fetch_detail(raw)
                ann = self.normalize(detailed)
                result = upsert_announcement(
                    conn,
                    source=ann.source,
                    locale=ann.locale,
                    article_id=ann.article_id,
                    url=ann.url,
                    title=ann.title,
                    content=ann.content,
                    post_time=ann.post_time,
                    update_time=ann.update_time,
                    category=ann.category,
                    raw_category=ann.raw_category,
                    source_endpoint=ann.source_endpoint,
                    group_id=ann.group_id,
                )
                if result.status == "new":
                    stats.new += 1
                elif result.status == "changed":
                    stats.changed += 1
                else:
                    stats.unchanged += 1

                if ann.update_time and (max_update_time is None or ann.update_time > max_update_time):
                    max_update_time = ann.update_time
            except Exception:
                logger.exception(
                    "处理条目失败：%s/%s article_id=%s", self.source_name, self.locale, raw.article_id
                )
                stats.failed += 1

        if self.strategy == "watermark":
            set_crawl_state(
                conn,
                source=self.source_name,
                locale=self.locale,
                high_watermark=max_update_time,
                strategy="watermark",
                category=self.category,
            )

        return stats
