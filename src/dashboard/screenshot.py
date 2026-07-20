"""Playwright 无头浏览器截图：从渲染好的看板页面（`docs/index.html`，本地文件、本地
`http.server`、或线上 GitHub Pages URL 均可，只要能访问到同目录下的
`data/dashboard.json`）截取每个 locale 的"推送视图"（`?view=push&locale=<X>` 触发
的紧凑页面，见 docs/index.html 的 `renderPushView()`），供 src/sinks/feishu_bot.py
推送到对应飞书群。

2026-07-20 架构变更：从"打开一次看板首页 + 依次点击 locale tab 截全页图"改成
"逐个 locale 各自导航到推送视图 URL 再截图"——这是 Phase 7 看板从 locale-first
改成 category-first 6 tab（Overview/Campaign/Product/Listing/Markets/Search）之后
的必然结果：顶层不再有 `.locale-tab` 元素可点，locale 变成了 Markets/Search 内部
的筛选维度，不再是一个可以"点一下就切出一整页内容"的入口。推送视图是专门为这个
用途设计的新入口（不是六个 tab 之一，纯 URL 触发），顺带解决了一个此前一直没验证过
的真实问题：旧版整页截图（EN 实测 3742px 高）从未确认过在飞书聊天窗口里是否好看，
推送视图把内容压缩成"统计条 + 最多 8 条 priority=高 重点"，图片高度大幅下降。

代价：每个 locale 各自 `page.goto()` 会重新 fetch 一次 `dashboard.json`（5 次而不是
1 次），是有意接受的取舍——数据量本来就小（几十 KB～小几百 KB），换来的是不需要
让 Playwright 感知看板内部 JS 函数名（不用 `page.evaluate()` 调用页面内部的
`renderPushView`），更健壮也更简单。
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

VIEWPORT_WIDTH = 1180
VIEWPORT_HEIGHT = 900
DEFAULT_TIMEOUT_MS = 20000
# renderPushView() 写完 DOM 后会设置 [data-push-ready] 标记，但页面复用了
# `.tab-pane`/`fadeIn` 之外的普通元素（push 视图本身不挂 `.tab-pane`，见
# docs/index.html 顶部注释），理论上标记出现即代表渲染完成；这里仍然保留一个
# 很短的额外等待，纯粹是给图片解码/布局收敛留一点安全边际，不是在猜测动画时长。
POST_READY_SETTLE_MS = 150


def capture_push_views(base_url: str, locales: list[str], out_dir: Path | str,
                        *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Path]:
    """对每个 locale 各自导航到 `{base_url}?view=push&locale=<locale>`，等
    `[data-push-ready="<locale>"]` 出现后截全页图，返回 {locale: 图片路径}。

    某个 locale 截图失败（页面结构变了、选择器找不到、导航超时等）不影响其它
    locale——记录警告并跳过，返回结果里就不会有这个 locale 的条目，调用方
    （feishu_bot.py）据此判断哪些 locale 这次没有产出截图，不会拿一张空白/半成品
    图片去推送。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
            for locale in locales:
                try:
                    query = urlencode({"view": "push", "locale": locale})
                    url = f"{base_url}?{query}"
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    # state="attached"（不是默认的 "visible"）——这个标记元素本身是
                    # 一个空 <div>（零尺寸），Playwright 默认的可见性判断（非零
                    # 宽高）会把它当"不可见"，永远等不到，见 2026-07-20 真实调试记录。
                    page.wait_for_selector(f'[data-push-ready="{locale}"]', state="attached", timeout=timeout_ms)
                    page.wait_for_timeout(POST_READY_SETTLE_MS)
                    out_path = out_dir / f"{locale.replace('-', '_')}.png"
                    page.screenshot(path=str(out_path), full_page=True)
                    result[locale] = out_path
                    logger.info("截图完成：%s -> %s", locale, out_path)
                except Exception as e:
                    logger.warning("locale=%s 截图失败，跳过：%s", locale, e)
        finally:
            browser.close()

    return result
