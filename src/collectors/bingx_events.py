"""BingX 活动中心采集器（bingx.com/{locale}/events）——本项目第一个、也是目前唯一
一个依赖真实浏览器运行时的采集器。

背景：常规公告采集（src/collectors/bingx.py，走 `bingx.com/{locale}/support/
notice-center` 的 Nuxt 首屏聚合视图）覆盖的是帮助中心公告，不是营销活动。
`/events` 页面本身纯客户端渲染，`__NUXT_DATA__` 里只有导航壳、没有活动数据（跟
CLAUDE.md「BingX 签名保护」记录的现象一致）。真实用 Playwright 抓包确认了背后
真实请求：`GET https://api-app.qq-os.com/api/act-operation/v1/activity/center
?page=N&pageSize=10&status=2`，带一整套签名头（`sign`/`device_id`/`timestamp`/
`traceid`...），是跟 CLAUDE.md 记录的 BingX 签名 API 同一套。顺着签名调用链追到
`bc()` 函数后，因为经过 bundler 拆包重命名，继续手工跨 chunk 追踪工作量不可
预估——这次不再重复当初放弃这条路的结论。

**改用浏览器驱动方案**（已跟用户确认这个取舍）：不逆向签名算法，而是启动真实
headless Chromium 打开 `/events` 页面，让页面自己的 JS 计算出合法签名、发出真实
请求，我们只是拦截响应。这是本项目目前唯一一个"每次采集都要跑一次真实浏览器"的
采集器——比其余全部纯 HTTP 请求的采集器慢得多、重得多（需要 Playwright +
Chromium 二进制常驻），也更脆弱（依赖页面结构/接口调用方式不变，任何一次 BingX
前端改版都可能让这里静默失败）。其余三个新增的活动端口（Bitunix/Lbank/Weex）
都不需要浏览器，纯 HTTP 请求即可，因为它们各自的活动数据要么是服务端直接渲染进
HTML（Bitunix/Lbank），要么是干净的 `__NEXT_DATA__`（Weex）——只有 BingX 这一个
是真正客户端渲染 + 签名保护的组合。

**分页现状（如实记录，不是遗漏）**：`activity/center` 响应本身是真分页的
（`total`/`pageSize` 字段真实存在，EN `total=21` > 单页 10 条），但真实测试过
的滚动交互（模拟用户滚到底）只会触发 page=1 这一次请求，找不到任何可点击的
"加载更多"/翻页控件（页面上出现的"More"文本是顶部导航菜单项，跟活动列表无关）；
手动在页面 JS 上下文里直接 `fetch()` page=2 会因为绕过了应用自己的签名封装、
拿到 `设备时间不正确` 的错误——说明签名不是挂在全局 `window.fetch` 上的简单
拦截器，无法这样借道。也就是说**这个采集器目前只能拿到 page=1（10 条）**，
`pagination.max_pages` 配置本身保留（万一以后 BingX 前端改版出现真正的翻页/
加载更多交互，只需要在 `_capture_activity_pages()` 里补上真实验证过的触发方式，
不需要改采集器其余逻辑），但当前运行时如果 `max_pages > 1` 会记一条警告说明
只拿到了 1 页，不会假装拿到了更多。

`article_id` 加 `promocenter-` 前缀，理由跟另外三个新端口一致（避免跟常规
BingX 公告流的 `articleId` 数值空间偶然撞号）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.collectors.base import BaseCollector, NormalizedAnnouncement, RawItem
from src.collectors.timeutil import offset_iso_to_utc_iso

logger = logging.getLogger(__name__)

_ARTICLE_ID_PREFIX = "promocenter-"
_ACTIVITY_CENTER_URL_PATTERN = "**/api/act-operation/v1/activity/center**"
_PAGE_SIZE = 10
_SCROLL_ATTEMPTS = 8
_NAV_TIMEOUT_MS = 45000
_POST_LOAD_WAIT_MS = 5000


def _capture_activity_pages(url: str, max_pages: int) -> list[dict[str, Any]]:
    """启动一次 headless Chromium，打开活动中心页面，拦截真实签名请求的响应。

    真实测试过滚动交互只会触发 page=1（见本文件顶部注释），这里仍然按
    `max_pages` 循环等待+滚动多轮，是为了给"以后页面行为变化、真的能触发翻页"
    留出空间，不是假装现在就支持——拿到几页就返回几页，不足 `max_pages` 时
    调用方（`fetch_list`）会记警告，不会拿不存在的数据充数。
    """
    from playwright.sync_api import sync_playwright  # 延迟 import：只有这个采集器需要

    captured: dict[str, dict[str, Any]] = {}  # 按 URL 去重，同一页可能因为重渲染触发多次

    def on_response(response: Any) -> None:
        if "act-operation/v1/activity/center" not in response.url:
            return
        try:
            body = response.json()
        except Exception:
            return
        if body.get("code") == 0:
            captured[response.url] = body

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except Exception:
                logger.warning("BingX events 页面加载超时（继续尝试拦截已发出的请求）：%s", url)
            page.wait_for_timeout(_POST_LOAD_WAIT_MS)

            for _ in range(_SCROLL_ATTEMPTS):
                if len(captured) >= max_pages:
                    break
                try:
                    page.mouse.wheel(0, 2000)
                except Exception:
                    break
                page.wait_for_timeout(1000)
        finally:
            browser.close()

    return list(captured.values())


class BingXEventsCollector(BaseCollector):
    source_name = "BingX"

    def __init__(self, locale: str, config: dict[str, Any]):
        super().__init__(locale, config)
        self.category = "activity_center"

    def fetch_list(self, since: Optional[str]) -> list[RawItem]:
        # full_scan：没有可靠的"最后编辑时间"，只有活动起止时间，since 不参与判断。
        cfg = self.config
        pagination = cfg.get("pagination") or {}
        max_pages = pagination.get("max_pages", 2) if not self.force_full else 999

        pages = _capture_activity_pages(cfg["endpoint"], max_pages)
        if not pages:
            logger.warning("BingX events 未捕获到任何真实响应，本轮跳过：%s", cfg["endpoint"])
            return []

        total = pages[0].get("data", {}).get("total")
        if total is not None and len(pages) * _PAGE_SIZE < total:
            logger.warning(
                "BingX events 只拿到 %d 页（约 %d 条），源端 total=%d，见本文件顶部"
                "「分页现状」说明——不是本轮网络故障，是当前浏览器交互找不到翻页触发方式。",
                len(pages), len(pages) * _PAGE_SIZE, total,
            )

        seen_ids: set[str] = set()
        items: list[RawItem] = []
        for page_body in pages:
            for entry in page_body.get("data", {}).get("activityCenterInfoVos", []):
                activity_id = entry.get("activityId")
                if activity_id is None or str(activity_id) in seen_ids:
                    continue
                seen_ids.add(str(activity_id))
                tags = ", ".join(t.get("name", "") for t in (entry.get("tags") or []) if t.get("name"))
                items.append(
                    RawItem(
                        article_id=activity_id,
                        title=entry.get("title"),
                        content=tags,
                        post_time=None,
                        url=entry.get("activityUrl"),
                        extra={
                            "start_time_raw": entry.get("beginTime"),
                            "end_time_raw": entry.get("endTime"),
                        },
                    )
                )
        return items

    def normalize(self, item: RawItem) -> NormalizedAnnouncement:
        article_id = f"{_ARTICLE_ID_PREFIX}{item.article_id}"
        content_text = item.content or ""
        activity_start = offset_iso_to_utc_iso(item.extra.get("start_time_raw"))
        activity_end = offset_iso_to_utc_iso(item.extra.get("end_time_raw"))
        period = _format_period(activity_start, activity_end)
        if period:
            content_text = f"{content_text}\n\n{period}" if content_text else period
        return NormalizedAnnouncement(
            source=self.source_name,
            locale=self.locale,
            article_id=article_id,
            url=item.url,
            title=item.title,
            content=content_text,
            post_time=item.post_time,
            update_time=None,
            activity_start_time=activity_start,
            activity_end_time=activity_end,
            category=None,
            raw_category=self.category,
            group_id=f"bingx_{article_id}",
            source_endpoint=self.config.get("endpoint"),
        )


def _format_period(start: Optional[str], end: Optional[str]) -> str:
    if not start and not end:
        return ""
    return f"活动周期: {start or '?'} ~ {end or '?'}"
