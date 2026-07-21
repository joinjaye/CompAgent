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

【2026-07-14 补充：daily 增量改为分页数上限，force_full 才全量翻页】
上面第 1 点"每轮翻完全部页"在建仓阶段没问题，但作为**每天都要跑一次**的常规增量，
对 menu_id 26（EN 552 条 ≈ 19 页）这种量级每天都全量翻一遍是不必要的开销。改为：
`fetch_list()` 默认（`force_full=False`，即 `run()` 走的正常增量路径）只翻
`pagination.max_pages`（见 sources.yaml，当前配置为 5）页就停，配合 `needs_detail()`
对这个窗口内的条目做增量判断；`force_full=True` 时忽略这个上限，翻完全部页
（建仓/定期全量核查用 `--force-full` 触发）。

**这是一个有意接受的正确性权衡，不是没有代价**：由于本文件顶部已经证实列表接口
不按任何可靠字段排序（不是 update_time 降序、也不是 created 降序），"新变更的文章
一定出现在前 5 页"这个假设本身没有被验证过，无法排除某篇很早以前创建、排在第 10 页
之后的旧文章被源站编辑过，而当天的 5 页窗口扫不到它、导致更新被漏采一天（不是永久
丢失——它仍然会被下一次 `--force-full` 全量核查捕获，只是增量运行之间存在滞后）。
接受这个权衡的前提是：Zoomex 是我方基线（对比基准），不是竞品情报本身，且已经安排
定期 `--force-full` 复核来兜底（cadence 由运维脚本决定，本文件不假设具体周期）。
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
        # max_pages：force_full=False（daily 增量）时只翻这么多页就停，见本文件顶部
        # "分页数上限"说明；force_full=True（建仓/全量核查）时忽略上限、翻完全部页。
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        page_size = pagination.get("page_size", 30)
        max_pages = None if self.force_full else pagination.get("max_pages", 5)
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
            if max_pages is not None and page_num >= max_pages:
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
        content = parse_slate_content(detail["content"])
        if not content.strip():
            # 2026-07-21 真实核查（curl getArticleById 逐条验证）：空正文有两种真实
            # 情况，都不是解析 bug——(1) isRedirect=true 的"跳转型"文章，正文本来
            # 就不在 content 字段里，真实内容在 redirectUrl 指向的落地页（该页是
            # help.zoomex.com 同款纯客户端渲染 SPA 壳，538555 字节固定壳，抓不到
            # 真实文案，见文件顶部 SPA 说明）；(2) Slate.js 内容本身只有一张图片、
            # 没有文字节点（如新年祝福图）。两种情况都不允许把 content 存成空字符串
            # 让它悄悄从下游分析/ZMX 目录里消失——退化到用标题兜底，好过完全没有
            # 信号；不去抓 redirectUrl 落地页（SPA 抓不到真实内容，尝试了也白搭）。
            item.content = item.title or ""
        else:
            item.content = content
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
            raw_category=str(self.menu_id),
            group_id=f"zoomex_{article_id}",
            source_endpoint=self.config.get("endpoint"),
        )
