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


def get_next_page(payload: dict[str, Any]) -> Optional[str]:
    """Zendesk 分页响应自带下一页的完整 URL，直接跟着走，不需要自己拼页码。"""
    return payload.get("next_page")
