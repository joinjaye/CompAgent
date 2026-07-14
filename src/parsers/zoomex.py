"""Zoomex getArticleListByMenuId / getArticleById 响应解析。

两个接口都是标准 JSON（不需要 devalue/preloadedData 那种脏解析），但结构上每篇文章的
`contents[]` 数组里混着全部 locale 的标题/正文，需要按 lang 精确匹配挑出目标 locale
的那一份——这部分匹配逻辑独立出来，方便离线单测，也避免 collector 里写重复的按 lang
过滤代码。
"""

from __future__ import annotations

from typing import Any, Optional


def _match_lang(contents: Any, lang: str) -> Optional[dict[str, Any]]:
    if not isinstance(contents, list):
        return None
    for entry in contents:
        if isinstance(entry, dict) and entry.get("lang") == lang:
            return entry
    return None


def get_total_count(payload: dict[str, Any]) -> int:
    return ((payload or {}).get("result") or {}).get("totalCount") or 0


def parse_list_response(payload: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """列表接口（getArticleListByMenuId）一页响应 -> [{article_id, title, post_time,
    update_time}]。只有 contents[] 里存在该 lang 标题的条目才算这个 locale 下真实存在
    的文章（不同 locale 的已发布文章子集不同，见 sources.yaml 侦察记录），没有命中的
    条目跳过——即这个 locale 还没有这篇文章，不是数据缺失。
    """
    content_list = ((payload or {}).get("result") or {}).get("content")
    if not isinstance(content_list, list):
        return []

    items = []
    for entry in content_list:
        if not isinstance(entry, dict):
            continue
        matched = _match_lang(entry.get("contents"), lang)
        if matched is None:
            continue
        article = entry.get("article") or {}
        items.append(
            {
                "article_id": article.get("id"),
                "title": matched.get("title"),
                "post_time": article.get("gmtCreatedAt"),
                "update_time": article.get("gmtUpdatedAt"),
            }
        )
    return items


def parse_detail_response(payload: dict[str, Any], lang: str) -> dict[str, Any]:
    """详情接口（getArticleById）响应 -> {article_id, title, content(原始 Slate JSON
    字符串), post_time, update_time}。目标 lang 不存在时 title/content 为 None
    （graceful degradation，不抛异常）。
    """
    result = (payload or {}).get("result") or {}
    article = result.get("article") or {}
    matched = _match_lang(result.get("contents"), lang)
    return {
        "article_id": article.get("id"),
        "title": matched.get("title") if matched else None,
        "content": matched.get("content") if matched else None,
        "post_time": article.get("gmtCreatedAt"),
        "update_time": article.get("gmtUpdatedAt"),
    }
