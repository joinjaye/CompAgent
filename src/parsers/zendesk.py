"""Zendesk Help Center articles.json 解析（Bitunix / Weex 共用，标准响应格式）。

只负责把响应结构变成好取用的 dict list，不做 field_mapping 之外的加工（时间转 UTC、
article_id 转 str、group_id 拼接等留给 collector.normalize）。
"""

from __future__ import annotations

from typing import Any, Optional


def parse_articles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从一页 articles.json 响应里取出条目列表。

    字段确实缺失时对应 key 给 None，不抛异常（graceful degradation）；非 dict 的
    脏条目直接跳过。
    """
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []

    result: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        result.append(
            {
                "article_id": article.get("id"),
                "title": article.get("title"),
                "content": article.get("body"),
                "post_time": article.get("created_at"),
                "update_time": article.get("updated_at"),
                "section_id": article.get("section_id"),
                "url": article.get("html_url") or article.get("url"),
            }
        )
    return result


def get_next_cursor(payload: dict[str, Any]) -> Optional[str]:
    """cursor 分页（JSON:API 风格，meta.has_more / meta.after_cursor）：还有更多结果时
    返回 opaque cursor 字符串，否则 None。

    【Phase 2.7 订正】原来的 get_next_page() 直接跟 payload["next_page"] 走（经典
    offset 分页），实测证伪了两个假设：(1) Zendesk 经典 offset 分页硬性限制最多翻到
    page=100（超过就 400，不管 per_page 是多少，Weex listings_delistings 3199 条、
    per_page=30 时在 page=101 触发），继续加大 per_page 只是把触发点往后挪，治标不
    治本；(2) 换成 cursor 分页（`page[size]`/`page[after]`）后，响应里的
    `links.next` 字段本身有 bug——URL 缺了 `.json` 后缀（`/articles` 而不是
    `/articles.json`），直接请求会 415（Unsupported Media Type）。所以这里只返回
    cursor 值本身，不返回完整 URL；调用方（collector）用自己已知的 endpoint 重新拼
    URL，不依赖响应里的 next 链接。cursor 分页在 Bitunix（support.bitunix.com）和
    Weex（weexsupport.zendesk.com）两个 Zendesk 实例上均已用真实请求验证可用
    （标准 Zendesk API 能力，非账号定制）。
    """
    meta = payload.get("meta")
    if not isinstance(meta, dict) or not meta.get("has_more"):
        return None
    return meta.get("after_cursor")
