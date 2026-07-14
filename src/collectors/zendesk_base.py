"""Zendesk Help Center 采集器（Bitunix / Weex 共用逻辑，见 config/sources.yaml 对应侦察注释）。

watermark 策略：URL 上带 sort_by=updated_at&sort_order=desc，服务端原生按 updated_at 降序
排好序，翻页直到遇到 update_time <= since 的条目即可安全提前退出（不需要翻完全部页）。
since=None（首次抓取，crawl_state 还没有水位线）时会翻到 next_page 耗尽为止，等价于一次全量回填。
"""

from __future__ import annotations

import time
from typing import Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch_json, rate_limit_seconds
from src.parsers.html_text import html_to_text
from src.parsers.zendesk import get_next_page, parse_articles


class ZendeskCollector(BaseCollector):
    group_id_prefix: str  # 子类设置，如 "bitunix" / "weex"，见 phasePrompts.md 跨语言 group_id 约定

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        page_size_param = pagination.get("page_size_param", "per_page")
        page_size = pagination.get("page_size", 100)
        rate_limit_s = rate_limit_seconds(cfg)

        endpoint = cfg["endpoint"]
        sep = "&" if "?" in endpoint else "?"
        url: Optional[str] = f"{endpoint}{sep}sort_by=updated_at&sort_order=desc&{page_size_param}={page_size}"

        items: list[RawItem] = []
        stop = False
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
            next_url = None if stop else get_next_page(payload)
            if next_url:
                time.sleep(rate_limit_s)
            url = next_url
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
