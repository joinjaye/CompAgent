"""Zendesk Help Center 采集器（Bitunix / Weex 共用逻辑，见 config/sources.yaml 对应侦察注释）。

watermark 策略：URL 上带 sort_by=updated_at&sort_order=desc，服务端原生按 updated_at 降序
排好序，翻页直到遇到 update_time <= since 的条目即可安全提前退出（不需要翻完全部页）。
since=None（首次抓取，crawl_state 还没有水位线）时会翻到没有更多结果为止，等价于一次全量回填。

分页机制（Phase 2.7 起改为 cursor 分页，JSON:API 风格 page[size]/page[after]）：
批次 1 用的是经典 offset 分页（page=/per_page=），实测证伪了"跟着 next_page 走就行"这个
假设——Zendesk 经典 offset 分页硬性限制最多翻到 page=100（不管 per_page 是多少），超过就
400，Weex 新增的 listings_delistings 分类（3199 条，per_page=30）在 page=101 触发了这个
限制。换成 cursor 分页后又发现响应里的 links.next 字段本身有 bug（URL 缺 .json 后缀，
直接请求会 415），所以改成只从 meta.after_cursor 取 cursor 值，用已知的 endpoint 自己
拼下一页 URL，不依赖响应里的任何链接字段。cursor 分页已在 Bitunix/Weex 两个 Zendesk
实例上用真实请求验证可用，且验证过 sort_by=updated_at&sort_order=desc 在 cursor 模式下
仍然生效（3199 条抽样全程严格降序），watermark 提前退出翻页的安全性不受影响。见
CLAUDE.md「Phase 2.7」。

多分类：Weex 从 Phase 2.7 起有两个 Zendesk category（Latest Announcements / Listings/
Delistings，各自互相独立翻页），一个 ZendeskCollector 实例 = 一个 locale × 一个 category，
crawl_state 用 category 区分（见 src/db/schema.sql 的 crawl_state.category 列），跟
ZoomexCollector 的 menu_id 是同一个模式。单分类源（Bitunix，以及未加 categories 结构的
config）不传 category_key，crawl_state.category 恒为 ''，行为不变。
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch_json, rate_limit_seconds
from src.parsers.html_text import html_to_text
from src.parsers.zendesk import get_next_cursor, parse_articles


class ZendeskCollector(BaseCollector):
    group_id_prefix: str  # 子类设置，如 "bitunix" / "weex"，见 phasePrompts.md 跨语言 group_id 约定

    def __init__(self, locale: str, config: dict[str, Any], category_key: str = ""):
        super().__init__(locale, config)
        self.category = category_key  # crawl_state 第三个 key；单分类源恒为 ''

    def _page_url(self, cursor: Optional[str] = None) -> str:
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        page_size = pagination.get("page_size", 100)
        endpoint = cfg["endpoint"]
        sep = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{sep}page%5Bsize%5D={page_size}&sort_by=updated_at&sort_order=desc"
        if cursor:
            url += f"&page%5Bafter%5D={quote(cursor, safe='')}"
        return url

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        cfg = self.config
        rate_limit_s = rate_limit_seconds(cfg)

        items: list[RawItem] = []
        stop = False
        url: Optional[str] = self._page_url()
        while url and not stop:
            payload = fetch_json(url, method="GET", headers=cfg.get("headers") or {})
            for raw_article in parse_articles(payload):
                update_time = raw_article["update_time"]
                if since is not None and update_time is not None and update_time <= since:
                    stop = True
                    break
                items.append(
                    RawItem(
                        article_id=raw_article["article_id"],
                        title=raw_article["title"],
                        content=raw_article["content"],
                        post_time=raw_article["post_time"],
                        update_time=update_time,
                        url=raw_article["url"],
                        category_raw=raw_article["section_id"],
                    )
                )
            cursor = None if stop else get_next_cursor(payload)
            url = self._page_url(cursor) if cursor else None
            if url:
                time.sleep(rate_limit_s)
        return items

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        article_id = str(item.article_id)
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=article_id,
            url=item.url,
            title=item.title,
            content=html_to_text(item.content),  # Zendesk body 是原始 HTML，采集层清洗为纯文本
            post_time=item.post_time,
            update_time=item.update_time,
            category=None,  # Phase 3 之前不分类，见 CLAUDE.md「category 可为 NULL」
            raw_category=str(item.category_raw) if item.category_raw is not None else None,
            group_id=f"{self.group_id_prefix}_{article_id}",
            source_endpoint=self.config.get("endpoint"),
        )
