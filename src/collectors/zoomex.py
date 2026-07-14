"""Zoomex 采集器（我方基线，非竞品）。

【重要：与 sources.yaml/phasePrompts.md 原计划的一处偏离，已用真实请求验证】
原计划是「翻页直到遇到 update_time <= watermark 的条目即停止」，假设列表接口按
gmtUpdatedAt 降序排列。实测（2026-07-14，pageNum=1/2/3 各抽样 5 条）发现列表既不是
按 gmtUpdatedAt 排序，也不严格按 order/gmtCreatedAt 排序（同页内出现非单调序列），
无法安全依赖任何排序假设做提前退出。见 CLAUDE.md Phase 2 批次 2 记录。

改为更稳健的做法：
1. fetch_list() 每轮翻完该 menu_id 下的全部页（列表请求本身很便宜，EN Platform
   Announcement 552 条 = 19 次请求），不依赖排序提前退出。
2. needs_detail() 对每条列表条目，用 DB 里已存的 update_time 做比对；只有真正新增
   或 update_time 变化的条目才会触发一次详情请求（这才是真正昂贵、需要省的部分——
   正文只有详情接口 getArticleById 才有）。
这样即使列表顺序不可预测，也不会漏掉「排在后面但被编辑过」的旧文章，且没有对已存
文章做多余的详情请求，达到跟 watermark 策略同等的省网络请求效果。

Zoomex 每个 locale 有 3-4 个 menu_id（categories，见 sources.yaml），互相独立翻页，
一个 ZoomexCollector 实例 = 一个 locale × 一个 menu_id，crawl_state 用 category 区分
（见 src/db/schema.sql 的 crawl_state.category 列）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch_json, rate_limit_seconds
from src.collectors.timeutil import ms_to_iso
from src.db.operations import compute_uid
from src.parsers.slate_json import parse_content as parse_slate_content
from src.parsers.zoomex import get_total_count, parse_detail_response, parse_list_response


class ZoomexCollector(BaseCollector):
    source_name = "Zoomex"

    def __init__(self, locale: str, config: dict[str, Any], category_key: str, menu_id: int):
        super().__init__(locale, config)
        self.category = category_key  # crawl_state 第三个 key
        self.menu_id = menu_id
        self.lang_code = config["lang_code"]

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # since 有意不用于提前退出翻页，见本文件顶部说明；増量效果由 needs_detail() 实现。
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        page_size = pagination.get("page_size", 30)
        rate_limit_s = rate_limit_seconds(cfg)
        headers = cfg.get("headers") or {}

        items: list[RawItem] = []
        page_num = 1
        total_count: Optional[int] = None
        while True:
            body = json.dumps(
                {
                    "lang": self.lang_code,
                    "pageNum": page_num,
                    "pageSize": page_size,
                    "parentId": self.menu_id,
                }
            ).encode()
            payload = fetch_json(cfg["endpoint"], method="POST", headers=headers, body=body)
            if total_count is None:
                total_count = get_total_count(payload)

            raw_articles = parse_list_response(payload, self.lang_code)
            if not raw_articles:
                break
            for raw in raw_articles:
                items.append(
                    RawItem(
                        article_id=raw["article_id"],
                        title=raw["title"],
                        post_time=ms_to_iso(raw["post_time"]),
                        update_time=ms_to_iso(raw["update_time"]),
                    )
                )

            if total_count is not None and page_num * page_size >= total_count:
                break
            page_num += 1
            time.sleep(rate_limit_s)
        return items

    def needs_detail(self, conn, item: RawItem) -> bool:
        if item.article_id is None:
            return True
        uid = compute_uid(self.source_name, self.locale, str(item.article_id))
        row = conn.execute(
            "SELECT update_time FROM announcements WHERE uid = ?", (uid,)
        ).fetchone()
        return row is None or row["update_time"] != item.update_time

    def fetch_detail(self, item: RawItem) -> RawItem:
        cfg = self.config
        rate_limit_s = rate_limit_seconds(cfg)
        time.sleep(rate_limit_s)

        body = json.dumps({"id": item.article_id}).encode()
        payload = fetch_json(
            cfg["detail_endpoint"], method="POST", headers=cfg.get("headers") or {}, body=body
        )
        detail = parse_detail_response(payload, self.lang_code)
        item.title = detail["title"] or item.title
        item.content = parse_slate_content(detail["content"])
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        article_id = str(item.article_id)
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=article_id,
            # Zoomex 详情页 URL 规则只在 EN 确认过一个真实样例
            # （help.zoomex.com/en/article/3858，Phase 1 侦察记录），其它 locale 的
            # path segment 没有逐个验证过，按"不允许猜测数据"的约束先留空。
            url=None,
            title=item.title,
            content=item.content,
            post_time=item.post_time,
            update_time=item.update_time,
            category=None,  # Phase 3 之前不分类
            group_id=f"zoomex_{article_id}",
            source_endpoint=self.config.get("endpoint"),
        )
