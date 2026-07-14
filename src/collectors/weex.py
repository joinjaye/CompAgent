"""Weex 采集器。

2026-07-14 起改为解析 www.weex.com 前台页面，**不再**走 weexsupport.zendesk.com 的
Zendesk REST API——那套 API 已经过期（实测确认从 2026-05-16 起再未更新过，但用户在
前台页面能看到当天发布的公告），完整迁移记录见 config/sources.yaml 的 weex 块注释
和 CLAUDE.md「Weex 数据源迁移」。解析细节（flight 流 / zendesk-html div）见
src/parsers/weex_web.py 顶部注释。

跟旧版（继承 ZendeskCollector）的关键差异：
- 不再是 detail_mode=inline：列表页只有 id/title/createdAt/sectionId，正文必须
  单独请求详情页解析。
- 不再有 update_time：strategy=full_scan，watermark 完全不适用，needs_detail()
  用默认实现（恒 True）——没有任何字段可以安全地判断"詳情要不要重新抓"，只能靠
  content_hash 兜底做变更检测（upsert_announcement 已经处理好这部分）。
- 分页是普通 `?page=N` 查询参数，不依赖排序做提前退出（列表条目有 prioritise 置顶
  标记，可能不按时间顺序排最前，同 Zoomex 批次 2 教训），只用 pagination.max_pages
  限制扫描深度，--force-full 时忽略上限、翻到 totalPage 为止。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.http import fetch as http_fetch
from src.collectors.http import rate_limit_seconds
from src.collectors.timeutil import ms_to_iso
from src.parsers.html_text import html_to_text
from src.parsers.weex_web import extract_article_body_html, parse_article_list, parse_page_info

logger = logging.getLogger(__name__)

# robots.txt（www.weex.com/robots.txt，2026-07-14 核对）明确 Disallow 的两篇文章，
# 尊重这个限制，遇到直接跳过、不抓取。
DISALLOWED_ARTICLE_IDS = {"f8gmpz85kfw6teot9f9xpa09", "gqv1v2r402ocvucwvllzz0mj"}


class WeexCollector(BaseCollector):
    source_name = "Weex"

    def __init__(self, locale: str, config: dict[str, Any], category_key: str = ""):
        super().__init__(locale, config)
        self.category = category_key  # crawl_state 第三个 key；full_scan 不写水位线，
        # 但仍保留这个字段用于 --category 过滤和多分类展开的一致性

    def _resolve_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        locale_path = self.config.get("locale_path", self.locale.lower())
        return f"https://www.weex.com/{locale_path}{url}"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan 策略：since 不参与判断（没有 per-item update_time，见 sources.yaml
        # 注释），只受 pagination.max_pages 限制扫描深度；--force-full 时忽略上限，
        # 翻到 totalPage 为止。
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        max_pages = None if self.force_full else pagination.get("max_pages", 5)
        rate_limit_s = rate_limit_seconds(cfg)
        endpoint = cfg["endpoint"]

        items: list[RawItem] = []
        page = 1
        while max_pages is None or page <= max_pages:
            url = f"{endpoint}?page={page}"
            html = http_fetch(url)
            page_items = parse_article_list(html)
            if not page_items:
                break
            for entry in page_items:
                article_id = entry["article_id"]
                if article_id in DISALLOWED_ARTICLE_IDS:
                    continue
                items.append(
                    RawItem(
                        article_id=article_id,
                        title=entry["title"],
                        post_time=ms_to_iso(entry["post_time_ms"]),
                        category_raw=entry["section_id"],
                        url=self._resolve_url(entry["url"]),
                    )
                )

            info = parse_page_info(html)
            if info is not None and page >= info[1]:
                break

            time.sleep(rate_limit_s)
            page += 1

        return items

    def fetch_detail(self, item: RawItem) -> RawItem:
        if not item.url:
            logger.warning("Weex 文章缺少详情页 URL，跳过正文抓取：article_id=%s", item.article_id)
            return item
        html = http_fetch(item.url)
        body_html = extract_article_body_html(html)
        if body_html is None:
            logger.warning(
                "Weex 详情页未找到 zendesk-html 容器，正文置空：article_id=%s url=%s",
                item.article_id, item.url,
            )
        item.content = body_html
        return item

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        raw_category = str(item.category_raw) if item.category_raw is not None else None
        content_text = html_to_text(item.content) if item.content else ""
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=str(item.article_id),
            url=item.url,
            title=item.title,
            content=content_text,
            post_time=item.post_time,
            update_time=None,
            raw_category=raw_category,
            group_id=f"weex_{item.article_id}",
            source_endpoint=self.config.get("endpoint"),
        )
