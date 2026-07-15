"""Lbank 公告 JSON API 响应解析（2026-07-14 起，替代旧版 RSC flight 流抓页方案，
见 CLAUDE.md「Lbank 真实 API 重写」）。

背景：Phase 2 批次 4 最初实现时只找到 `www.lbank.com` 前台 SSR 页面（默认聚合视图
固定 10 条，`?pageNo=`/`?page=` 均被服务端忽略，翻页被判定为未逆向），采集器走的是
解析 Next.js RSC flight 流（`src/parsers/lbank_web.py`，已废弃删除）。用户要求投入
headless browser 抓包核实后，找到了页面 hydration 之后真正调用的匿名 JSON API：

- 列表：`POST https://www.lbank.com/lbk-api/huamao-media-center/notice/latestList`
  body `{"pageNo": N, "pageSize": M, "topCategory": "NOTICE", "categoryCode": "<code>"}`，
  **真正支持翻页**（pageNo 递增返回完全不同的条目，非重复/非忽略）、**真正支持分类
  筛选**（categoryCode 传顶层分类 code 会聚合其全部子分类，如 "CO00000053" New
  Listings 聚合 Spot/Futures/Copy Trading 三个子分类，总量 6909 条，远超默认视图的
  10 条）。响应里每条已经带完整 `content`（纯文本，偶有 HTML 实体如 `&rsquo;`，
  交给 `html_to_text` 顺手转掉，即使没有真正的标签也不影响）。**不需要**登录/cookie/
  签名，只需要请求头 `ex-language: <en-US|vi-VN|id>` 控制语言（`Accept-Language`
  标准请求头不可靠——实测 "id" 这个值走 Accept-Language 不生效，必须用这个应用自定义
  的 `ex-language` 头，`vi-VN`/`en-US`/`id` 三个值均已用真实请求验证）。
- 详情：`GET .../notice/content/{code}?noticeCode={code}`，同样只需要 `ex-language`
  头。返回 `noticeContent.columnId`（该公告实际归属的叶子分类数值 id）+ `createTime`/
  `updateTime`（均 unix 毫秒，可靠）。**详情接口的 `content` 字段本身是一个指向另一个
  域名（`jiz.lbank.com`）静态文本文件的 URL，不是字面量**——真实抓过一次这个 URL，
  内容跟列表接口的 `content` 字段实质一致（同一篇长文本抽样比对，字符数几乎相等，只是
  详情版本多了 HTML 标签/换行），不值得为了这点差异再多一跳网络请求，本模块因此不
  解析这个字段。【2026-07-15 简化，见 CLAUDE.md「Lbank 真实 API 重写」末尾追加记录】
  采集器（`src/collectors/lbank.py`）已不再调用这个详情接口——`updateTime` 对
  full_scan 策略没有作用（不驱动 watermark），`columnId` 的精度超过了下游实际需要
  的粒度（下游只用到 7 个顶层分类）。`parse_detail_response` 函数已删除，这段说明
  仅保留作为该接口存在、结构如上的历史记录，以后如果需要更细粒度分类可以参考。
- 分类树：`POST .../notice/category/list` body `{"topCategory": "NOTICE"}`，返回
  7 个顶层 tab（跟 Phase 1 补充侦察记录的页面级 tab 代码树一致：LBank VIP/New
  Listings/Event Announcements/System Upgrades & Maintenance/Platform Updates/
  Delisting Information/Fiat，各自 categoryId/code 已用真实请求核对），每个顶层 tab
  下还有子分类——但 `latestList` 的 `categoryCode` 传顶层 code 就会自动聚合全部子
  分类，不需要逐个子分类单独请求（已用真实请求验证：`categoryCode=CO00000053`
  返回的条目分布在 Spot/Futures/Copy Trading 三个子分类，不是只有顶层本身）。本项目
  按顶层 tab 粒度采集（`config/sources.yaml` 的 `categories.*`），不逐子分类展开，
  跟 Weex 用 category 级聚合（而不是 section 级）是同一个设计选择。
"""

from __future__ import annotations

from typing import Any, Optional


def parse_list_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`notice/latestList` 响应 -> 文章条目 dict list。字段：notice_id/code/title/
    content（已经是纯文本/带少量 HTML 实体，非 RSC 引用）/post_time_ms。响应结构
    异常时返回空 list，不抛异常。"""
    result_list = (payload.get("data") or {}).get("resultList")
    if not isinstance(result_list, list):
        return []
    items = []
    for item in result_list:
        if not isinstance(item, dict) or item.get("noticeId") is None:
            continue
        items.append(
            {
                "notice_id": item.get("noticeId"),
                "code": item.get("code"),
                "title": item.get("title"),
                "content": item.get("content"),
                "post_time_ms": item.get("contentShowTime"),
            }
        )
    return items


def get_total_count(payload: dict[str, Any]) -> Optional[int]:
    total = (payload.get("data") or {}).get("totalCount")
    return total if isinstance(total, int) else None
