"""BingX 公告页面（bingx.com/{locale}/support/...）解析。

Nuxt 3 SSR，数据内嵌在 `<script type="application/json" data-nuxt-data="nuxt-app"
id="__NUXT_DATA__">` 里，devalue 格式的扁平数组：整数元素是同一数组内的索引引用
（可以直接 json.loads 再手动解引用，不需要专门的 devalue 库，Phase 1 侦察已确认，
见 CLAUDE.md/sources.yaml bingx 块）。2026-07-14 实现前用真实请求逐层核对过下面
两种页面的结构（不是凭 Phase 1 侦察记录的字段名猜的解析路径）：

- 列表页（.../support/notice-center）：解出的 `data` 字典下唯一一个带 `articles`
  字段的条目（实测 key 是 `"support-notice-center-0"`，但页面构建 ID 可能随部署
  变化，不依赖固定 key 名，改为遍历 `data` 找带 `articles` 字段的 value）就是本页
  数据，固定 20 条、跨分区聚合，不是可翻页的分页接口——`?page=`/`?sectionId=` 等
  query 参数不改变 SSR 输出，真正的翻页是未逆向的客户端交互（Phase 1 结论，本次
  未重新验证是否仍然如此）。每条 article 字段：articleId/newArticleId/sectionId/
  newSectionId/weight/title/createTime/updateTime/promoted。
- 详情页（.../support/articles/{articleId}）：`data` 字典下唯一一个带
  `articleData` 字段的条目（实测顶层 key 是形如 `$xxxxxxxxxx` 的随机变量名，
  同样不依赖固定名字，遍历查找）。articleData 字段：categoryId/categoryPathsStr/
  articleId/sectionId/title/body（HTML）/lang/createTime（没有独立的 updateTime，
  跟"列表页 createTime==updateTime 恒等"的观察一致）。

devalue 编码里出现的几种标记（本文件只处理这几种，遇到未识别的标记原样保留在结构
里、不报错——调用方按 key 取不到期望字段时会自然拿到 None/空 list，比强行解析未
验证过的新标记更安全）：
  ["ShallowReactive"/"Reactive"/"Ref", <ref>]  —— Vue 响应式包装，去掉标记取内层值
  ["null", k1, v1, k2, v2, ...]                —— `Object.create(null)` 实例的
                                                    devalue 编码，转普通 dict
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_NUXT_DATA_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_REACTIVE_TAGS = {"ShallowReactive", "Reactive", "Ref"}


def _resolve_all(raw: list[Any]) -> list[Any]:
    """把 devalue 扁平数组里的整数索引引用，逐个解成实际值。只有列表/字典内部的
    整数元素才是引用，字符串/浮点/布尔/None 等原样保留（devalue 数组本身也可能
    包含字面量整数，但本项目用到的字段都不是纯数字数组元素，未观察到误判场景）。"""
    cache: dict[int, Any] = {}

    def resolve(idx: int) -> Any:
        if idx in cache:
            return cache[idx]
        cache[idx] = None  # 环路守卫（未观察到真实自引用场景，仅做防御，避免死循环）
        v = raw[idx]
        if isinstance(v, list):
            resolved = [resolve(x) if isinstance(x, int) else x for x in v]
        elif isinstance(v, dict):
            resolved = {k: (resolve(x) if isinstance(x, int) else x) for k, x in v.items()}
        else:
            resolved = v
        cache[idx] = resolved
        return resolved

    return [resolve(i) for i in range(len(raw))]


def _normalize(value: Any) -> Any:
    """解引用之后的收尾清洗：去掉 Reactive 包装、把 null-prototype 编码转成 dict。"""
    if isinstance(value, list):
        if len(value) == 2 and value[0] in _REACTIVE_TAGS:
            return _normalize(value[1])
        if value and value[0] == "null" and len(value) % 2 == 1:
            pairs = value[1:]
            return {k: _normalize(v) for k, v in zip(pairs[0::2], pairs[1::2])}
        return [_normalize(x) for x in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _load_nuxt_data(html: str) -> Optional[dict[str, Any]]:
    """提取 + 解引用 + 收尾清洗 __NUXT_DATA__，返回顶层 `data` 字典（各个
    page-level useAsyncData key 的集合）。找不到/解析失败返回 None。"""
    m = _NUXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    resolved_all = _resolve_all(raw)
    root = _normalize(resolved_all[0])
    if not isinstance(root, dict):
        return None
    data = _normalize(root.get("data"))
    return data if isinstance(data, dict) else None


def parse_article_list(html: str) -> list[dict[str, Any]]:
    """列表页（首屏聚合视图，固定约 20 条跨分区公告，不是分页接口）-> 文章条目
    dict list。字段：article_id/title/create_time（原始 +08:00 偏移字符串）/
    update_time（同上）/section_id。找不到数据时返回空 list，不抛异常。"""
    data = _load_nuxt_data(html)
    if not data:
        return []
    page_data = next(
        (v for v in data.values() if isinstance(v, dict) and isinstance(v.get("articles"), list)),
        None,
    )
    if page_data is None:
        return []
    result: list[dict[str, Any]] = []
    for item in page_data["articles"]:
        if not isinstance(item, dict) or item.get("articleId") is None:
            continue
        result.append(
            {
                "article_id": item.get("articleId"),
                "title": item.get("title"),
                "create_time": item.get("createTime"),
                "update_time": item.get("updateTime"),
                "section_id": item.get("sectionId"),
            }
        )
    return result


def parse_article_detail(html: str) -> Optional[dict[str, Any]]:
    """详情页 -> {title, body, section_id, create_time}；解析不到返回 None
    （调用方应该记日志，不要把 None 当空字符串静默吞掉）。"""
    data = _load_nuxt_data(html)
    if not data:
        return None
    article = next(
        (
            v.get("articleData")
            for v in data.values()
            if isinstance(v, dict) and isinstance(v.get("articleData"), dict)
        ),
        None,
    )
    if article is None:
        return None
    return {
        "title": article.get("title"),
        "body": article.get("body"),
        "section_id": article.get("sectionId"),
        "create_time": article.get("createTime"),
    }
