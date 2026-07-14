"""Lbank 采集器（2026-07-14 重写，走真实 JSON API，见 src/parsers/lbank.py 顶部
注释和 CLAUDE.md「Lbank 真实 API 重写」）。

跟 Zoomex 一样，一个 LbankCollector 实例 = 一个 locale × 一个顶层分类
（`categories.*` 的 category_key/category_code），crawl_state 用 category 区分。

**force_full 不再是 no-op**：新 API 真正支持 `pageNo` 翻页，`force_full=True` 时
忽略 `pagination.max_pages` 上限、翻到 `hasNext=false`（或 `resultList` 返回空）
为止，等同 Zoomex 的全量核查语义。但仍然遵守项目政策（见 CLAUDE.md「水位逻辑策略
调整」）：Lbank 默认（`force_full=False`）只翻前 `max_pages` 页，不是每天都全量
翻一遍——这一点跟 Zoomex/Weex 完全一致，只是 Lbank 从"没有这个能力"变成"有能力但
默认不用"。

strategy=full_scan：`latestList` 默认排序是 `noticeId` 降序（等价创建顺序），不是
按 `updateTime`，无法安全依赖排序做提前退出翻页判断"内容是否被编辑过"（同 Zoomex
批次 2 教训），变更检测交给 `upsert_announcement` 的 content_hash 比对。

正文来源是列表接口的 `content` 字段（已经是纯文本，真实抽样比对跟详情接口的正文
实质一致，见 lbank.py 顶部说明），`fetch_detail()` 只用来补 `columnId`（叶子分类）+
`updateTime`，不覆盖 `content`/`title`——跟 BingX/Phemex/旧版 Weex 的"详情页覆盖
列表页字段"是反过来的，这里列表接口才是正文的权威来源。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch_json, rate_limit_seconds
from src.collectors.timeutil import ms_to_iso
from src.parsers.html_text import html_to_text
from src.parsers.lbank import parse_detail_response, parse_list_response

logger = logging.getLogger(__name__)


class LbankCollector(BaseCollector):
    source_name = "Lbank"

    def __init__(self, locale: str, config: dict[str, Any], category_key: str, category_code: str):
        super().__init__(locale, config)
        self.category = category_key  # crawl_state 第三个 key，如 "new_listings"
        self.category_code = category_code  # 请求体里的 categoryCode，如 "CO00000053"
        self.lang_header = config["lang_header"]  # ex-language 请求头的值，如 "vi-VN"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # since 不参与判断，见本文件顶部 strategy=full_scan 说明。
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        page_size = pagination.get("page_size", 50)
        max_pages = None if self.force_full else pagination.get("max_pages", 5)
        rate_limit_s = rate_limit_seconds(cfg)
        headers = {"Content-Type": "application/json", "ex-language": self.lang_header}

        items: list[RawItem] = []
        page_no = 1
        while True:
            body = json.dumps(
                {
                    "pageNo": page_no,
                    "pageSize": page_size,
                    "topCategory": "NOTICE",
                    "categoryCode": self.category_code,
                }
            ).encode()
            payload = fetch_json(cfg["endpoint"], method="POST", headers=headers, body=body)
            raw_items = parse_list_response(payload)
            if not raw_items:
                break
            for raw in raw_items:
                if raw.get("code") is None:
                    continue
                items.append(
                    RawItem(
                        article_id=raw["notice_id"],
                        title=raw["title"],
                        content=raw["content"],
                        post_time=ms_to_iso(raw["post_time_ms"]),
                        extra={"code": raw["code"]},
                    )
                )

            if max_pages is not None and page_no >= max_pages:
                break
            page_no += 1
            time.sleep(rate_limit_s)
        return items

    def fetch_detail(self, item: RawItem) -> RawItem:
        rate_limit_s = rate_limit_seconds(self.config)
        time.sleep(rate_limit_s)

        code = item.extra.get("code")
        detail_url = self.config["detail_endpoint"].format(code=code)
        headers = {"ex-language": self.lang_header}
        payload = fetch_json(detail_url, method="GET", headers=headers)
        detail = parse_detail_response(payload)
        if detail is None:
            logger.warning("Lbank 详情接口解析失败，仅用列表数据：notice_id=%s code=%s", item.article_id, code)
            return item
        item.update_time = ms_to_iso(detail.get("update_time_ms"))
        if detail.get("create_time_ms") is not None:
            item.post_time = ms_to_iso(detail["create_time_ms"])
        item.category_raw = detail.get("column_id")
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        article_id = str(item.article_id)
        content_text = html_to_text(item.content) if item.content else ""
        code = item.extra.get("code")
        locale_path = self.config.get("locale_path", "")
        url = f"https://www.lbank.com/{locale_path}support/articles/{code}" if code else None
        # raw_category 用详情接口给的 columnId（真实叶子分类，比请求时用的顶层
        # categoryCode 更精确，跟 Weex 用真实 section_id 做 raw_category 是同一个
        # 惯例）；detail 请求失败时 item.category_raw 不会被设置，兜底退回顶层
        # categoryCode，保证这一列不会因为一次详情请求失败就变成 NULL。
        raw_category = item.category_raw if item.category_raw is not None else self.category_code
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=article_id,
            url=url,
            title=item.title,
            content=content_text,
            post_time=item.post_time,
            update_time=item.update_time,
            category=None,  # Phase 3 之前不分类
            raw_category=str(raw_category),
            group_id=f"lbank_{article_id}",  # noticeId 跨 locale 一致，Phase 1 已确认
            source_endpoint=self.config.get("endpoint"),
        )
